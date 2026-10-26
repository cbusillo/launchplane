from collections.abc import Callable
from datetime import UTC, datetime
import hashlib
import json
import time
from typing import Any

from control_plane.contracts.odoo_cutover import OdooCutoverPlan, OdooSlot
from control_plane.contracts.prepared_public_site import WriterTarget
from control_plane.dokploy.slot_maintenance import ContainerCommand
from control_plane.dokploy.slot_writers import SlotWriterContainer


DATABASE_OBSERVATION_PROGRAM = r"""
import json,os,sys
import psycopg2
spec=json.load(sys.stdin)
with psycopg2.connect(host=os.environ['ODOO_DB_HOST'],port=os.environ.get('ODOO_DB_PORT','5432'),dbname=spec['database'],user=os.environ['ODOO_DB_USER'],password=os.environ['ODOO_DB_PASSWORD'],connect_timeout=10) as connection:
    connection.set_session(readonly=True,autocommit=True)
    with connection.cursor() as cursor:
        cursor.execute("SELECT value FROM ir_config_parameter WHERE key='database.uuid'")
        row=cursor.fetchone()
        if not row or row[0]!=spec['database_uuid']: raise ValueError('physical database differs')
        cursor.execute("SELECT pid,datname,client_addr::text,backend_start::text,state FROM pg_stat_activity WHERE backend_type='client backend' AND pid<>pg_backend_pid() AND (datname=%s OR usename=current_user) ORDER BY pid",(spec['database'],))
        sessions=[dict(zip(('pid','database','address','backend_start','state'),row)) for row in cursor.fetchall()]
        cursor.execute('SELECT count(*) FROM pg_prepared_xacts WHERE database=%s',(spec['database'],))
        prepared=cursor.fetchone()[0]
        cursor.execute('SELECT clock_timestamp()::text')
        observed_at=cursor.fetchone()[0]
print(json.dumps({'database_uuid':spec['database_uuid'],'sessions':sessions,'prepared_transactions':prepared,'observed_at':observed_at},sort_keys=True))
"""


PROCESS_OBSERVATION_PROGRAM = r"""
import configparser,json,os,sys
from pathlib import Path
spec=json.load(sys.stdin)
processes=[]
http_ports=set()
for path in Path('/proc').glob('[0-9]*/cmdline'):
    try: argv=[part.decode() for part in path.read_bytes().split(b'\0') if part]
    except FileNotFoundError: continue
    if '/odoo/odoo-bin' not in argv: continue
    config_path=argv[argv.index('-c')+1] if '-c' in argv else argv[argv.index('--config')+1]
    parser=configparser.ConfigParser(interpolation=None); parser.read(config_path)
    options=dict(parser['options'])
    for index,argument in enumerate(argv):
        if argument=='--no-http': options['http_enable']='false'
        elif argument.startswith('--max-cron-threads='): options['max_cron_threads']=argument.split('=',1)[1]
        elif argument=='--max-cron-threads': options['max_cron_threads']=argv[index+1]
        elif argument=='-d': options['db_name']=argv[index+1]
        elif argument.startswith('--db_host='): options['db_host']=argument.split('=',1)[1]
    if options.get('db_name')!=spec['database'] or options.get('db_host')!=os.environ['ODOO_DB_HOST']:
        raise ValueError('process database differs')
    processes.append({'pid':int(path.parent.name),'http':options.get('http_enable','true').lower() not in {'false','0'},'cron_threads':int(options['max_cron_threads'])})
    http_ports.update((int(options.get('http_port','8069')),int(options.get('gevent_port','8072'))))
listeners=[]
for path in (Path('/proc/net/tcp'),Path('/proc/net/tcp6')):
    for line in path.read_text().splitlines()[1:]:
        fields=line.split()
        if fields[3]=='0A': listeners.append(int(fields[1].split(':')[1],16))
print(json.dumps({'processes':processes,'listening_ports':sorted(listeners),'http_ports':sorted(http_ports)},sort_keys=True))
"""


def process_supports_writer(observation: dict[str, Any], writer: WriterTarget) -> bool:
    processes = observation.get("processes")
    if not isinstance(processes, list) or not processes:
        return False
    if writer.kind == "web":
        return all(
            process.get("http") is True and process.get("cron_threads") == 0
            for process in processes
        )
    ports = observation.get("http_ports")
    listeners = observation.get("listening_ports")
    return (
        isinstance(ports, list)
        and bool(ports)
        and isinstance(listeners, list)
        and not set(ports).intersection(listeners)
        and all(
            process.get("http") is False
            and type(process.get("cron_threads")) is int
            and process["cron_threads"] > 0
            for process in processes
        )
    )


