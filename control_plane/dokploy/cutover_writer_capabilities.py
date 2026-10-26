from collections.abc import Callable
import hashlib
import json
from typing import Any

from control_plane.contracts.cutover_host import OdooCronWriterBinding
from control_plane.contracts.odoo_cutover import OdooCutoverPlan, OdooSlot
from control_plane.contracts.prepared_public_site import WriterTarget
from control_plane.dokploy.slot_maintenance import ContainerCommand
from control_plane.dokploy.slot_compose import slot_startup_command


CRON_CAPABILITY_PROGRAM = r"""
import hashlib,json,os,sys
import psycopg2
spec=json.load(sys.stdin)
with psycopg2.connect(host=os.environ['ODOO_DB_HOST'],port=os.environ.get('ODOO_DB_PORT','5432'),dbname=spec['database'],user=os.environ['ODOO_DB_USER'],password=os.environ['ODOO_DB_PASSWORD'],connect_timeout=10) as connection, connection.cursor() as cursor:
    cursor.execute("SELECT value FROM ir_config_parameter WHERE key='database.uuid'")
    row=cursor.fetchone()
    if not row or row[0]!=spec['database_uuid']: raise ValueError('writer capability database differs')
    cursor.execute("SELECT d.module||'.'||d.name,c.id,c.active,c.nextcall,c.interval_number,m.model,a.state,a.code FROM ir_model_data d JOIN ir_cron c ON d.res_id=c.id AND d.model='ir.cron' JOIN ir_act_server a ON c.ir_actions_server_id=a.id JOIN ir_model m ON a.model_id=m.id JOIN ir_module_module installed ON installed.name=d.module AND installed.state='installed' WHERE d.module||'.'||d.name=ANY(%s) ORDER BY d.module,d.name",(spec['jobs'],))
    rows=cursor.fetchall()
    if len(rows)!=len(spec['jobs']) or {row[0] for row in rows}!=set(spec['jobs']) or any(not active or not nextcall or interval<1 or not model or state!='code' or not code for _,_,active,nextcall,interval,model,state,code in rows):
        raise ValueError('logical writer lacks its enabled native cron executor')
    evidence=hashlib.sha256(json.dumps(rows,default=str,sort_keys=True).encode()).hexdigest()
print(json.dumps({'writer_id':spec['writer_id'],'backend':'odoo_cron','jobs':sorted(spec['jobs']),'registry_sha256':evidence},sort_keys=True))
"""


EXECUTOR_PREFLIGHT_PROGRAM = r"""
import ast,configparser,hashlib,json,os,runpy,sys,tempfile
from pathlib import Path
import psycopg2
spec=json.load(sys.stdin)
sys.path.insert(0,'/volumes/scripts')
startup=runpy.run_path('/volumes/scripts/run_odoo_startup.py',run_name='executor_preflight')
with tempfile.TemporaryDirectory() as directory:
    arguments=type('Arguments',(),{'config_path':directory+'/odoo.conf'})()
    runner_settings=startup['_load_settings'](arguments)
    os.environ.clear();os.environ.update(spec['environment'])
    settings=startup['_load_settings'](arguments)
    if settings.database_name!=spec['database'] or any(getattr(settings,key)!=getattr(runner_settings,key) for key in ('database_host','database_port','database_user','database_password')):
        raise ValueError('effective web database differs from the admitted runner transport')
    startup['_write_runtime_config'](settings)
    parser=configparser.ConfigParser(interpolation=None)
    parser.read(settings.config_path)
    options=dict(parser['options'])
    if int(options.get('max_cron_threads','-1'))!=0: raise ValueError('web warming would start cron writers')
    server_wide={name.strip() for name in options.get('server_wide_modules','base,web').split(',') if name.strip()}
    if not server_wide <= {'base','web'}: raise ValueError('web-hosted executors lack isolated cutover support')
    if parser.has_section('queue_job') or os.environ.get('ODOO_QUEUE_JOB_CHANNELS') or os.environ.get('QUEUE_JOB_CHANNELS'):
        raise ValueError('HTTP-dependent queue executors are unsupported')
    with psycopg2.connect(host=settings.database_host,port=settings.database_port,dbname=settings.database_name,user=settings.database_user,password=settings.database_password,connect_timeout=10) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT value FROM ir_config_parameter WHERE key='database.uuid'")
        if cursor.fetchone()!=(spec['database_uuid'],): raise ValueError('executor inventory database differs')
        cursor.execute("SELECT name FROM ir_module_module WHERE state IN ('installed','to install','to upgrade','to remove')")
        modules={row[0] for row in cursor.fetchall()} | set(spec['planned_modules']) | server_wide
    if 'queue_job' in modules: raise ValueError('installed HTTP-dependent queue executor is unsupported')
    import odoo
    from odoo.tools import config
    from odoo.modules.module import initialize_sys_path
    config.parse_config(['-c',settings.config_path])
    initialize_sys_path()
    roots=[Path(path) for path in odoo.addons.__path__]
    manifests={}
    for name in sorted(modules):
        paths=[root/name/'__manifest__.py' for root in roots if (root/name/'__manifest__.py').is_file()]
        if len(paths)!=1: raise ValueError('executor module inventory is missing or ambiguous')
        raw=paths[0].read_bytes()
        manifest=ast.literal_eval(raw.decode())
        if manifest.get('post_load'): raise ValueError('module starts an unqualified server-wide hook')
        manifests[name]=hashlib.sha256(raw).hexdigest()
    evidence=hashlib.sha256(json.dumps({'manifests':manifests,'server_wide':sorted(server_wide),'cron_threads':0},sort_keys=True).encode()).hexdigest()
print(json.dumps({'image':spec['image'],'database_uuid':spec['database_uuid'],'modules':sorted(modules),'executor_sha256':evidence,'web_crons':False,'server_wide':sorted(server_wide)}))
"""


