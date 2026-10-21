"""Fixed image entrypoint and value-free receipt checks for the isolated runner."""

BUSINESS_VALIDATION_ENTRYPOINT = "/opt/project/addons/launchplane_business_validation.py"
BUSINESS_VALIDATION_RECEIPT_MAX_BYTES = 8192
BUSINESS_VALIDATION_COMMAND_TIMEOUT_SECONDS = 900
BUSINESS_VALIDATION_OUTPUT_MAX_BYTES = 2 * BUSINESS_VALIDATION_RECEIPT_MAX_BYTES + 1024

# The remote host has only the standard library. Keep this parser identical to
# the local validator rather than maintaining a second interpretation.
BUSINESS_VALIDATION_RECEIPT_LIBRARY = r"""
def checked_business_receipt(value, expected=None):
    if (not isinstance(value,dict)
            or set(value)!={'product','rehearsal_id','artifact_id','source_commit','database',
                'attempt_sha256','passed','committed','checks'}
            or len(json.dumps(value,separators=(',',':'),ensure_ascii=True).encode())>BUSINESS_VALIDATION_RECEIPT_MAX_BYTES):
        raise ValueError('Invalid product validation receipt')
    if expected is not None and any(value[key]!=item for key,item in expected.items()):
        raise ValueError('Product validation identity differs')
    if (type(value['passed']) is not bool or type(value['committed']) is not bool
            or not isinstance(value['checks'],list) or not 0<len(value['checks'])<=64):
        raise ValueError('Invalid product validation outcome')
    keys=set()
    for check in value['checks']:
        if (not isinstance(check,dict) or set(check)!={'key','passed','records'}
                or not isinstance(check['key'],str) or not re.fullmatch('[a-z][a-z0-9_]{0,39}',check['key'])
                or check['key'] in keys or type(check['passed']) is not bool
                or not isinstance(check['records'],list)):
            raise ValueError('Invalid product validation check')
        keys.add(check['key'])
        for record in check['records']:
            if (not isinstance(record,dict) or set(record)!={'model','id'}
                    or not isinstance(record['model'],str)
                    or not re.fullmatch(r'[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+',record['model'])
                    or type(record['id']) is not int or record['id']<1):
                raise ValueError('Invalid product validation record')
        if (value['passed'] and (not check['passed'] or not check['records'])
                or not value['committed'] and check['records']):
            raise ValueError('Product validation records differ from commit outcome')
    if value['passed'] and not value['committed']:
        raise ValueError('Passing validation requires committed writes')
    return value
"""


def checked_business_receipt(value: object) -> object:
    import json
    import re

    namespace = {
        "json": json,
        "re": re,
        "BUSINESS_VALIDATION_RECEIPT_MAX_BYTES": BUSINESS_VALIDATION_RECEIPT_MAX_BYTES,
    }
    exec(BUSINESS_VALIDATION_RECEIPT_LIBRARY, namespace)
    return namespace["checked_business_receipt"](value)  # type: ignore[operator]


BUSINESS_ENTRYPOINT_CHECK_LIBRARY = r"""
import json
from pathlib import Path
import re
import subprocess
import tempfile


def check_business_entrypoint(spec):
    status='provider_unavailable'
    def run(arguments):
        return subprocess.run(arguments,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
            timeout=__COMMAND_TIMEOUT__,check=False).returncode
    try:
        if run(['docker','container','inspect',spec['container_name']])==0:
            return {**spec['binding'],'status':status}
        if run(['docker','image','inspect',spec['image']]) and run(['docker','pull',spec['image']]):
            return {**spec['binding'],'status':status}
        with tempfile.TemporaryDirectory(prefix='lp-entrypoint-') as temporary:
            cid=Path(temporary)/'container-id'
            try:
                code=run(['docker','run','--pull','never','--name',spec['container_name'],
                    '--cidfile',str(cid),'--network','none','--read-only','--cap-drop','ALL',
                    '--security-opt','no-new-privileges','--entrypoint','python3',spec['image'],
                    spec['entrypoint'],'--check'])
                status='available' if code==0 else 'provider_unavailable' if code in {125,126,127} else 'entrypoint_unavailable'
            finally:
                if cid.exists():
                    identifier=cid.read_text().strip()
                    if not re.fullmatch('[a-f0-9]{64}',identifier):
                        status='provider_unavailable'
                    else:
                        cleaned=subprocess.run(['docker','rm','-f',identifier],stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,timeout=30,check=False).returncode
                        if cleaned:
                            status='provider_unavailable'
                else:
                    status='provider_unavailable'
    except (OSError,ValueError,subprocess.TimeoutExpired):
        status='provider_unavailable'
    return {**spec['binding'],'status':status}
""".replace("__COMMAND_TIMEOUT__", str(BUSINESS_VALIDATION_COMMAND_TIMEOUT_SECONDS))


def business_entrypoint_check_script(image: str, rehearsal_id: str, binding: dict[str, str]) -> str:
    import json

    spec = {
        "image": image,
        "container_name": rehearsal_id + "-entrypoint-check",
        "entrypoint": BUSINESS_VALIDATION_ENTRYPOINT,
        "binding": binding,
    }
    return (
        "#!/usr/bin/env bash\nset -euo pipefail\npython3 - <<'LP_ENTRYPOINT_CHECK'\n"
        + BUSINESS_ENTRYPOINT_CHECK_LIBRARY
        + "\nspec=json.loads("
        + repr(json.dumps(spec))
        + ")\n"
        + "print('rehearsal_result='+json.dumps(check_business_entrypoint(spec)).encode().hex(),flush=True)\n"
        + "LP_ENTRYPOINT_CHECK\n"
    )


def final_business_database(rehearsal_id: str) -> str:
    return rehearsal_id.replace("-", "_") + "_final_upgrade"