def container_addresses(value: dict[str, Any]) -> set[str]:
    networks = value.get("NetworkSettings", {}).get("Networks")
    if not isinstance(networks, dict):
        raise ValueError("native container network observation is incomplete")
    return {
        str(network[key])
        for network in networks.values()
        for key in ("IPAddress", "GlobalIPv6Address")
        if network.get(key)
    }


class NativeWriterObserver:
    def __init__(
        self,
        plan: OdooCutoverPlan,
        *,
        execute: ContainerCommand,
        runner: Callable[[OdooSlot], str],
        target: Callable[[OdooSlot, WriterTarget], SlotWriterContainer],
        addresses: Callable[[OdooSlot, WriterTarget], set[str]],
        timeout_seconds: float = 60,
        capability: Callable[[OdooSlot, WriterTarget], str] | None = None,
    ) -> None:
        self.plan, self.execute, self.runner = plan, execute, runner
        self.target, self.addresses = target, addresses
        self.timeout_seconds = timeout_seconds
        self.capability = capability
        self.evidence: dict[tuple[str, str], str] = {}

    def database(self) -> dict[str, Any]:
        output = self.execute(
            (
                "docker",
                "exec",
                "-i",
                self.runner(self.plan.old),
                "/venv/bin/python",
                "-c",
                DATABASE_OBSERVATION_PROGRAM,
            ),
            json.dumps(
                {
                    "database": self.plan.old.binding.database,
                    "database_uuid": self.plan.old.runtime_identity.database_uuid,
                }
            ),
        )
        value: dict[str, Any] = json.loads(output)
        if value.get(
            "database_uuid"
        ) != self.plan.old.runtime_identity.database_uuid or not isinstance(
            value.get("sessions"), list
        ):
            raise ValueError("native writer database observation is incomplete")
        if (
            type(value.get("prepared_transactions")) is not int
            or value["prepared_transactions"] < 0
        ):
            raise ValueError("native prepared-transaction observation is incomplete")
        observed_at = datetime.fromisoformat(value["observed_at"])
        if not 0 <= (datetime.now(UTC) - observed_at).total_seconds() <= 30:
            raise ValueError("native database observation is stale")
        return value

    def active_jobs(self, slot: OdooSlot, writer: WriterTarget) -> int:
        value = self.database()
        own = self.addresses(slot, writer)
        known_other = set().union(
            *(
                self.addresses(current, item)
                for current in (self.plan.old, self.plan.new)
                for item in self.plan.writers
                if (current, item) != (slot, writer)
            )
        )
        count = int(value["prepared_transactions"])
        for session in value["sessions"]:
            address = session.get("address")
            if not isinstance(address, str) or not address:
                count += 1
            elif address in own or address not in known_other:
                count += 1
        self.evidence[slot.slot_id, writer.writer_id] = hashlib.sha256(
            json.dumps(value, sort_keys=True).encode()
        ).hexdigest()
        return count

    def native_ready(self, target: SlotWriterContainer) -> None:
        slot = self.plan.old if target.slot_id == self.plan.old.slot_id else self.plan.new
        if target.writer.kind != "web" and self.capability is None:
            raise ValueError("background writer lacks its own native executor capability")
        deadline = time.monotonic() + self.timeout_seconds
        previous: tuple[int, str] | None = None
        while True:
            if target.writer.kind != "web":
                assert self.capability is not None
                self.capability(slot, target.writer)
            bound = self.target(slot, target.writer)
            observed = json.loads(self.execute(("docker", "inspect", bound.container), None))
            if len(observed) != 1 or observed[0]["Config"]["Image"] != slot.image:
                raise ValueError("native writer image or physical process changed")
            state = observed[0]["State"]
            if (
                state.get("Running") is not True
                or state.get("Restarting") is not False
                or state.get("Paused") is not False
            ):
                raise ValueError("native writer is not steadily running")
            if type(observed[0].get("RestartCount")) is not int or not state.get("StartedAt"):
                raise ValueError("native writer restart identity is unavailable")
            process = json.loads(
                self.execute(
                    (
                        "docker",
                        "exec",
                        "-i",
                        bound.container,
                        "/venv/bin/python",
                        "-c",
                        PROCESS_OBSERVATION_PROGRAM,
                    ),
                    json.dumps({"database": slot.binding.database}),
                )
            )
            addresses = container_addresses(observed[0])
            database = self.database()
            connected = bool(addresses) and any(
                session.get("address") in addresses for session in database["sessions"]
            )
            identity = observed[0].get("RestartCount", 0), state.get("StartedAt", "")
            if process_supports_writer(process, target.writer) and connected:
                if previous == identity:
                    return
                previous = identity
            else:
                previous = None
            if time.monotonic() >= deadline:
                raise TimeoutError("native writer has no observed process/database liveness")
            time.sleep(min(1, max(0, deadline - time.monotonic())))
