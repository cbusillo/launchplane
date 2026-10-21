"""Fixed ORM program. Product input is JSON data, never Python or a shell command."""

BUSINESS_VALIDATION_LIBRARY = r"""
def run_business_validation(env, plan):
    records = {}
    evidence = []

    def resolve(value):
        if isinstance(value, dict):
            if '$ref' in value:
                return records[value['$ref']].id
            return {key: resolve(item) for key, item in value.items()}
        if isinstance(value, list):
            return [resolve(item) for item in value]
        return value

    try:
        if env.cr.dbname != plan['database']:
            raise ValueError('Copy identity differs')
        for step in plan['steps']:
            passed = True
            if step['op'] == 'create':
                record = env[step['model']].with_context(
                    tracking_disable=True, mail_create_nosubscribe=True,
                    mail_notify_force_send=False).create(resolve(step['values']))
                records[step['key']] = record
            else:
                record = records[step['record']]
                if step['op'] == 'call':
                    getattr(record, step['method'])()
                else:
                    value = record[step['field']]
                    if hasattr(value, 'ids'):
                        value = value.ids
                    if step['comparison'] == 'truthy':
                        value = bool(value)
                    passed = value == step['expected']
            evidence.append({'key': step['key'], 'record_ids': record.ids, 'passed': passed})
            if not passed:
                env.cr.rollback()
                return {'passed': False, 'steps': evidence}
        env.flush_all()
        env.cr.commit()
        env.invalidate_all()
        for step in plan['steps']:
            if step['op'] == 'assert':
                value = records[step['record']][step['field']]
                if hasattr(value, 'ids'):
                    value = value.ids
                if step['comparison'] == 'truthy':
                    value = bool(value)
                if value != step['expected']:
                    return {'passed': False, 'steps': evidence}
        return {'passed': True, 'steps': evidence}
    except Exception:
        env.cr.rollback()
        # The caller retains only a closed failure, never exception text or
        # business values from the copied Client database.
        return {'passed': False, 'steps': evidence or [
            {'key': plan['steps'][0]['key'], 'record_ids': [], 'passed': False}]}
"""


def business_validation_runner(plan: dict[str, object]) -> str:
    """The image's existing runner supplies native Odoo shell configuration."""
    import json

    shell = (
        "import json\n"
        + BUSINESS_VALIDATION_LIBRARY
        + "\nplan=json.loads("
        + repr(json.dumps(plan))
        + ")\n"
        + "result=run_business_validation(env,plan)\n"
        + "print('business_validation_result='+json.dumps(result).encode().hex(),flush=True)\n"
    )
    return (
        "import sys,subprocess\nsys.path.insert(0,'/volumes/scripts')\n"
        "from run_odoo_data_workflows import LocalServerSettings,OdooDataWorkflowRunner\n"
        "settings=LocalServerSettings()\n"
        "if settings.host!='database' or settings.platform_instance!='rehearsal' "
        "or settings.db_name!=" + repr(plan["database"]) + ":\n"
        " raise RuntimeError('Independent rehearsal settings required')\n"
        "runner=OdooDataWorkflowRunner(settings,None,None)\n"
        "runner.acquire_data_workflow_lock()\n"
        "try:\n"
        " runner._ensure_odoo_shell_preflight()\n"
        " result=subprocess.run(runner._odoo_shell_command(),input=" + repr(shell) + ".encode(),"
        "env=runner.os_env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,check=False)\n"
        " markers=[line for line in result.stdout.splitlines() if line.startswith(b'business_validation_result=')]\n"
        " if result.returncode or len(markers)!=1: raise RuntimeError('Validation result unavailable')\n"
        " print(markers[0].decode(),flush=True)\n"
        "finally:\n runner.release_data_workflow_lock()\n"
    )
