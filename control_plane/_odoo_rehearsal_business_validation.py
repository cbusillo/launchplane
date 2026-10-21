"""Fixed image entrypoint and value-free receipt checks for the isolated runner."""

BUSINESS_VALIDATION_ENTRYPOINT = "/volumes/scripts/launchplane_business_validation.py"
BUSINESS_VALIDATION_RECEIPT_MAX_BYTES = 8192

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
