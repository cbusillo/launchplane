"""Value-free occupancy reads through the service's existing provider transport."""

from dataclasses import dataclass
from pathlib import Path
from typing import Literal
import click
from websockets.exceptions import WebSocketException

from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.dokploy import api, source
from control_plane.dokploy.compose import ODOO_WEB_CONFIG_PATH, ODOO_WEB_START_COMMAND
from control_plane.dokploy.native_copy_stream import _read_remote, NativeSourceAuthorizationDenied


# Public diagnostics are a closed vocabulary, never exception text or topology.
ODOO_USAGE_PROBE_REASON_CODES = frozenset(
    [
        "container_identity_changed",
        "container_inventory_unavailable",
        "container_probe_unavailable",
        "database_cleanup_unavailable",
        "database_connection_unavailable",
        "database_driver_unavailable",
        "database_network_unavailable",
        "login_history_unavailable",
        "probe_result_invalid",
        "process_inventory_unavailable",
        "runtime_changed",
        "service_ambiguous_or_stopped",
        "service_unavailable",
        "serving_configuration_mismatch",
        "serving_process_absent",
        "session_alias_unsupported",
        "session_file_unavailable",
        "session_file_unreadable",
        "session_inventory_exceeds_bound",
        "session_layout_unsupported",
        "session_location_unavailable",
        "session_store_unavailable",
        "testing_environment_unavailable",
        "testing_identity_mismatch",
        "unsupported_lane",
        "web_command_unsupported",
        "web_configuration_unavailable",
        "web_database_mismatch",
    ]
)
ODOO_USAGE_REASON_CODES = ODOO_USAGE_PROBE_REASON_CODES | frozenset(
    [
        "complete",
        "diagnostics_unavailable",
        "diagnostics_unrecognized",
        "not_collected",
        "never_served",
        "never_served_records_incomplete",
        "never_served_protection_seen",
        "provider_config_unavailable",
        "provider_target_changed",
        "provider_target_unavailable",
        "provider_transport_unavailable",
        "staff_testing_hold",
        "target_alias",
        "target_unavailable",
    ]
)


@dataclass(frozen=True)
class OdooTestingUsage:
    usage: Literal["unknown", "unused", "in_use"] = "unknown"
    recent_signins: bool | None = None
    authenticated_sessions: bool | None = None
    reason_code: str = "not_collected"

    proof_sha256: str = ""

    def payload(self) -> dict[str, object]:
        return {
            "recent_signins": self.recent_signins,
            "authenticated_sessions": self.authenticated_sessions,
            "reason_code": (
                self.reason_code
                if self.reason_code in ODOO_USAGE_REASON_CODES
                else "probe_result_invalid"
            ),
        }


