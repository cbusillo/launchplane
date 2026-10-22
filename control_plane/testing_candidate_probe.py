"""Fixed private readiness probe; application passwords never leave the host."""

import inspect

from control_plane._odoo_copy_runtime_guard import COPY_RUNTIME_INSPECTION_LIBRARY
from control_plane.docker_errors import docker_daemon_authorization_denied

CANDIDATE_CONTAINER_PROBE = r"""
import configparser, hmac, json, os, sys
from urllib.request import Request, build_opener, HTTPRedirectHandler, ProxyHandler
from urllib.parse import urlsplit
spec = json.load(sys.stdin)
class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None
opener = build_opener(ProxyHandler({}), NoRedirect())
port = spec['http_port']
if type(port) is not int or not 1 <= port <= 65535:
    raise RuntimeError('Candidate HTTP port is invalid')
base = 'http://127.0.0.1:' + str(port)
headers = {'Host': urlsplit(spec['base_url']).netloc}
def request(path):
    with opener.open(Request(base + path, headers=headers), timeout=15) as response:
        if response.status != 200:
            raise RuntimeError('Candidate HTTP readiness failed')
        return json.load(response)
health = request('/launchplane/health')
actual = next((health.get(key) for key in ('runtime_identity','launchplane_runtime_identity','launchplaneRuntimeIdentity','LAUNCHPLANE_RUNTIME_IDENTITY_JSON') if health.get(key)), None)
if isinstance(actual, str):
    actual = json.loads(actual)
if not isinstance(actual, dict):
    raise RuntimeError('Candidate HTTP identity is missing')
actual.pop('deployed_at', None)
if actual != spec['runtime_identity']:
    raise RuntimeError('Candidate HTTP identity differs')
config = configparser.ConfigParser(interpolation=None)
if config.read(spec['web_config']) != [spec['web_config']]:
    raise RuntimeError('Candidate running config is unavailable')
options = config['options']
if (options.get('db_host') != 'database'
        or options.get('db_name') != os.environ['ODOO_DB_NAME']
        or options.get('db_user') != os.environ['ODOO_DB_USER']
        or not hmac.compare_digest(options.get('db_password', ''), os.environ['ODOO_DB_PASSWORD'])):
    raise RuntimeError('Candidate local database credential differs')
# Never authenticate a web session here: a readiness login would manufacture
# the sessions and login history that protect a lane from its next rebuild.
import psycopg2
from passlib.context import CryptContext
context = CryptContext(schemes=['pbkdf2_sha512'])
try:
    connection = psycopg2.connect(
        host='database', port=options.get('db_port') or '5432',
        dbname=options['db_name'], user=options['db_user'], password=options['db_password'],
        connect_timeout=5,
        options='-c default_transaction_read_only=on -c statement_timeout=10000',
    )
except psycopg2.Error:
    # Local candidate credentials are not the provider's read authority. Keep
    # driver stderr from masquerading as a host/provider authorization refusal.
    raise RuntimeError('Candidate local database verification failed') from None
try:
    with connection.cursor() as cursor:
        cursor.execute('SET TRANSACTION READ ONLY')
        cursor.execute('SELECT password FROM res_users WHERE login = %s AND active',
                       (os.environ.get('ODOO_ADMIN_LOGIN', '').strip() or 'admin',))
        rows = cursor.fetchall()
        if (len(rows) != 1 or not rows[0][0]
                or not context.verify(os.environ['ODOO_ADMIN_PASSWORD'], rows[0][0])):
            raise RuntimeError('Candidate local admin credential differs')
finally:
    try:
        connection.rollback()
    finally:
        connection.close()
# Supported startup renders the master value directly. This checks rendering
# consistency; it is not an interactive authentication or an admission digest.
if not hmac.compare_digest(os.environ['ODOO_MASTER_PASSWORD'], options['admin_passwd']):
    raise RuntimeError('Candidate local master credential differs')
print('candidate_credentials_verified=true')
"""

