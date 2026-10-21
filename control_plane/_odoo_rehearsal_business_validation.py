"""Fixed ORM program. Product input is JSON data, never Python or a shell command."""

BUSINESS_VALIDATION_LIBRARY = r"""
def run_business_validation(env, plan):
    records = {}
    evidence = []
    committed = False
    commit_started = False

    def resolve(value):
        if isinstance(value, dict):
            if '$ref' in value:
                return records[value['$ref']].id
        return value

    def finish(passed):
        steps = evidence or [{'key': plan['steps'][0]['key'], 'record_ids': [], 'passed': False}]
        if not committed:
            steps = [{**step, 'record_ids': []} for step in steps]
        return {'passed': passed, 'committed': committed, 'steps': steps}

    try:
        if env.cr.dbname != plan['database']:
            raise ValueError('Copy identity differs')
        for step in plan['steps']:
            passed = True
            if step['op'] == 'create':
                model = env[step['model']]
                for name, value in step['values'].items():
                    field = model._fields[name]
                    if field.type in {'one2many', 'many2many'}:
                        raise ValueError('Relational commands are unavailable')
                    if isinstance(value, dict):
                        referenced = records[value['$ref']]
                        if field.type != 'many2one' or field.comodel_name != referenced._name:
                            raise ValueError('Synthetic reference model differs')
                    elif field.type == 'many2one' and value is not None and value is not False:
                        raise ValueError('Relations require an earlier synthetic record')
                record = model.with_context(
                    tracking_disable=True, mail_create_nosubscribe=True,
                    mail_notify_force_send=False).create({key: resolve(value) for key, value in step['values'].items()})
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
                return finish(False)
        env.flush_all()
        commit_started = True
        env.cr.commit()
        committed = True
        env.invalidate_all()
        for step in plan['steps']:
            if step['op'] == 'assert':
                value = records[step['record']][step['field']]
                if hasattr(value, 'ids'):
                    value = value.ids
                if step['comparison'] == 'truthy':
                    value = bool(value)
                if value != step['expected']:
                    return finish(False)
        return finish(True)
    except Exception:
        env.cr.rollback()
        if commit_started and not committed:
            raise RuntimeError('Validation commit outcome unavailable') from None
        # The caller retains only a closed failure, never exception text or
        # business values from the copied Client database.
        return finish(False)
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