# Runs inside the existing web container. No ORM, registry, HTTP authentication,
# session-store get/save/GC, account names, tokens or session IDs leave it.
ODOO_TESTING_USAGE_CONTAINER_PROGRAM = r"""
import argparse, configparser, json, os, re, stat
from pathlib import Path
CONFIG_PATH=__CONFIG_PATH__
checkpoint='testing_environment_unavailable'

class UsageUnavailable(ValueError):
    pass


def serving_configured(process, options, database):
    arguments=[arg.decode() for arg in process]
    if not arguments:
        return False
    if arguments[0].rsplit('/',1)[-1].startswith('python'):
        arguments=arguments[1:]
    if not arguments or arguments[0].rsplit('/',1)[-1] not in {'odoo','odoo-bin'}:
        return False
    parser=argparse.ArgumentParser(add_help=False,allow_abbrev=False,exit_on_error=False)
    parser.add_argument('-c','--config')
    parser.add_argument('-d','--database')
    for name in ('db_host','db_port','db_user','db-filter'):
        parser.add_argument('--'+name)
    try:
        args,unknown=parser.parse_known_args(arguments[1:])
    except (argparse.ArgumentError,SystemExit):
        return False
    # Reject unknown switches, especially session-directory/config overrides.
    return (not unknown and args.config==CONFIG_PATH and args.database==database
            and args.db_host==options['db_host']
            and args.db_port==options.get('db_port','5432')
            and args.db_user==options['db_user']
            and args.db_filter=='^'+re.escape(database)+'$')

def read_sessions(path, database):
    global checkpoint
    checkpoint='session_store_unavailable'
    # Count every authenticated or partially authenticated file, regardless of
    # age. A database/device row is not proof that a browser session has ended.
    # Bound traversal and bytes; truncation or an unreadable file is unknown.
    if path.is_symlink() or not path.is_dir():
        raise UsageUnavailable('session_store_unavailable')
    pending=[path]
    visited=0
    used=False
    while pending:
        checkpoint='session_store_unavailable'
        current=pending.pop()
        for entry in current.iterdir():
            checkpoint='session_file_unavailable'
            visited+=1
            if visited>20000:
                raise UsageUnavailable('session_inventory_exceeds_bound')
            info=entry.lstat()
            if stat.S_ISLNK(info.st_mode):
                raise UsageUnavailable('session_alias_unsupported')
            if stat.S_ISDIR(info.st_mode):
                if current!=path:
                    raise UsageUnavailable('session_layout_unsupported')
                pending.append(entry)
                continue
            if not stat.S_ISREG(info.st_mode) or info.st_size>1024*1024:
                raise UsageUnavailable('session_file_unavailable')
            # Include legacy flat files and in-progress writes. Incomplete JSON
            # cannot establish absence. Never deserialize executable pickle data.
            checkpoint='session_file_unreadable'
            with entry.open(encoding='utf-8') as handle:
                data=json.load(handle)
            if not isinstance(data,dict):
                raise UsageUnavailable('session_file_unreadable')
            if data.get('db')==database and (data.get('uid') or data.get('pre_uid')):
                used=True
    return used

def probe():
    global checkpoint
    checkpoint='database_driver_unavailable'
    import psycopg2
    checkpoint='testing_environment_unavailable'
    if os.environ.get('PLATFORM_INSTANCE')!='testing':
        raise UsageUnavailable('unsupported_lane')
    database=os.environ['ODOO_DB_NAME']
    checkpoint='web_configuration_unavailable'
    config=configparser.ConfigParser(interpolation=None)
    if config.read(CONFIG_PATH)!=[CONFIG_PATH]:
        raise UsageUnavailable('web_configuration_unavailable')
    options=config['options']
    if any(key not in options for key in ('db_user','db_password')):
        raise UsageUnavailable('web_configuration_unavailable')
    if options.get('db_name')!=database or options.get('db_host')!='database':
        raise UsageUnavailable('web_database_mismatch')
    checkpoint='session_location_unavailable'
    data_dir=Path(options['data_dir'])
    if not data_dir.is_absolute():
        raise UsageUnavailable('session_location_unavailable')
    # The supported startup hands this configuration to Odoo. A waiting gate,
    # custom command or another config cannot qualify this read.
    checkpoint='process_inventory_unavailable'
    processes=[[a for a in p.read_bytes().split(b'\0') if a]
               for p in Path('/proc').glob('[0-9]*/cmdline')]
    odoo_processes=[p for p in processes if any(
        arg.rsplit(b'/',1)[-1] in {b'odoo',b'odoo-bin'} for arg in p[:2])]
    if not odoo_processes:
        raise UsageUnavailable('serving_process_absent')
    if not all(serving_configured(p,options,database) for p in odoo_processes):
        raise UsageUnavailable('serving_configuration_mismatch')
    session_use=read_sessions(data_dir/'sessions',database)
    checkpoint='database_connection_unavailable'
    connection=psycopg2.connect(
        host='database',port=options.get('db_port') or '5432',
        user=options['db_user'],password=options['db_password'],
        dbname=database,connect_timeout=5,
        options='-c default_transaction_read_only=on -c statement_timeout=10000',
    )
    try:
        checkpoint='login_history_unavailable'
        with connection.cursor() as cursor:
            cursor.execute('SET TRANSACTION READ ONLY')
            # Include all accounts, including portal/admin/automation. Restored
            # production history may conservatively protect a lane too.
            cursor.execute("SELECT EXISTS (SELECT 1 FROM res_users_log WHERE create_date >= (CURRENT_TIMESTAMP AT TIME ZONE 'UTC') - interval '24 hours')")
            recent=cursor.fetchone()[0]
            if type(recent) is not bool:
                raise UsageUnavailable('login_history_unavailable')
        checkpoint='database_cleanup_unavailable'
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
    except UsageUnavailable as error:
        result={'type':'complete','unavailable':True,'reason_code':str(error)}
    except Exception:
        result={'type':'complete','unavailable':True,'reason_code':checkpoint}
    print(json.dumps(result),flush=True)
""".replace("__CONFIG_PATH__", repr(ODOO_WEB_CONFIG_PATH))


