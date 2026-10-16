"""Value-free occupancy reads through the service's existing provider transport."""

from dataclasses import dataclass
from pathlib import Path
from typing import Literal
import click

from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.dokploy import api, source
from control_plane.dokploy.native_copy_stream import _read_remote, NativeSourceAuthorizationDenied


@dataclass(frozen=True)
class OdooTestingUsage:
    usage: Literal["unknown", "unused", "in_use"] = "unknown"
    recent_signins: bool | None = None
    authenticated_sessions: bool | None = None

    def payload(self) -> dict[str, object]:
        return {
            "recent_signins": self.recent_signins,
            "authenticated_sessions": self.authenticated_sessions,
        }


# Runs inside the existing web container. No ORM, registry, HTTP authentication,
# session-store get/save/GC, account names, tokens or session IDs leave it.
ODOO_TESTING_USAGE_CONTAINER_PROGRAM = r"""
import configparser, json, os, stat
from pathlib import Path
import psycopg2

def read_sessions(path, database):
    # Count every authenticated or partially authenticated file, regardless of
    # age. A database/device row is not proof that a browser session has ended.
    # Bound traversal and bytes; truncation or an unreadable file is unknown.
    if path.is_symlink() or not path.is_dir():
        raise ValueError('Session store unavailable')
    pending=[path]
    visited=0
    used=False
    while pending:
        current=pending.pop()
        for entry in current.iterdir():
            visited+=1
            if visited>20000:
                raise ValueError('Session inventory exceeds bound')
            info=entry.lstat()
            if stat.S_ISLNK(info.st_mode):
                raise ValueError('Session alias is unsupported')
            if stat.S_ISDIR(info.st_mode):
                if current!=path:
                    raise ValueError('Unsupported session layout')
                pending.append(entry)
                continue
            if not stat.S_ISREG(info.st_mode) or info.st_size>1024*1024:
                raise ValueError('Session file unavailable')
            # Include legacy flat files and in-progress writes. Incomplete JSON
            # cannot establish absence. Never deserialize executable pickle data.
            with entry.open(encoding='utf-8') as handle:
                data=json.load(handle)
            if not isinstance(data,dict):
                raise ValueError('Session file unreadable')
            if data.get('db')==database and (data.get('uid') or data.get('pre_uid')):
                used=True
    return used

def probe():
    if os.environ.get('PLATFORM_INSTANCE')!='testing':
        raise ValueError('Testing only')
    database=os.environ['ODOO_DB_NAME']
    config=configparser.ConfigParser(interpolation=None)
    if config.read('/tmp/platform.odoo.conf')!=['/tmp/platform.odoo.conf']:
        raise ValueError('Web configuration unavailable')
    options=config['options']
    if options.get('db_name')!=database or options.get('db_host')!='database':
        raise ValueError('Web database configuration changed')
    data_dir=Path(options['data_dir'])
    if not data_dir.is_absolute():
        raise ValueError('Session location unavailable')
    # The supported startup hands this configuration to Odoo. A waiting gate,
    # custom command or another config cannot qualify this read.
    processes=[[a for a in p.read_bytes().split(b'\0') if a]
               for p in Path('/proc').glob('[0-9]*/cmdline')]
    config_args=[b'-c',b'/tmp/platform.odoo.conf']
    if not any((p[1:]==config_args or p[2:]==config_args) and
               any(arg.rsplit(b'/',1)[-1] in {b'odoo',b'odoo-bin',b'run_odoo_startup.py'}
                   for arg in p[:2]) for p in processes):
        raise ValueError('Serving configuration unavailable')
    session_use=read_sessions(data_dir/'sessions',database)
    connection=psycopg2.connect(
        host='database',port=options.get('db_port') or '5432',
        user=options['db_user'],password=options['db_password'],
        dbname=database,connect_timeout=5,
        options='-c default_transaction_read_only=on -c statement_timeout=10000',
    )
    try:
        with connection.cursor() as cursor:
            cursor.execute('SET TRANSACTION READ ONLY')
            # Include all accounts, including portal/admin/automation. Restored
            # production history may conservatively protect a lane too.
            cursor.execute("SELECT EXISTS (SELECT 1 FROM res_users_log WHERE create_date >= (CURRENT_TIMESTAMP AT TIME ZONE 'UTC') - interval '24 hours')")
            recent=cursor.fetchone()[0]
            if type(recent) is not bool:
                raise ValueError('Login history unavailable')
        connection.rollback()
    finally:
        connection.close()
    # Scan again after SQL: a login during collection must not be hidden behind
    # an earlier empty session inventory. No scan refreshes session timestamps.
    session_use=read_sessions(data_dir/'sessions',database) or session_use
    return {'type':'complete','recent_signins':recent,'authenticated_sessions':session_use}

if __name__=='__main__':
    try:
        result=probe()
    except Exception:
        result={'type':'complete','unavailable':True}
    print(json.dumps(result),flush=True)
"""


