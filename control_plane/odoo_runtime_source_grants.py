"""Fixed, value-free source-grant read inside the selected testing web container."""

import json
from pathlib import Path
from typing import cast

from control_plane import odoo_runtime_reads as reads
from control_plane.contracts.runtime_identity import (
    RUNTIME_IDENTITY_ENV_KEY,
    parse_runtime_identity_payload,
)
from control_plane.dokploy.compose import ODOO_WEB_CONFIG_PATH
from control_plane.dokploy.native_copy_stream import _read_remote
from control_plane.odoo_source_probe import GrantStatus

# These programs receive only runtime identity, a source allowlist and fixed
# code. Credentials are consumed where already installed, never sent to Docker
# command arguments or back through the provider terminal.
CONTAINER_PROGRAM = r"""
import configparser, hmac, json, os, subprocess, sys
import psycopg2
spec=json.loads(sys.stdin.read(65537))
prefixes=__PREFIXES__
fields=__FIELDS__

def parameters():
    config=configparser.ConfigParser(interpolation=None)
    if config.read(__CONFIG_PATH__)!=[__CONFIG_PATH__]:
        raise ValueError()
    options=config['options']
    database=os.environ['ODOO_DB_NAME']
    if options.get('db_name')!=database or options.get('db_host')!='database':
        raise ValueError()
    keys=[prefix+'.'+field for prefix in prefixes.values() for field in (*fields,'password')]
    connection=psycopg2.connect(
        host='database', port=options.get('db_port') or '5432',
        user=options['db_user'], password=options['db_password'], dbname=database,
        connect_timeout=3,
        options='-c default_transaction_read_only=on -c statement_timeout=3000',
    )
    try:
        with connection.cursor() as cursor:
            cursor.execute('SELECT key, value FROM ir_config_parameter WHERE key = ANY(%s)', (keys,))
            rows=cursor.fetchmany(len(keys)+1)
            if len(rows)>len(keys):
                raise ValueError()
            values={}
            for key,value in rows:
                if key not in keys or key in values or not isinstance(value,str):
                    raise ValueError()
                values[key]=value
        connection.rollback()
        return values
    finally:
        connection.close()

def matches(field,left,right):
    if field=='ssl_verify':
        return left.lower() in {'true','1'} and right.lower() in {'true','1'}
    return bool(left) and hmac.compare_digest(left.encode(),right.encode())

def run():
    if (os.environ.get('PLATFORM_INSTANCE')!='testing'
        or json.loads(os.environ[__IDENTITY_KEY__])!=spec['identity']):
        raise ValueError()
    before=parameters()
    results={}
    for name in spec['sources']:
        results[name]='unknown'
        prefix=prefixes[name]
        settings={field:before.get(prefix+'.'+field,'') for field in (*fields,'password')}
        expected={field:os.environ.get(__ENV_KEYS__[name][field],'') for field in settings}
        if not all(matches(field,settings[field],expected[field]) for field in settings):
            continue
        try:
            child=subprocess.run(
                [sys.executable,'-I','-c',__PROBE__],
                input=json.dumps({key:settings[key] for key in ('host','port','user','password')}),
                text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                env={key:os.environ[key] for key in ('SSL_CERT_FILE','SSL_CERT_DIR') if os.environ.get(key)},
                timeout=10, check=False,
            )
            if child.returncode==0 and len(child.stdout)<128:
                payload=json.loads(child.stdout)
                if payload in ({'grants':'read_only'},{'grants':'unsafe'}):
                    results[name]=payload['grants']
        except Exception:
            pass
    if parameters()!=before:
        return {name:'unknown' for name in spec['sources']}
    return results

try:
    result=run()
except Exception:
    result={name:'unknown' for name in spec['sources']}
print(json.dumps(result))
"""