CANDIDATE_HOST_PROBE = (
    COPY_RUNTIME_INSPECTION_LIBRARY
    + "\n"
    + inspect.getsource(docker_daemon_authorization_denied)
    + r"""
import base64, hashlib, json, re, subprocess, sys
from pathlib import Path

def docker(*arguments, stdin=None):
    result = subprocess.run(['docker', *arguments], input=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60, check=False)
    if result.returncode:
        if docker_daemon_authorization_denied(result.stderr):
            raise PermissionError('Candidate runtime refused the operation')
        raise RuntimeError('Candidate runtime readback failed')
    return result.stdout

def one_inspection(identifier):
    values = json.loads(docker('inspect', identifier))
    if not isinstance(values, list) or len(values) != 1:
        raise RuntimeError('Candidate runtime inventory is incomplete')
    return values[0]

def probe(spec):
    containers = inspect_copy_runtime(spec, expected_instance='testing')
    if any(item[1].get('State', {}).get('Status') != 'running' for item in containers.values()):
        raise RuntimeError('Candidate containers are not running')
    runner = containers['script-runner'][0]
    web, observed = containers['web']
    labels = observed['Config'].get('Labels') or {}
    if labels.get('traefik.enable') != 'false' or any(key.startswith('traefik.http.routers.') for key in labels):
        raise RuntimeError('Candidate acquired a public router')
    namespace = spec['generation']['namespace']
    aliases = observed['NetworkSettings']['Networks']['dokploy-network'].get('Aliases')
    if not isinstance(aliases, list) or namespace not in aliases:
        raise RuntimeError('Candidate private endpoint is unavailable')
    for identifier in docker('ps','-aq').decode().splitlines():
        other = one_inspection(identifier)
        if other['Id'] == observed['Id']:
            continue
        if namespace in other.get('NetworkSettings',{}).get('Networks',{}).get('dokploy-network',{}).get('Aliases',[]):
            raise RuntimeError('Candidate private endpoint is ambiguous')
    if observed['Image'] != containers['script-runner'][1]['Image']:
        raise RuntimeError('Candidate web and runner images differ')
    image = json.loads(docker('image','inspect',observed['Image']))[0]
    if spec['image_reference'] not in image.get('RepoDigests', []):
        raise RuntimeError('Candidate installed image differs')
    for _, observed in containers.values():
        if observed.get('HostConfig', {}).get('AutoRemove'):
            raise RuntimeError('Candidate custody is transient')
    for service in ('web','script-runner'):
        environment = dict(item.split('=',1) for item in containers[service][1]['Config'].get('Env',[]) if '=' in item)
        identity = json.loads(environment.get('LAUNCHPLANE_RUNTIME_IDENTITY_JSON', 'null'))
        if not isinstance(identity, dict):
            raise RuntimeError('Candidate runtime identity missing')
        identity.pop('deployed_at', None)
        if identity != spec['runtime_identity']:
            raise RuntimeError('Candidate runtime identity differs')
        if environment.get('ODOO_INSTANCE_OVERRIDES_PAYLOAD_B64','') != spec['expected_overrides_payload']:
            raise RuntimeError('Candidate override intent differs')
    docker('exec', runner, 'python3','-c', "import hashlib,os,sys;from pathlib import Path;expected=hashlib.sha256((sys.argv[1]+'\\n'+os.environ.get('ODOO_INSTANCE_OVERRIDES_PAYLOAD_B64','')).encode()).hexdigest();assert Path(sys.argv[2]).read_text()==expected",spec['database_name'],spec['web_gate_path'])
    if spec['copy_binding_sha256']:
        actual = docker('exec','-i',containers['database'][0],'sh','-c','psql -X -q -t -A -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$1"','_',spec['database_name'],stdin=spec['witness_sql'].encode()).decode().strip()
        if actual != spec['copy_binding_sha256']:
            raise RuntimeError('Candidate copy witness differs')
    readback = docker('exec','-i',runner,'python3','-',spec['database_name'],spec['readback_spec'],stdin=base64.b64decode(spec['readback_program'])).decode()
    if 'integration_readback_ok=true' not in readback.splitlines():
        raise RuntimeError('Candidate connections remain configured')
    credentials = docker('exec','-i',web,'python3','-c',base64.b64decode(spec['credential_program']).decode(),stdin=json.dumps(spec).encode()).decode().splitlines()
    if credentials != ['candidate_credentials_verified=true']:
        raise RuntimeError('Candidate local credentials are unverified')
    return {'type':'complete','private_readiness_verified':True}

if __name__ == '__main__':
    try:
        spec = json.loads(base64.b64decode(sys.argv[1], validate=True))
        print(sys.argv[2] + json.dumps(probe(spec)), flush=True)
    except PermissionError:
        print(sys.argv[2] + json.dumps({'type':'denied'}), flush=True)
        sys.exit(1)
    except Exception as error:
        print(sys.argv[2] + json.dumps(copy_readback_failure_frame(error)), flush=True)
        sys.exit(1)
"""
)
