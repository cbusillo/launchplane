ODOO_RUNNER_PROGRAM = r"""
from collections.abc import Callable
from typing import Any

def qualified_script_runner(spec: dict[str, Any], docker: Callable[..., bytes], refused: Callable[[str], Exception] = RuntimeError) -> str:
    project=spec['project']
    ids=docker('ps','--filter','label=com.docker.compose.project='+project,
        '--filter','label=com.docker.compose.service=script-runner','--format','{{.ID}}').decode().split()
    if len(ids)!=1: raise refused('runner_unselected')
    runner=json.loads(docker('inspect',ids[0]))[0]
    env=dict(value.split('=',1) for value in runner['Config']['Env'] if '=' in value)
    observed=json.loads(env.get(spec['identity_key'],'{}'))
    expected=spec['identity']
    observed.pop('deployed_at',None); expected.pop('deployed_at',None)
    if observed!=expected or runner['Config']['Image']!=spec['image']:
        raise refused('runner_changed')
    if env.get('PLATFORM_INSTANCE')!=('preview' if spec['identity']['environment_kind']=='preview' else spec['identity']['instance']) or env.get('ODOO_DB_HOST')!='database' or env.get('ODOO_DB_NAME')!=spec['database']:
        raise refused('runner_changed')
    return ids[0]
"""

ODOO_SHELL_WRAPPER = r"""

import argparse, contextlib, io, json, logging, os, subprocess, sys, tempfile, textwrap
logging.disable(logging.CRITICAL)
request=json.loads(sys.stdin.read())
program=request.pop('program')
failure=request.pop('failure_program')
fallback=request.pop('fallback_result')
sys.path.insert(0,'/volumes/scripts')
import run_odoo_startup as startup
os.environ['ODOO_DB_NAME']=request['database']
os.environ['ODOO_LOG_LEVEL']='critical'
with tempfile.TemporaryDirectory(prefix='launchplane-walkthrough-') as directory:
    output=os.path.join(directory,'result.json')
    request['result_file']=output
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        settings=startup._load_settings(argparse.Namespace(config_path=os.path.join(directory,'odoo.conf')))
        startup._write_runtime_config(settings)
    script='request='+repr(request)+'\ncommit_started=False\ntry:\n'+textwrap.indent(program,'    ')+'\nexcept Exception:\n'+textwrap.indent(failure,'    ')
    try:
        child=subprocess.run(startup._build_odoo_shell_command(settings),
            input=script.encode(),
            env=startup._odoo_environment(settings),capture_output=True,timeout=80)
        if child.returncode: raise RuntimeError('Testing account consumer failed')
        with open(output) as stream: result=json.load(stream)
    except Exception:
        result=fallback
    print(json.dumps(result))
"""
