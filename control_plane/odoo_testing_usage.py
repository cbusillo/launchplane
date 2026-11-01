"""Value-free occupancy reads through the service's existing provider transport."""

from dataclasses import dataclass, field
import base64
import inspect
import hashlib
import json
from urllib.parse import parse_qsl, unquote, urlsplit
from pathlib import Path
from typing import Literal
import click
from websockets.exceptions import WebSocketException

from control_plane._odoo_copy_runtime_guard import COPY_RUNTIME_INSPECTION_LIBRARY
from control_plane.copy_readback_diagnostics import ReadbackPredicate
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.dokploy import api, source
from control_plane.dokploy.compose import (
    ODOO_DATABASE_IMAGE,
    ODOO_WEB_CONFIG_PATH,
    ODOO_WEB_START_COMMAND,
)
from control_plane.dokploy.native_copy_stream import _read_remote, NativeSourceAuthorizationDenied


# Public diagnostics are a closed vocabulary, never exception text or topology.
ODOO_USAGE_PROBE_REASON_CODES = frozenset(
    [
        "container_identity_changed",
        "container_inventory_unavailable",
        "container_probe_unavailable",
        "credential_inventory_unavailable",
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
        "serving_container_absent",
        "serving_container_stopped",
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
) | frozenset("resource_" + predicate.value for predicate in ReadbackPredicate)
ODOO_USAGE_REASON_CODES = ODOO_USAGE_PROBE_REASON_CODES | frozenset(
    [
        "complete",
        "diagnostics_unavailable",
        "diagnostics_unrecognized",
        "not_collected",
        "never_served",
        "never_served_records_incomplete",
        "never_served_protection_seen",
        "never_served_not_attempted",
        "never_served_diagnostics_unavailable",
        "provider_history_unavailable",
        "serving_history_present",
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
    never_served_candidate: bool = False
    resources_qualified: bool = False
    credential_hashes: tuple[str, ...] = field(default=(), repr=False)

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
ODOO_USAGE_CONTAINER_PROGRAM_TEMPLATE = r"""
import argparse, configparser, json, os, re, stat
EXPECTED_INSTANCE=__EXPECTED_INSTANCE__
from pathlib import Path
CONFIG_PATH=__CONFIG_PATH__
checkpoint='testing_environment_unavailable'

class UsageUnavailable(ValueError):
    pass


def odoo_process(process: list[bytes]) -> bool:
    return any(arg.rsplit(b'/',1)[-1] in {b'odoo',b'odoo-bin',b'odoo-bin.source'}
               for arg in process[:2])


def serving_configured(process, options, database):
    arguments=[arg.decode() for arg in process]
    if not arguments:
        return False
    if arguments[0].rsplit('/',1)[-1].startswith('python'):
        arguments=arguments[1:]
    if not arguments or not (arguments[0].rsplit('/',1)[-1] in {'odoo','odoo-bin'}
                             or arguments[0]=='/odoo/odoo-bin.source'):
        return False
    arguments=arguments[1:]
    if arguments and arguments[0]=='gevent':
        arguments=arguments[1:]
    parser=argparse.ArgumentParser(add_help=False,allow_abbrev=False,exit_on_error=False)
    parser.add_argument('-c','--config')
    parser.add_argument('-d','--database')
    parser.add_argument('--data-dir')
    parser.add_argument('--load')
    parser.add_argument('--addons-path')
    for name in ('db_host','db_port','db_user','db-filter'):
        parser.add_argument('--'+name)
    try:
        args,unknown=parser.parse_known_args(arguments)
    except (argparse.ArgumentError,SystemExit):
        return False
    # Reject unknown switches, especially session-directory/config overrides.
    return (not unknown and args.config==CONFIG_PATH and args.database==database
            and (args.data_dir is None or args.data_dir==options.get('data_dir'))
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
    if os.environ.get('PLATFORM_INSTANCE')!=EXPECTED_INSTANCE:
        raise UsageUnavailable('unsupported_lane')
    database=os.environ['ODOO_DB_NAME']
    checkpoint='process_inventory_unavailable'
    processes=[[a for a in p.read_bytes().split(b'\0') if a]
               for p in Path('/proc').glob('[0-9]*/cmdline')]
    odoo_processes=[p for p in processes if odoo_process(p)]
    if not odoo_processes:
        raise UsageUnavailable('serving_process_absent')
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


def odoo_usage_container_program(instance: str) -> str:
    if instance not in {"testing", "preview"}:
        raise ValueError("unsupported_lane")
    return ODOO_USAGE_CONTAINER_PROGRAM_TEMPLATE.replace("__EXPECTED_INSTANCE__", repr(instance))


ODOO_TESTING_USAGE_CONTAINER_PROGRAM = odoo_usage_container_program("testing")


MAX_CREDENTIAL_CANDIDATES = 4096


def credential_digest(nonce: str, value: str) -> str:
    return hashlib.sha256((nonce + value).encode()).hexdigest()


def credential_child_name(parent: str, child: str) -> str:
    return f"{parent}.{child}" if parent else child


def credential_scalar_text(value: bool | int | float) -> tuple[str, str]:
    return str(value), json.dumps(value)


def credential_candidates(values: dict[str, str]) -> set[tuple[str, str]]:
    candidates: set[tuple[str, str]] = set()
    pending: list[tuple[str, object]] = list(values.items())
    while pending:
        name, value = pending.pop()
        if isinstance(value, dict):
            setting = value.get("key") or value.get("setting")
            literal = value.get("value")
            if isinstance(setting, str) and isinstance(literal, dict):
                pending.append((credential_child_name(name, setting), literal.get("value")))
            else:
                pending.extend(
                    (credential_child_name(name, key), item) for key, item in value.items()
                )
            continue
        if isinstance(value, list):
            pending.extend((name, item) for item in value)
            continue
        if isinstance(value, (bool, int, float)):
            scalar, encoded_scalar = credential_scalar_text(value)
            if encoded_scalar != scalar:
                pending.append((name, encoded_scalar))
            value = scalar
        if not isinstance(value, str) or not value or (name, value) in candidates:
            continue
        if len(value) > 1024 * 1024:
            raise ValueError("credential_inventory_unavailable")
        candidates.add((name, value))
        try:
            address = urlsplit(value)
        except ValueError:
            address = urlsplit("")
        password = address.password
        if password:
            pending.append((credential_child_name(name, "password"), unquote(password)))
        pending.extend(
            (credential_child_name(name, str(key)), item) for key, item in parse_qsl(address.query)
        )
        for encoded in (False, True):
            try:
                pending.append(
                    (name, json.loads(base64.b64decode(value, validate=True) if encoded else value))
                )
            except (ValueError, UnicodeError):
                continue
    return candidates


ODOO_TESTING_USAGE_HOST_PROGRAM = (
    COPY_RUNTIME_INSPECTION_LIBRARY
    + f"MAX_CREDENTIAL_CANDIDATES={MAX_CREDENTIAL_CANDIDATES}\n"
    + inspect.getsource(credential_digest)
    + inspect.getsource(credential_child_name)
    + inspect.getsource(credential_scalar_text)
    + inspect.getsource(credential_candidates)
    + r"""
import base64,hashlib,json,subprocess,sys
from urllib.parse import parse_qsl,unquote,urlsplit
spec=json.loads(base64.b64decode(sys.argv[1],validate=True))
prefix=sys.argv[2]
START_COMMAND=__START_COMMAND__
DATABASE_IMAGE=__DATABASE_IMAGE__
checkpoint='container_inventory_unavailable'

class UsageUnavailable(ValueError):
    pass


def docker(*args,stdin=None):
    return subprocess.run(['docker',*args],input=stdin,capture_output=True,check=True,timeout=25).stdout

def containers():
    ids=docker('ps','-aq','--filter','label=com.docker.compose.project='+spec['project']).decode().split()
    if len(ids)>16:
        raise UsageUnavailable('container_inventory_unavailable')
    if not ids:
        return {}
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
        if service in services or type(value['State'].get('Running')) is not bool:
            raise UsageUnavailable('service_ambiguous_or_stopped')
        if service=='database' and value['State']['Running'] is not True:
            raise UsageUnavailable('service_ambiguous_or_stopped')
        services[service]=value
    if set(services)!={'web','database'}:
        raise UsageUnavailable('service_unavailable')
    web=services['web']
    env=dict(item.split('=',1) for item in web['Config']['Env'] if '=' in item)
    if env.get('PLATFORM_CONTEXT')!=spec['context'] or env.get('PLATFORM_INSTANCE')!=spec.get('instance','testing') or env.get('ODOO_DB_HOST')!='database':
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
                 'Running':value['State']['Running'],
                 'StartedAt':value['State']['StartedAt']} for key,value in services.items()}

def one_inspection(identifier):
    try:
        rows=json.loads(docker('inspect',identifier))
    except subprocess.CalledProcessError:
        raise UsageUnavailable('container_inventory_unavailable')
    if not isinstance(rows,list) or len(rows)!=1:
        raise UsageUnavailable('container_inventory_unavailable')
    return rows[0]

def preview_resources():
    resource_spec=spec['preview_spec']
    inspected=inspect_copy_runtime(resource_spec,expected_instance='preview')
    database_id,database=inspected['database']
    if database['Config']['Image']!=DATABASE_IMAGE:
        raise CopyReadbackRefused(ReadbackPredicate.IMAGE)
    candidates=set()
    snapshots={}
    for service,(_,container) in inspected.items():
        environment=dict(item.split('=',1) for item in container['Config']['Env'] if '=' in item)
        try:
            candidates.update(value for _,value in credential_candidates(environment))
            if len(candidates)>MAX_CREDENTIAL_CANDIDATES:
                raise ValueError('credential_inventory_unavailable')
        except ValueError:
            raise UsageUnavailable('credential_inventory_unavailable')
        if any(environment.get(key) for key in resource_spec['forbidden_credential_keys']):
            raise CopyReadbackRefused(ReadbackPredicate.CREDENTIALS)
        if service!='database' and json.loads(environment.get('LAUNCHPLANE_RUNTIME_IDENTITY_JSON','null'))!=resource_spec['runtime_identity']:
            raise CopyReadbackRefused(ReadbackPredicate.RUNTIME_IDENTITY)
        snapshots[service]={key:container.get(key) for key in ('Id','Image','Config','Mounts','NetworkSettings','HostConfig')}
        snapshots[service]['State']={key:container['State'].get(key) for key in ('Running','StartedAt','FinishedAt','Restarting','Paused')}
    sql="BEGIN READ ONLY; SET LOCAL statement_timeout=10000; SELECT COALESCE(json_agg(json_build_array(key,value)), '[]'::json) FROM ir_config_parameter; ROLLBACK;"
    try:
        output=docker('exec','-i',database_id,'sh','-c','psql -X -q -t -A -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$1"','_',resource_spec['database_name'],stdin=sql.encode())
        parameters=json.loads(output)
        if not isinstance(parameters,list) or any(not isinstance(row,list) or len(row)!=2 or not all(isinstance(value,str) for value in row) for row in parameters):
            raise ValueError('credential_inventory_unavailable')
        candidates.update(value for _,value in credential_candidates(dict(parameters)))
        if len(candidates)>MAX_CREDENTIAL_CANDIDATES:
            raise ValueError('credential_inventory_unavailable')
    except subprocess.SubprocessError:
        raise CopyReadbackRefused(ReadbackPredicate.DATABASE_READ)
    except ValueError:
        raise UsageUnavailable('credential_inventory_unavailable')
    hashes=sorted(credential_digest(resource_spec['credential_nonce'],value) for value in candidates)
    return snapshots,hashes

try:
    checkpoint='resource_resources_qualified' if spec.get('preview_spec') else checkpoint
    resources_before=preview_resources() if spec.get('preview_spec') else None
    checkpoint='container_inventory_unavailable'
    before=containers()
    checkpoint='container_probe_unavailable'
    if before and before['web']['Running'] is False:
        # This is current serving availability, never a negative login/session
        # assertion. Both complete snapshots must still agree before publication.
        output=json.dumps({'type':'complete','unavailable':True,'reason_code':'serving_container_stopped'})
    elif before:
        output=docker('exec','-i',before['web']['Id'],'python3','-',stdin=spec['container_program'].encode())
    else:
        output=json.dumps({'type':'complete','unavailable':True,'reason_code':'serving_container_absent'})
    checkpoint='container_inventory_unavailable'
    after=containers()
    # Compare full Docker snapshots: identities, start times, env, command,
    # mounts and network changes invalidate the observation.
    if before!=after:
        raise UsageUnavailable('runtime_changed')
    if resources_before is not None and before and before['web']['Running']:
        resource_spec=spec['preview_spec']
        checkpoint='resource_integrations_isolated'
        isolation=docker('exec','-i',before['web']['Id'],'python3','-',resource_spec['database_name'],resource_spec['integration_spec'],stdin=resource_spec['integration_program'].encode())
        if b'integration_readback_ok=true' not in isolation.splitlines():
            raise UsageUnavailable(checkpoint)
        checkpoint='resource_resources_qualified'
        if resources_before!=preview_resources() or before!=containers():
            raise UsageUnavailable('runtime_changed')
    checkpoint='probe_result_invalid'
    result=json.loads(output)
    if not isinstance(result,dict) or result.get('type')!='complete':
        raise UsageUnavailable('probe_result_invalid')
    if resources_before is not None and 'unavailable' not in result:
        result['resources_qualified']=True
        result['credential_hashes']=resources_before[1]
    print(prefix+json.dumps(result),flush=True)
except CopyReadbackRefused as error:
    print(prefix+json.dumps({'type':'complete','unavailable':True,'reason_code':'resource_'+error.predicate}),flush=True)
except UsageUnavailable as error:
    print(prefix+json.dumps({'type':'complete','unavailable':True,'reason_code':str(error)}),flush=True)
except Exception:
    print(prefix+json.dumps({'type':'complete','unavailable':True,'reason_code':checkpoint}),flush=True)
""".replace("__START_COMMAND__", repr(ODOO_WEB_START_COMMAND)).replace(
        "__DATABASE_IMAGE__", repr(ODOO_DATABASE_IMAGE)
    )
)


def read_odoo_testing_usage(
    *,
    target: DokployTargetRecord,
    target_id: DokployTargetIdRecord,
    control_plane_root: Path,
    database_url: str,
    preview_spec: dict[str, object] | None = None,
) -> OdooTestingUsage:
    if target.target_type != "compose" or (preview_spec is None and target.instance != "testing"):
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
                "instance": "preview" if preview_spec is not None else "testing",
                "container_program": odoo_usage_container_program(
                    "preview" if preview_spec is not None else "testing"
                ),
                **(
                    {
                        "preview_spec": {
                            **preview_spec,
                            "compose_project": project,
                            "generation": {"instance": "preview", "namespace": project},
                        }
                    }
                    if preview_spec is not None
                    else {}
                ),
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
                if reason in {"serving_process_absent", "serving_container_absent"}:
                    checkpoint = "provider_history_unavailable"
                    if api.list_deployments_for_target(
                        host=host,
                        token=token,
                        target_type="compose",
                        target_id=target_id.target_id,
                        require_complete=True,
                    ):
                        return OdooTestingUsage(reason_code="serving_history_present")
                    return OdooTestingUsage(reason_code=reason, never_served_candidate=True)
                return OdooTestingUsage(reason_code=reason)
        if (
            len(frames) != 1
            or set(frames[0])
            != {"type", "recent_signins", "authenticated_sessions"}
            | ({"resources_qualified", "credential_hashes"} if preview_spec is not None else set())
            or frames[0]["type"] != "complete"
            or preview_spec is not None
            and frames[0].get("resources_qualified") is not True
        ):
            return OdooTestingUsage(reason_code="probe_result_invalid")
        recent, sessions = frames[0]["recent_signins"], frames[0]["authenticated_sessions"]
        if type(recent) is not bool or type(sessions) is not bool:
            return OdooTestingUsage(reason_code="probe_result_invalid")
        hashes = frames[0].get("credential_hashes", [])
        if not isinstance(hashes, list):
            return OdooTestingUsage(reason_code="probe_result_invalid")
        if preview_spec is not None and (
            not 0 < len(hashes) <= MAX_CREDENTIAL_CANDIDATES
            or any(
                not isinstance(value, str)
                or len(value) != 64
                or any(character not in "0123456789abcdef" for character in value)
                for value in hashes
            )
        ):
            return OdooTestingUsage(reason_code="probe_result_invalid")
        return OdooTestingUsage(
            "in_use" if recent or sessions else "unused",
            recent,
            sessions,
            "complete",
            resources_qualified=preview_spec is not None,
            credential_hashes=tuple(hashes),
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