ODOO_TESTING_USAGE_HOST_PROGRAM = r"""
import base64,json,subprocess,sys
spec=json.loads(base64.b64decode(sys.argv[1],validate=True))
prefix=sys.argv[2]

def docker(*args,stdin=None):
    return subprocess.run(['docker',*args],input=stdin,capture_output=True,check=True,timeout=25).stdout

def containers():
    ids=docker('ps','-aq','--filter','label=com.docker.compose.project='+spec['project']).decode().split()
    if not ids or len(ids)>16:
        raise ValueError('Container inventory unavailable')
    values=json.loads(docker('inspect',*ids))
    if not isinstance(values,list) or len(values)!=len(ids):
        raise ValueError('Container inventory incomplete')
    services={}
    for value in values:
        config=value['Config']
        labels=config['Labels']
        if labels.get('com.docker.compose.project')!=spec['project']:
            raise ValueError('Container changed')
        service=labels.get('com.docker.compose.service')
        if service not in {'web','database'}:
            continue
        if service in services or value['State'].get('Running') is not True:
            raise ValueError('Ambiguous or stopped service')
        services[service]=value
    if set(services)!={'web','database'}:
        raise ValueError('Service unavailable')
    web=services['web']
    env=dict(item.split('=',1) for item in web['Config']['Env'] if '=' in item)
    if env.get('PLATFORM_CONTEXT')!=spec['context'] or env.get('PLATFORM_INSTANCE')!='testing' or env.get('ODOO_DB_HOST')!='database':
        raise ValueError('Testing identity unavailable')
    # A custom startup may select a different config/session store.
    command=web['Config'].get('Cmd') or []
    if not any('run_odoo_startup.py -c /tmp/platform.odoo.conf' in arg for arg in command):
        raise ValueError('Unsupported web command')
    networks=web['NetworkSettings']['Networks']
    db_networks=services['database']['NetworkSettings']['Networks']
    if not set(networks)&set(db_networks):
        raise ValueError('Local database unavailable')
    return {key:{'Id':value['Id'],'Config':value['Config'],'Mounts':value['Mounts'],
                 'NetworkSettings':value['NetworkSettings'],
                 'StartedAt':value['State']['StartedAt']} for key,value in services.items()}

try:
    before=containers()
    output=docker('exec','-i',before['web']['Id'],'python3','-',stdin=spec['container_program'].encode())
    after=containers()
    # Compare full Docker snapshots: identities, start times, env, command,
    # mounts and network changes invalidate the observation.
    if before!=after:
        raise ValueError('Runtime changed')
    result=json.loads(output)
    if not isinstance(result,dict) or result.get('type')!='complete':
        raise ValueError('Read incomplete')
    print(prefix+json.dumps(result),flush=True)
except Exception:
    print(prefix+json.dumps({'type':'complete','unavailable':True}),flush=True)
"""


def read_odoo_testing_usage(
    *,
    target: DokployTargetRecord,
    target_id: DokployTargetIdRecord,
    control_plane_root: Path,
    database_url: str,
) -> OdooTestingUsage:
    if target.instance != "testing" or target.target_type != "compose":
        return OdooTestingUsage()
    try:
        host, token = source.read_dokploy_config(
            control_plane_root=control_plane_root, database_url=database_url
        )
        provider = api.fetch_dokploy_target_payload(
            host=host, token=token, target_type="compose", target_id=target_id.target_id
        )
        if str(provider.get("name") or "") != target.target_name:
            return OdooTestingUsage()
        project = str(provider.get("appName") or "")
        if not project:
            return OdooTestingUsage()
        frames: list[dict[str, object]] = []
        _read_remote(
            host=host,
            token=token,
            server_id=str(provider.get("serverId") or ""),
            program=ODOO_TESTING_USAGE_HOST_PROGRAM,
            payload={
                "project": project,
                "context": target.context,
                "container_program": ODOO_TESTING_USAGE_CONTAINER_PROGRAM,
            },
            timeout_seconds=90,
            receive=frames.append,
        )
        current = api.fetch_dokploy_target_payload(
            host=host, token=token, target_type="compose", target_id=target_id.target_id
        )
        if any(current.get(key) != provider.get(key) for key in ("name", "appName", "serverId")):
            return OdooTestingUsage()
        if (
            len(frames) != 1
            or set(frames[0]) != {"type", "recent_signins", "authenticated_sessions"}
            or frames[0]["type"] != "complete"
        ):
            return OdooTestingUsage()
        recent, sessions = frames[0]["recent_signins"], frames[0]["authenticated_sessions"]
        if type(recent) is not bool or type(sessions) is not bool:
            return OdooTestingUsage()
        return OdooTestingUsage("in_use" if recent or sessions else "unused", recent, sessions)
    except NativeSourceAuthorizationDenied:
        raise
    except api.DokployRequestFailed as error:
        if error.status_code in {401, 403}:
            raise NativeSourceAuthorizationDenied("The provider refused the usage read.") from None
        return OdooTestingUsage()
    except (ValueError, OSError, RuntimeError, click.ClickException):
        return OdooTestingUsage()