class NativeCronWriterCapabilities:
    def __init__(
        self,
        plan: OdooCutoverPlan,
        bindings: tuple[OdooCronWriterBinding, ...],
        execute: ContainerCommand,
        runner: Callable[[OdooSlot], str],
        web: Callable[[OdooSlot], dict[str, Any]] | None = None,
    ) -> None:
        expected = {writer.writer_id for writer in plan.writers if writer.kind != "web"}
        if {binding.writer_id for binding in bindings} != expected or len(bindings) != len(
            expected
        ):
            raise ValueError(
                "every logical background writer requires its own native cron capability"
            )
        self.plan, self.bindings, self.execute, self.runner = plan, bindings, execute, runner
        self.web = web

    def preflight(self, slot: OdooSlot) -> str:
        if slot not in (self.plan.old, self.plan.new):
            raise ValueError("executor preflight crossed the admitted artifact")
        if self.web is None:
            raise ValueError("executor preflight requires the exact web container")
        web = self.web(slot)
        config = web["Config"]
        command = config.get("Cmd")
        allowed = web.get("startup_commands", ["exec " + slot_startup_command()])
        if (
            config.get("Image") != slot.image
            or config.get("Entrypoint") != ["/bin/sh", "-c"]
            or not isinstance(command, list)
            or len(command) != 1
            or command[0] not in allowed
            or any(
                mount.get("Type") != "volume" or mount.get("Destination") != "/volumes/data"
                for mount in web.get("Mounts", [])
            )
        ):
            raise ValueError("web executor differs from the supported immutable startup topology")
        environment: dict[str, str] = {}
        for entry in config.get("Env", []):
            key, separator, value = entry.partition("=")
            if not separator or key in environment:
                raise ValueError("web executor environment is incomplete or ambiguous")
            environment[key] = value
        observed = json.loads(
            self.execute(
                (
                    "docker",
                    "exec",
                    "-i",
                    self.runner(slot),
                    "/venv/bin/python",
                    "-c",
                    EXECUTOR_PREFLIGHT_PROGRAM,
                ),
                json.dumps(
                    {
                        "image": slot.image,
                        "environment": environment,
                        "database": slot.binding.database,
                        "database_uuid": slot.runtime_identity.database_uuid,
                        "planned_modules": list(self.plan.compatibility.install_modules)
                        if slot == self.plan.new
                        else [],
                    }
                ),
            )
        )
        if (
            observed.get("image") != slot.image
            or observed.get("database_uuid") != slot.runtime_identity.database_uuid
            or observed.get("web_crons") is not False
            or not isinstance(observed.get("modules"), list)
            or not observed["modules"]
            or not isinstance(observed.get("server_wide"), list)
            or not set(observed["server_wide"]) <= {"base", "web"}
            or not isinstance(observed.get("executor_sha256"), str)
            or len(observed["executor_sha256"]) != hashlib.sha256().digest_size * 2
        ):
            raise ValueError(
                "native executor preflight lacks complete effective configuration/inventory"
            )
        return str(observed["executor_sha256"])

    def __call__(self, slot: OdooSlot, writer: WriterTarget) -> str:
        if writer not in self.plan.writers or slot not in (self.plan.old, self.plan.new):
            raise ValueError("native writer capability crossed its original lane")
        if writer.kind == "web":
            return ""
        binding = next(item for item in self.bindings if item.writer_id == writer.writer_id)
        observed = json.loads(
            self.execute(
                (
                    "docker",
                    "exec",
                    "-i",
                    self.runner(slot),
                    "/venv/bin/python",
                    "-c",
                    CRON_CAPABILITY_PROGRAM,
                ),
                json.dumps(
                    dict(
                        database=slot.binding.database,
                        database_uuid=slot.runtime_identity.database_uuid,
                        writer_id=writer.writer_id,
                        jobs=list(binding.cron_xmlids),
                    )
                ),
            )
        )
        if (
            observed.get("writer_id") != writer.writer_id
            or observed.get("backend") != "odoo_cron"
            or observed.get("jobs") != sorted(binding.cron_xmlids)
            or not isinstance(observed.get("registry_sha256"), str)
            or len(observed["registry_sha256"]) != hashlib.sha256().digest_size * 2
        ):
            raise ValueError("native writer capability has no bound registry readback")
        return str(observed["registry_sha256"])
