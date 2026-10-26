from collections.abc import Callable
import hashlib
import json

from control_plane.contracts.cutover_host import OdooCronWriterBinding
from control_plane.contracts.odoo_cutover import OdooCutoverPlan, OdooSlot
from control_plane.contracts.prepared_public_site import WriterTarget
from control_plane.dokploy.slot_maintenance import ContainerCommand


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


class NativeCronWriterCapabilities:
    def __init__(
        self,
        plan: OdooCutoverPlan,
        bindings: tuple[OdooCronWriterBinding, ...],
        execute: ContainerCommand,
        runner: Callable[[OdooSlot], str],
    ) -> None:
        expected = {writer.writer_id for writer in plan.writers if writer.kind != "web"}
        if {binding.writer_id for binding in bindings} != expected or len(bindings) != len(
            expected
        ):
            raise ValueError(
                "every logical background writer requires its own native cron capability"
            )
        self.plan, self.bindings, self.execute, self.runner = plan, bindings, execute, runner

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