HOST_PROGRAM = r"""
import base64,json,re,subprocess,sys
spec=json.loads(base64.b64decode(sys.argv[1],validate=True))
prefix=sys.argv[2]
result={name:'unknown' for name in spec['sources']}

def inspect():
    raw=subprocess.run(['docker','inspect','--type','container',spec['container']], capture_output=True, check=True, timeout=5)
    values=json.loads(raw.stdout)
    if len(values)!=1:
        raise ValueError()
    value=values[0]
    config=value['Config']
    labels=config['Labels']
    env=dict(item.split('=',1) for item in config['Env'] if '=' in item)
    if (not re.fullmatch(r'[0-9a-f]{12,64}',spec['container'])
        or not re.fullmatch(r'[0-9a-f]{64}',value['Id'])
        or not value['Id'].startswith(spec['container'])
        or value['State'].get('Running') is not True
        or config['Image']!=spec['identity']['image_reference']
        or labels.get('com.docker.compose.project')!=spec['project']
        or labels.get('com.docker.compose.service')!='web'
        or env.get('PLATFORM_INSTANCE')!='testing'
        or json.loads(env[__IDENTITY_KEY__])!=spec['identity']):
        raise ValueError()
    # Healthcheck logs change on every tick and carry no runtime authority.
    return {key:value.get(key) for key in ('Id','Image','Config','HostConfig','Mounts','NetworkSettings','RestartCount')} | {
        'State':{key:value['State'].get(key) for key in ('Running','StartedAt','Pid')}
    }

try:
    before=inspect()
    child=subprocess.run(
        ['docker','exec','-i',before['Id'],'timeout','--kill-after=2s','50s','python3','-I','-c',spec['container_program']],
        input=json.dumps({'identity':spec['identity'],'sources':spec['sources']}),
        text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=55, check=False,
    )
    after=inspect()
    # Inspect contains private values; it is used only for equality here.
    if before!=after or child.returncode!=0 or len(child.stdout)>512:
        raise ValueError()
    observed=json.loads(child.stdout)
    if (not isinstance(observed,dict) or set(observed)!=set(result)
        or any(value not in ('read_only','unsafe','unknown') for value in observed.values())):
        raise ValueError()
    result=observed
except Exception:
    pass
print(prefix+json.dumps({'type':'complete','sources':result}),flush=True)
""".replace("__IDENTITY_KEY__", repr(RUNTIME_IDENTITY_ENV_KEY))


def runtime_grants(
    connection: reads.OdooRuntimeConnection,
    sources: tuple[str, ...],
    *,
    prefixes: dict[str, str],
    fields: tuple[str, ...],
    environment_keys: dict[str, dict[str, str]],
    observed_identity: str,
) -> dict[str, GrantStatus]:
    unknown: dict[str, GrantStatus] = {name: "unknown" for name in sources}
    if not sources or connection.selection.identity.instance != "testing":
        return unknown
    program = CONTAINER_PROGRAM
    for key, value in {
        "__PREFIXES__": repr(prefixes),
        "__FIELDS__": repr(fields),
        "__ENV_KEYS__": repr(environment_keys),
        "__CONFIG_PATH__": repr(ODOO_WEB_CONFIG_PATH),
        "__IDENTITY_KEY__": repr(RUNTIME_IDENTITY_ENV_KEY),
        "__PROBE__": repr(Path(__file__).with_name("odoo_source_probe.py").read_text()),
    }.items():
        program = program.replace(key, value)
    frames: list[dict[str, object]] = []
    try:
        # Bind to the actual container payload after the canonical service
        # comparator has normalized defaults/aliases and excluded deployed_at.
        # The remote programs can then compare that immutable payload exactly.
        reads.require_runtime_identity(
            connection.selection.identity, parse_runtime_identity_payload(observed_identity)
        )
        _read_remote(
            host=connection.host,
            token=connection.token,
            server_id=connection.server_id,
            program=HOST_PROGRAM,
            payload={
                "project": connection.app_name,
                "container": connection.evidence.container_id,
                "identity": json.loads(observed_identity),
                "sources": sources,
                "container_program": program,
            },
            receive=frames.append,
            timeout_seconds=75,
        )
        if len(frames) != 1 or set(frames[0]) != {"type", "sources"}:
            return unknown
        observed = frames[0]["sources"]
        if (
            frames[0]["type"] != "complete"
            or not isinstance(observed, dict)
            or set(observed) != set(sources)
            or any(value not in ("read_only", "unsafe", "unknown") for value in observed.values())
        ):
            return unknown
        return cast(dict[str, GrantStatus], observed)
    except Exception:
        return unknown