ODOO_TESTING_USAGE_HOST_PROGRAM = r"""
import base64,json,subprocess,sys
spec=json.loads(base64.b64decode(sys.argv[1],validate=True))
prefix=sys.argv[2]
START_COMMAND=__START_COMMAND__
checkpoint='container_inventory_unavailable'

class UsageUnavailable(ValueError):
    pass


def docker(*args,stdin=None):
    return subprocess.run(['docker',*args],input=stdin,capture_output=True,check=True,timeout=25).stdout

def containers():
    ids=docker('ps','-aq','--filter','label=com.docker.compose.project='+spec['project']).decode().split()
    if not ids or len(ids)>16:
        raise UsageUnavailable('container_inventory_unavailable')
    values=json.loads(docker('inspect',*ids))
    if not isinstance(values,list) or len(values)!=len(ids):
        raise UsageUnavailable('container_inventory_unavailable')
    services={}
    for value in values:
        config=value['Config']
        labels=config['Labels']
        if labels.get('com.docker.compose.project')!=spec['project']:
            raise UsageUnavailable('container_identity_changed')
        service=labels.get('com.docker.compose.service')
        if service not in {'web','database'}:
            continue
        if service in services or value['State'].get('Running') is not True:
            raise UsageUnavailable('service_ambiguous_or_stopped')
        services[service]=value
    if set(services)!={'web','database'}:
        raise UsageUnavailable('service_unavailable')
    web=services['web']
    env=dict(item.split('=',1) for item in web['Config']['Env'] if '=' in item)
    if env.get('PLATFORM_CONTEXT')!=spec['context'] or env.get('PLATFORM_INSTANCE')!='testing' or env.get('ODOO_DB_HOST')!='database':
        raise UsageUnavailable('testing_identity_mismatch')
    # A custom startup may select a different config/session store.
    command=web['Config'].get('Cmd') or []
    if not any(START_COMMAND in arg for arg in command):
        raise UsageUnavailable('web_command_unsupported')
    networks=web['NetworkSettings']['Networks']
    db_networks=services['database']['NetworkSettings']['Networks']
    if not set(networks)&set(db_networks):
        raise UsageUnavailable('database_network_unavailable')
    return {key:{'Id':value['Id'],'Config':value['Config'],'Mounts':value['Mounts'],
                 'NetworkSettings':value['NetworkSettings'],
                 'StartedAt':value['State']['StartedAt']} for key,value in services.items()}

try:
    before=containers()
    checkpoint='container_probe_unavailable'
    output=docker('exec','-i',before['web']['Id'],'python3','-',stdin=spec['container_program'].encode())
    checkpoint='container_inventory_unavailable'
    after=containers()
    # Compare full Docker snapshots: identities, start times, env, command,
    # mounts and network changes invalidate the observation.
    if before!=after:
        raise UsageUnavailable('runtime_changed')
    checkpoint='probe_result_invalid'
    result=json.loads(output)
    if not isinstance(result,dict) or result.get('type')!='complete':
        raise UsageUnavailable('probe_result_invalid')
    print(prefix+json.dumps(result),flush=True)
except UsageUnavailable as error:
    print(prefix+json.dumps({'type':'complete','unavailable':True,'reason_code':str(error)}),flush=True)
except Exception:
    print(prefix+json.dumps({'type':'complete','unavailable':True,'reason_code':checkpoint}),flush=True)
""".replace("__START_COMMAND__", repr(ODOO_WEB_START_COMMAND))


def read_odoo_testing_usage(
    *,
    target: DokployTargetRecord,
    target_id: DokployTargetIdRecord,
    control_plane_root: Path,
    database_url: str,
) -> OdooTestingUsage:
    if target.instance != "testing" or target.target_type != "compose":
        return OdooTestingUsage(reason_code="unsupported_lane")
    checkpoint = "provider_config_unavailable"
    try:
        host, token = source.read_dokploy_config(
            control_plane_root=control_plane_root, database_url=database_url
        )
        checkpoint = "provider_target_unavailable"
        provider = api.fetch_dokploy_target_payload(
            host=host, token=token, target_type="compose", target_id=target_id.target_id
        )
        if not isinstance(provider, dict) or str(provider.get("name") or "") != target.target_name:
            return OdooTestingUsage(reason_code="provider_target_unavailable")
        project = str(provider.get("appName") or "")
        if not project:
            return OdooTestingUsage(reason_code="provider_target_unavailable")
        frames: list[dict[str, object]] = []
        checkpoint = "provider_transport_unavailable"
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
        checkpoint = "provider_target_unavailable"
        current = api.fetch_dokploy_target_payload(
            host=host, token=token, target_type="compose", target_id=target_id.target_id
        )
        if not isinstance(current, dict) or any(
            current.get(key) != provider.get(key) for key in ("name", "appName", "serverId")
        ):
            return OdooTestingUsage(reason_code="provider_target_changed")
        checkpoint = "probe_result_invalid"
        if (
            len(frames) == 1
            and isinstance(frames[0], dict)
            and set(frames[0]) == {"type", "unavailable", "reason_code"}
        ):
            frame = frames[0]
            reason = frame["reason_code"]
            if (
                frame["type"] == "complete"
                and frame["unavailable"] is True
                and isinstance(reason, str)
                and reason in ODOO_USAGE_PROBE_REASON_CODES
            ):
                return OdooTestingUsage(reason_code=reason)
        if (
            len(frames) != 1
            or set(frames[0]) != {"type", "recent_signins", "authenticated_sessions"}
            or frames[0]["type"] != "complete"
        ):
            return OdooTestingUsage(reason_code="probe_result_invalid")
        recent, sessions = frames[0]["recent_signins"], frames[0]["authenticated_sessions"]
        if type(recent) is not bool or type(sessions) is not bool:
            return OdooTestingUsage(reason_code="probe_result_invalid")
        return OdooTestingUsage(
            "in_use" if recent or sessions else "unused", recent, sessions, "complete"
        )
    except NativeSourceAuthorizationDenied:
        raise
    except api.DokployRequestFailed as error:
        if error.status_code in {401, 403}:
            raise NativeSourceAuthorizationDenied("The provider refused the usage read.") from None
        return OdooTestingUsage(reason_code=checkpoint)
    except (
        ValueError,
        OSError,
        RuntimeError,
        TypeError,
        AttributeError,
        WebSocketException,
        click.ClickException,
    ):
        return OdooTestingUsage(reason_code=checkpoint)
