from control_plane.durable_operation_authorization import DurableOperationAuthorizationDeniedError
import base64
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
import json
import logging
import hashlib
import os
from pathlib import Path
import secrets
import time
from typing import Any, Protocol, cast
from urllib.parse import urlsplit

import yaml

from control_plane.contracts.generic_web_cutover import GenericWebCutoverLane, GenericWebCutoverPlan
from control_plane.contracts.deployment_record import DeploymentRecord
from control_plane.contracts.odoo_cutover import (
    ODOO_CUTOVER_TERMINAL_STEPS,
    OdooCutoverPlan,
    OdooCutoverRecord,
    OdooMaintenanceReceipt,
    OdooRecoveryTuple,
    OdooSlot,
    OdooSlotReadiness,
)
from control_plane.contracts.odoo_cutover_lane import OdooCutoverLane
from control_plane.contracts.odoo_online_backup import OdooProdBackupCaptureEvidence
from control_plane.contracts.odoo_website_settings import OdooWebsiteSettingsIntent
from control_plane.contracts.prepared_public_site import (
    CapturedPublicResponse,
    PreparedPublicSite,
    PublicSiteBinding,
    PublicSitePlan,
    WriterTarget,
)
from control_plane.contracts.promotion_record import HealthcheckEvidence
from control_plane.dokploy import api, post_deploy
from control_plane.dokploy.cutover_execution import require_cutover_execution
from control_plane.dokploy.cutover_host_state import (
    CutoverHostState,
    verify_shared_watched_directory,
    writer_service,
)
from control_plane.dokploy.cutover_probes import CutoverProbes, PrivateHealthFailed
from control_plane.dokploy.cutover_transport import CutoverHostTransport, CutoverCommandFailed
from control_plane.dokploy.native_copy_stream import NativeSourceUnavailable
from control_plane.dokploy.cutover_writer_observer import NativeWriterObserver, container_addresses
from control_plane.dokploy.cutover_writer_capabilities import NativeCronWriterCapabilities
from control_plane.dokploy.cutover_restore_probe import (
    verify_disposable_database_restore,
    RESTORE_PREFLIGHT_PROGRAM,
)
from control_plane.dokploy.generic_web_cutover import WebSlotDriver
from control_plane.dokploy.odoo_cutover import OdooSlotDriver
from control_plane.dokploy.prepared_host import PreparedSiteDirectory
from control_plane.dokploy.prepared_server import require_prepared_server
from control_plane.contracts.backup_gate_record import BackupGateRecord
from control_plane.contracts.cutover_host import OdooCutoverHostRuntime
from control_plane.dokploy.service_cutover import OdooHostCapabilities
from control_plane.dokploy.slot_compose import (
    render_odoo_app_slot_compose,
    held_slot_command,
    slot_startup_command,
)
from control_plane.dokploy.slot_maintenance import (
    maintenance_payload,
    payload_digest,
    slot_run_marker,
    slot_run_stamp,
)
from control_plane.dokploy.slot_prepare import prepare_dokploy_odoo_slot
from control_plane.dokploy.slot_readiness import verify_odoo_slot_readiness
from control_plane.dokploy.slot_startup import DokploySlotStartup
from control_plane.dokploy.slot_website_settings import StartupSettingsReadback
from control_plane.dokploy.slot_writers import (
    ContainerSlotWriters,
    SlotWriterContainer,
    physical_writer_targets,
)
from control_plane.dokploy.source import read_dokploy_config
from control_plane.dokploy.traefik_cutover import (
    TraefikFileRoute,
    TraefikRouteTarget,
    route_payload,
)
from control_plane.odoo_cutover_plan import (
    CutoverAuthorityStore,
    cutover_record_id,
    rollback_pause_identity,
)
from control_plane.odoo_cutover_storage import DeploymentStore
from control_plane.odoo_cutover_recovery import (
    CutoverEntryRefusedError,
    startup_recovery_pause,
    BackupVerificationRejected,
    backup_recovery_eligible,
    abort_reconciliation_eligible,
    publication_recovery_pause,
    maintenance_reconciliation_eligible,
)
from control_plane.prepared_public_site import prepare_public_site, verify_snapshot
from control_plane.workflows.odoo_prod_backup_gate import OdooProdBackupVerificationEvidence
from control_plane.workflows.production_promotion_backup import (
    ODOO_PROMOTION_BACKUP_ACTION,
    ProductionPromotionBackupStore,
    require_production_promotion_backup,
)


class CutoverHostStore(
    CutoverAuthorityStore, DeploymentStore, ProductionPromotionBackupStore, Protocol
):
    def write_backup_gate_record(self, record: BackupGateRecord) -> None: ...

    def list_deployment_records(
        self, *, context_name: str, instance_name: str
    ) -> tuple[DeploymentRecord, ...]: ...


STARTUP_CAPABILITY_PROGRAM = r"""
import json,runpy,sys
spec=json.load(sys.stdin)
sys.path.insert(0,'/volumes/scripts')
module=runpy.run_path('/volumes/scripts/run_odoo_startup.py',run_name='cutover_startup_capability')
sys.argv=['startup','-c','/tmp/platform.odoo.conf','--scheduler','--ready-file',spec['marker'],'--ready-token',spec['stamp']]
parsed=module['_parse_arguments']()
if parsed.scheduler is not True or parsed.ready_file!=spec['marker'] or parsed.ready_token!=spec['stamp']:
    raise ValueError('native scheduler startup is unavailable')
print(json.dumps({'scheduler':True}))
"""


READINESS_RELEASE_PROGRAM = r"""
import json,os,sys
import psycopg2
spec=json.load(sys.stdin)
with psycopg2.connect(host=os.environ['ODOO_DB_HOST'],port=os.environ.get('ODOO_DB_PORT','5432'),dbname=spec['database'],user=os.environ['ODOO_DB_USER'],password=os.environ['ODOO_DB_PASSWORD'],connect_timeout=10) as connection, connection.cursor() as cursor:
    if 'release' not in spec: connection.set_session(readonly=True)
    cursor.execute("SELECT key,value FROM ir_config_parameter WHERE key IN ('database.uuid','odoo_devkit.credential_boundary_commit')")
    values=dict(cursor.fetchall())
    if values.get('database.uuid')!=spec['database_uuid'] or not values.get('odoo_devkit.credential_boundary_commit'):
        raise ValueError('native readiness has no committed credential boundary')
    if spec['credential_commit_id'] and values['odoo_devkit.credential_boundary_commit']!=spec['credential_commit_id']:
        raise ValueError('native boundary differs from the passing maintenance')
    cursor.execute("SELECT name FROM ir_module_module WHERE state IN ('to install','to upgrade','to remove')")
    if cursor.fetchall(): raise ValueError('native readiness has pending modules')
    cursor.execute("SELECT name FROM ir_module_module WHERE state='installed' AND name=ANY(%s)",(spec['modules'],))
    if {row[0] for row in cursor.fetchall()}!=set(spec['modules']): raise ValueError('native readiness module coverage differs')
    if 'release' in spec:
        payload=json.dumps(spec['release'],sort_keys=True,separators=(',',':'))
        cursor.execute("INSERT INTO ir_config_parameter(key,value,create_uid,write_uid) VALUES ('launchplane.readiness.release',%s,1,1) ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value",(payload,))
print(json.dumps({'release':spec['release']} if 'release' in spec else {'ready_base':True},sort_keys=True))
"""


BACKUP_MANIFEST_PROGRAM = r"""
import fcntl,json,os,sys,tempfile
from pathlib import Path
spec=json.load(sys.stdin); path=Path(spec['directory']); claim=path.with_name(path.name+'.attempt.json')
if path.is_symlink() or claim.is_symlink() or path.resolve()!=path: raise ValueError('backup path differs')
if 'cancel_absent' in spec and not claim.exists() and (not path.exists() or path.is_dir() and not any(path.iterdir())):
    claim.parent.mkdir(parents=True,exist_ok=True)
    descriptor,temporary=tempfile.mkstemp(dir=claim.parent,prefix='.capture-cancel-')
    try:
        with os.fdopen(descriptor,'w') as handle:
            os.fchmod(handle.fileno(),0o600);json.dump({**spec['cancel_absent'],'cancelled':True},handle);handle.flush();os.fsync(handle.fileno())
        try: os.link(temporary,claim)
        except FileExistsError: pass
        descriptor=os.open(claim.parent,os.O_RDONLY|os.O_DIRECTORY)
        try: os.fsync(descriptor)
        finally: os.close(descriptor)
    finally: os.unlink(temporary)
binding=json.loads(claim.read_text()) if claim.is_file() else None
canonical={key:binding[key] for key in ('record_id','nonce')} if binding is not None else None
if binding is not None and binding.get('cancelled') is True:
    if set(binding)!={'record_id','nonce','cancelled'}: raise ValueError('cancelled capture claim differs')
    print(json.dumps({'state':'failed','claim':canonical}))
elif (path/'manifest.json').is_file() and claim.is_file():
    print(json.dumps({'state':'captured','manifest':json.loads((path/'manifest.json').read_text()),'claim':canonical}))
elif path.with_name(path.name+'.failed.json').is_file():
    failure=path.with_name(path.name+'.failed.json')
    if failure.is_symlink() or not claim.is_file() or json.loads(failure.read_text())!=json.loads(claim.read_text()): raise ValueError('backup failure differs from original claim')
    print(json.dumps({'state':'failed','claim':canonical}))
elif binding is not None and binding.get('lock_inherited') is True:
    lock=path.with_name(path.name+'.capture.lock')
    if not lock.is_file() or lock.is_symlink(): print(json.dumps({'state':'uncertain'}))
    else:
        descriptor=os.open(lock,os.O_RDWR|os.O_NOFOLLOW)
        try:
            try: fcntl.flock(descriptor,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError: print(json.dumps({'state':'uncertain'}))
            else: print(json.dumps({'state':'failed','claim':canonical}))
        finally: os.close(descriptor)
elif claim.exists() or path.exists() and (not path.is_dir() or any(path.iterdir())): print(json.dumps({'state':'uncertain'}))
else: print(json.dumps({'state':'absent'}))
"""


def readiness_container_states(
    transport: CutoverHostTransport, candidate: str, runner: Callable[[], str]
) -> dict[str, object]:
    template = "{{json .State.Status}} {{json .State.Running}} {{json .State.OOMKilled}} {{json .State.ExitCode}}"
    try:
        output = transport.read(
            ("docker", "inspect", "--format", template, candidate, runner()),
            None,
            timeout_seconds=20,
        )
        lines = output.splitlines()
        if len(lines) != 2:
            return {"status": "unavailable"}
        states: dict[str, object] = {}
        for role, line in zip(("candidate_web", "probe_runner"), lines, strict=True):
            status, running, oom_killed, exit_code = json.loads("[" + ",".join(line.split()) + "]")
            if (
                status
                not in {"created", "running", "paused", "restarting", "removing", "exited", "dead"}
                or type(running) is not bool
                or type(oom_killed) is not bool
                or type(exit_code) is not int
            ):
                return {"status": "unavailable"}
            states[role] = {
                "status": status,
                "running": running,
                "oom_killed": oom_killed,
                "exit_code": exit_code,
            }
        return states
    except (ValueError, OSError, TypeError):
        return {"status": "unavailable"}


class GuardedFileRoute(TraefikFileRoute):
    def __init__(self, target: TraefikRouteTarget, guard: Callable[[], None]) -> None:
        super().__init__(target)
        self.guard = guard

    def publish(self, backend: str, *, expected: tuple[str, ...]) -> str:
        self.guard()
        return super().publish(backend, expected=expected)


class DokployCutoverHost:
    def __init__(self) -> None:
        self.routes: dict[str, TraefikFileRoute] = {}

    def odoo(
        self, plan: OdooCutoverPlan, lane: OdooCutoverLane, store: object, root: Path
    ) -> OdooHostCapabilities:
        bound = BoundOdooCutoverHost(plan, lane, cast(CutoverHostStore, store), root)
        bound.preflight(plan)
        self.routes[lane.route_id] = bound.route
        return bound.capabilities()

    def web(self, plan: GenericWebCutoverPlan, store: object, root: Path) -> WebSlotDriver:
        raise ValueError("generic-web installed host capability is not qualified")

    def route(self, lane: OdooCutoverLane | GenericWebCutoverLane) -> TraefikFileRoute:
        route = self.routes.get(lane.route_id)
        if route is None or route.target.host != lane.host:
            raise ValueError("cutover route has no bound installed host")
        return route


class BoundOdooCutoverHost:
    def __init__(
        self, plan: OdooCutoverPlan, lane: OdooCutoverLane, store: CutoverHostStore, root: Path
    ) -> None:
        if lane.host_runtime is None or lane.host_runtime != plan.host_runtime:
            raise ValueError("cutover host requires its persisted lane capabilities")
        self.plan, self.lane, self.store, self.root = plan, lane, store, root
        self.runtime: OdooCutoverHostRuntime = lane.host_runtime
        self.host, self.token = read_dokploy_config(control_plane_root=root)
        self.payloads = {slot.slot_id: self.payload(slot) for slot in (plan.old, plan.new)}
        servers = {str(payload.get("serverId") or "") for payload in self.payloads.values()}
        if len(servers) != 1:
            raise ValueError("cutover slots do not share one admitted provider host")
        self.transport = CutoverHostTransport(
            host=self.host,
            token=self.token,
            server_id=servers.pop(),
            guard=self.guard,
            timeout_seconds=self.runtime.operation_timeout_seconds,
        )
        self.state = CutoverHostState(
            plan,
            self.runtime.authority_directory,
            self.transport.read,
            self.transport,
            placement={
                "server_id": self.transport.server_id,
                "projects": {key: value.get("appName") for key, value in self.payloads.items()},
            },
        )
        self.admitted = False
        self.route = GuardedFileRoute(
            TraefikRouteTarget(
                route_id=plan.route_id, host=plan.host, **self.runtime.route.model_dump()
            ),
            self.guard,
        )
        prepared_root = os.environ.get("LAUNCHPLANE_PREPARED_PUBLIC_ROOT", "")
        if (
            not prepared_root
            or not Path(prepared_root).is_absolute()
            or not Path(prepared_root).is_dir()
        ):
            raise ValueError("cutover host requires its dedicated prepared-public volume")
        self.prepared_path = Path(prepared_root) / plan.route_id
        targets = self.initial_targets()
        self.native_writer_capability = NativeCronWriterCapabilities(
            plan,
            self.runtime.cron_writer_bindings,
            self.transport.read,
            self.runner,
            self.executor_web,
        )
        self.observer = NativeWriterObserver(
            plan,
            execute=self.transport.read,
            runner=self.runner,
            target=self.target,
            addresses=self.state.addresses,
            timeout_seconds=self.runtime.readiness_timeout_seconds,
            capability=self.native_writer_capability,
        )
        self.writers = ContainerSlotWriters(
            plan,
            targets,
            self.transport,
            lambda target: self.observer.active_jobs(self.slot(target.slot_id), target.writer),
            native_ready=self.observer.native_ready,
            database_evidence=lambda target: self.observer.evidence.get(
                (target.slot_id, target.writer.writer_id), ""
            ),
        )
        self.startup = self.startup_adapter(self.transport)
        self.startup_reader = self.startup_adapter(self.transport.read)
        self.probes = CutoverProbes(plan, self.transport.read, self.runner)

    def slot(self, slot_id: str) -> OdooSlot:
        return self.plan.old if slot_id == self.plan.old.slot_id else self.plan.new

    def payload(self, slot: OdooSlot) -> api.JsonObject:
        return api.fetch_dokploy_target_payload(
            host=self.host, token=self.token, target_type="compose", target_id=slot.compose_id
        )

    def record(self) -> OdooCutoverRecord | None:
        try:
            deployment = self.store.read_deployment_record(
                self.plan.new.runtime_identity.deployment_record_id
            )
        except FileNotFoundError:
            return None
        record = deployment.odoo_cutover
        if record is not None and record.plan != self.plan:
            raise ValueError("installed host crossed the original cutover operation")
        return record

    def guard(self) -> None:
        require_cutover_execution(self.plan)
        profile = self.store.read_product_profile_record(self.plan.new.binding.product_id)
        lanes = [
            lane
            for lane in profile.lanes
            if (lane.context, lane.instance)
            == (self.plan.new.runtime_identity.context, self.plan.new.binding.environment_id)
        ]
        if len(lanes) != 1 or lanes[0].odoo_cutover != self.lane:
            raise ValueError("cutover lane authority changed during the original operation")

    def startup_adapter(self, execute: Any) -> DokploySlotStartup:
        return DokploySlotStartup(
            host=self.host,
            token=self.token,
            writers=self.writers,
            execute=execute,
            authority_directory=self.runtime.authority_directory,
        )

    def containers(self, slot: OdooSlot, service: str) -> tuple[str, ...]:
        project = str(self.payloads[slot.slot_id].get("appName") or "")
        if not project:
            raise ValueError("cutover slot has no provider-owned project")
        return tuple(
            self.transport.read(
                (
                    "docker",
                    "ps",
                    "--all",
                    "--no-trunc",
                    "--quiet",
                    "--filter",
                    "label=com.docker.compose.project=" + project,
                    "--filter",
                    "label=com.docker.compose.service=" + service,
                ),
                None,
            ).split()
        )

    def inspect(self, slot: OdooSlot, service: str, container: str) -> dict[str, Any]:
        values = json.loads(self.transport.read(("docker", "inspect", container), None))
        if len(values) != 1:
            raise ValueError("cutover process inventory is ambiguous")
        value: dict[str, Any] = values[0]
        labels = value["Config"]["Labels"]
        if (
            value["Config"]["Image"] != slot.image
            or labels.get("com.docker.compose.project")
            != self.payloads[slot.slot_id].get("appName")
            or labels.get("com.docker.compose.service") != service
            or str(labels.get("com.docker.compose.oneoff")).lower() != "false"
            or labels.get("com.docker.compose.container-number") != "1"
        ):
            raise ValueError("cutover process differs from its immutable project/service binding")
        return value

    def target(self, slot: OdooSlot, writer: WriterTarget) -> SlotWriterContainer:
        service = writer_service(writer)
        containers = self.containers(slot, service)
        if len(containers) != 1:
            raise ValueError("cutover writer inventory requires guarded startup recovery")
        value = self.inspect(slot, service, containers[0])
        if self.admitted:
            self.state.observe_address(slot, writer, value)
        return SlotWriterContainer(writer, slot.slot_id, containers[0], slot.image)

    def initial_targets(self) -> tuple[SlotWriterContainer, ...]:
        result = []
        record = self.record()
        for slot in (self.plan.old, self.plan.new):
            for writer in self.plan.writers:
                containers = self.containers(slot, writer_service(writer))
                if len(containers) == 1:
                    try:
                        self.inspect(slot, writer_service(writer), containers[0])
                    except ValueError:
                        if (
                            slot == self.plan.old
                            or record is not None
                            and record.step != "preparing"
                        ):
                            raise
                    else:
                        result.append(
                            SlotWriterContainer(writer, slot.slot_id, containers[0], slot.image)
                        )
                        continue
                if slot == self.plan.old and record is None:
                    raise ValueError("serving slot lacks its native writer topology")
                result.append(
                    SlotWriterContainer(
                        writer,
                        slot.slot_id,
                        "unbound-" + slot.slot_id + "-" + writer_service(writer),
                        slot.image,
                    )
                )
        return tuple(result)

    def runner(self, slot: OdooSlot) -> str:
        containers = self.containers(slot, "script-runner")
        if len(containers) != 1:
            raise ValueError("cutover slot requires one immutable private runner")
        value = self.inspect(slot, "script-runner", containers[0])
        if value["State"].get("Running") is not True or value["State"].get("Restarting"):
            raise ValueError("cutover private runner is unavailable")
        return containers[0]

    def executor_web(self, slot: OdooSlot) -> dict[str, Any]:
        web = next(writer for writer in self.plan.writers if writer.kind == "web")
        value = self.inspect(slot, "web", self.target(slot, web).container)
        startup = slot_startup_command()
        value["startup_commands"] = ["exec " + startup]
        if slot == self.plan.new:
            value["startup_commands"].append(held_slot_command(self.plan, startup))
        else:
            try:
                deployment = self.store.read_deployment_record(
                    slot.runtime_identity.deployment_record_id
                )
            except FileNotFoundError:
                deployment = None
            previous = deployment.odoo_cutover if deployment is not None else None
            if previous is not None and previous.plan.new.runtime_identity == slot.runtime_identity:
                value["startup_commands"].append(held_slot_command(previous.plan, startup))
        return value

    def old_readiness(self) -> None:
        slot = self.plan.old
        observed = json.loads(
            self.transport.read(
                (
                    "docker",
                    "exec",
                    "-i",
                    self.runner(slot),
                    "/venv/bin/python",
                    "-c",
                    READINESS_RELEASE_PROGRAM,
                ),
                json.dumps(
                    {
                        "database": slot.binding.database,
                        "database_uuid": slot.runtime_identity.database_uuid,
                        "credential_commit_id": "",
                        "modules": list(self.runtime.modules),
                    }
                ),
            )
        )
        if observed != {"ready_base": True}:
            raise ValueError("old slot cannot supply its native recovery readiness")

    def startup_capability(self, slot: OdooSlot) -> None:
        result = json.loads(
            self.transport.read(
                (
                    "docker",
                    "exec",
                    "-i",
                    self.runner(slot),
                    "/venv/bin/python",
                    "-c",
                    STARTUP_CAPABILITY_PROGRAM,
                ),
                json.dumps(
                    {"marker": slot_run_marker(self.plan), "stamp": slot_run_stamp(self.plan)}
                ),
            )
        )
        if result != {"scheduler": True}:
            raise ValueError("exact artifact has no native scheduler startup capability")

    def preflight(self, plan: OdooCutoverPlan) -> None:
        if plan != self.plan:
            raise ValueError("host preflight crossed its admitted release")
        self.state.verify_execution(require_existing=self.record() is not None)
        record = self.record()
        self.prepared_server(
            require_running=not (
                record is not None
                and (
                    abort_reconciliation_eligible(record)
                    or maintenance_reconciliation_eligible(record)
                    or publication_recovery_pause(record) is not None
                    or record.step in ODOO_CUTOVER_TERMINAL_STEPS
                )
            )
        )
        for slot in (plan.old, plan.new):
            current = self.payload(slot)
            if (
                current.get("appName") != self.payloads[slot.slot_id].get("appName")
                or current.get("serverId") != self.transport.server_id
                or current.get("domains")
                or current.get("autoDeploy") is not False
            ):
                raise ValueError(
                    "two-slot targets require private, release-owned deployment authority"
                )
        if urlsplit(self.runtime.public_origin).hostname != plan.host:
            raise ValueError("public probe origin differs from the admitted route host")
        profile = self.store.read_product_profile_record(plan.new.binding.product_id)
        if (
            profile.production_use == "live"
            and self.runtime.public_origin != "https://" + plan.host
        ):
            raise ValueError("live cutover requires its public HTTPS origin")
        verify_shared_watched_directory(self.route.target.route_file, self.transport.read)
        observed = self.route.observed_target()
        targets = (plan.old.private_url, plan.new.private_url, self.lane.prepared_origin)
        owned = yaml.safe_load(self.route.target.route_file.read_text())
        if observed not in targets or owned not in [
            json.loads(json.dumps(route_payload(self.route.target, target))) for target in targets
        ]:
            raise ValueError("cutover has no exact single-route file authority")
        if self.record() is None and observed != plan.old.private_url:
            raise ValueError("new cutover does not own the current serving route")
        old_env, new_env = [
            api.parse_dokploy_env_text(str(self.payloads[slot.slot_id].get("env") or ""))
            for slot in (plan.old, plan.new)
        ]
        for key in ("ODOO_DB_HOST", "ODOO_DB_PORT", "ODOO_DB_USER", "ODOO_DB_PASSWORD"):
            if key == "ODOO_DB_PORT":
                agrees = old_env.get(key, "5432") == new_env.get(key, "5432")
            else:
                agrees = key in old_env and old_env.get(key) == new_env.get(key)
            if not agrees:
                raise ValueError("slot database transport does not name one physical database")
        record = self.record()
        recovery_pause = startup_recovery_pause(record) if record else None
        old_addresses: set[str] = set()
        for writer in {
            writer_service(target.writer): target.writer
            for target in self.writers.targets
            if target.slot_id == plan.old.slot_id
        }.values():
            if (
                len(self.containers(plan.old, writer_service(writer))) != 1
                and recovery_pause is not None
            ):
                continue
            value = self.inspect(
                plan.old, writer_service(writer), self.target(plan.old, writer).container
            )
            if self.runtime.private_network not in value["NetworkSettings"]["Networks"] or not any(
                mount.get("Type") == "volume"
                and mount.get("Name") == self.runtime.data_volume
                and mount.get("Destination") == "/volumes/data"
                for mount in value.get("Mounts", [])
            ):
                raise ValueError("serving writers differ from the admitted data/network resources")
            old_addresses.update(container_addresses(value))
        record = self.record()
        if record is None or record.step == "preparing":
            database = self.observer.database()
            if database["prepared_transactions"] or any(
                session.get("address") not in old_addresses for session in database["sessions"]
            ):
                raise ValueError("inactive candidate or unknown database writers are not fenced")
        if record is None or record.step in {
            "preparing",
            "prepared",
            "pausing",
            "backing_up",
            "aborting",
        }:
            self.old_readiness()
        qualified_slot = (
            plan.new
            if record is not None
            and record.step
            in {"maintaining", "ready", "switching", "resuming", "draining", "complete"}
            else plan.old
        )
        self.startup_capability(qualified_slot)
        if (
            qualified_slot == plan.new
            and recovery_pause is not None
            and self.startup_inventory_interrupted()
        ):
            self.verify_recovery_pause()
        else:
            self.native_writer_capability.preflight(qualified_slot)
            for writer in plan.writers:
                self.native_writer_capability(qualified_slot, writer)
        rendered = render_odoo_app_slot_compose(
            plan, data_volume=self.runtime.data_volume, private_network=self.runtime.private_network
        )
        record = self.record()
        if record is None or record.step == "preparing":
            self.startup_reader.preflight_candidate(plan, rendered)
        elif recovery_pause is not None and self.startup_inventory_interrupted():
            self.verify_recovery_pause()
        else:
            self.startup_reader.preflight(plan)

    @contextmanager
    def exclusive(self, plan: OdooCutoverPlan) -> Iterator[None]:
        with api.guard_dokploy_mutations(self.guard), self.exclusive_state(plan):
            yield

    def enter_execution(self, plan: OdooCutoverPlan) -> None:
        if plan != self.plan:
            raise ValueError("host execution crossed its original plan")
        self.guard()
        self.state.admit_execution(recovery=self.record() is not None)
        self.admitted = True
        retained = self.state.authority("retained-assets")
        if retained is None:
            if self.record() is not None:
                raise ValueError("held cutover lacks its original retained asset evidence")
            retained = {}
            for path in self.runtime.retained_assets:
                response = self.probes.private_resource(plan.old.private_url, path)
                if response["status"] != 200:
                    raise ValueError("serving slot cannot supply its retained asset")
                retained[path] = hashlib.sha256(base64.b64decode(response["body"])).hexdigest()
            if self.state.authority("retained-assets", retained) != retained:
                raise ValueError("retained asset evidence has no durable readback")
        record = self.record()
        if (
            record is not None
            and startup_recovery_pause(record) is not None
            and self.startup_inventory_interrupted()
        ):
            self.verify_recovery_guard(plan)
            slot = plan.old if record.step == "rolling_back" else plan.new
            intent = (
                plan.rollback_website_settings
                if record.step == "rolling_back"
                else plan.website_settings
            )
            if intent is None:
                raise ValueError("original startup recovery lacks its admitted website intent")
            self.startup.recover(
                plan,
                slot,
                intent,
                verify_guard=self.verify_recovery_guard,
                active_jobs=self.observer.active_jobs,
            )
        for target in physical_writer_targets(plan, self.writers.targets):
            slot = self.slot(target.slot_id)
            record = self.record()
            if slot == plan.new and (record is None or record.step == "preparing"):
                continue
            containers = self.containers(slot, writer_service(target.writer))
            if len(containers) != 1:
                self.verify_recovery_guard(plan)
                continue
            self.target(slot, target.writer)

    @contextmanager
    def exclusive_state(self, plan: OdooCutoverPlan) -> Iterator[None]:
        try:
            try:
                self.enter_execution(plan)
            except DurableOperationAuthorizationDeniedError as error:
                if self.record() is None:
                    raise CutoverEntryRefusedError(str(error)) from error
                raise
            yield
            try:
                self.prune_prepared()
            except (OSError, ValueError, KeyError):
                logging.getLogger(__name__).warning("Prepared bundle retention remains incomplete.")
        finally:
            self.admitted = False

    def prune_prepared(self) -> None:
        reconstructable: dict[str, PreparedPublicSite] = {}
        protected: set[str] = set()
        for deployment in self.store.list_deployment_records(
            context_name=self.plan.new.runtime_identity.context,
            instance_name=self.plan.new.binding.environment_id,
        ):
            record = deployment.odoo_cutover
            if (
                record is None
                or record.plan.route_id != self.plan.route_id
                or record.public_site is None
            ):
                continue
            verify_snapshot(record.public_site)
            for pause in (record.pause, record.rollback_pause):
                if pause is None:
                    continue
                reconstructable[pause.pause_id] = record.public_site
                if record.step not in ODOO_CUTOVER_TERMINAL_STEPS:
                    protected.add(pause.pause_id)
        self.prepared_store().prune(
            reconstructable,
            protected=protected,
            serving_prepared=self.route.observed_target() == self.lane.prepared_origin,
        )

    def prepare_candidate(self, plan: OdooCutoverPlan) -> None:
        self.guard()
        dump_version = self.transport.read(
            ("docker", "exec", self.runner(plan.old), "pg_dump", "--version"), None
        )
        dump_major = int(dump_version.split()[2].split(".")[0])
        tools = json.loads(
            self.transport(
                ("python3", "-c", RESTORE_PREFLIGHT_PROGRAM),
                json.dumps(
                    {
                        "image": self.runtime.restore_image,
                        "dump_major": dump_major,
                        "timeout_seconds": self.runtime.operation_timeout_seconds,
                    }
                ),
            )
        )
        if tools != {"restore_tools": True}:
            raise ValueError("immutable restore tools lack qualified native preflight")
        prepare_dokploy_odoo_slot(
            plan,
            host=self.host,
            token=self.token,
            data_volume=self.runtime.data_volume,
            private_network=self.runtime.private_network,
        )
        self.writers.targets = tuple(
            self.target(slot, writer) for slot in (plan.old, plan.new) for writer in plan.writers
        )
        self.startup_capability(plan.new)
        self.writers.fence_candidate()
        self.preflight(plan)
        self.native_writer_capability.preflight(plan.new)

    def prepare_public(self, plan: OdooCutoverPlan) -> PreparedPublicSite:
        self.guard()
        self.probes.private_health(plan.old)
        public_plan = PublicSitePlan(
            binding=plan.old.binding,
            origin=self.runtime.public_origin,
            public_routes=self.runtime.public_routes,
            retained_assets=self.runtime.retained_assets,
            excluded_prefixes=self.runtime.excluded_prefixes,
            writers=plan.writers,
            notice=self.runtime.notice,
        )
        probes = self.probes

        class Capture:
            def capture(self, path: str) -> CapturedPublicResponse:
                response = probes.private_resource(plan.old.private_url, path)
                return CapturedPublicResponse(
                    binding=plan.old.binding,
                    anonymous=True,
                    public=True,
                    status=response["status"],
                    content_type=response["headers"].get("content-type", ""),
                    body=base64.b64decode(response["body"]),
                    headers=response["headers"],
                )

        site = prepare_public_site(public_plan, Capture())
        self.probes.private_health(plan.old)
        return site

    def prepared_store(self) -> PreparedSiteDirectory:
        return PreparedSiteDirectory(self.prepared_path)

    def prepared_server(self, *, require_running: bool = True) -> None:
        require_prepared_server(self.runtime, self.transport.read, require_running=require_running)

    def stage_public(self, site: PreparedPublicSite, pause_id: str) -> str:
        self.guard()
        self.prepared_server()
        if (
            pause_id
            not in {
                cutover_record_id(self.plan),
                rollback_pause_identity(cutover_record_id(self.plan)),
            }
            or site.plan.binding != self.plan.old.binding
        ):
            raise ValueError("prepared bundle crossed its original pause")
        self.prepared_path.mkdir(mode=0o755, parents=False, exist_ok=True)
        self.prepared_store().publish(site, pause_id)
        for resource in site.resources:
            observed = self.probes.private_resource(self.lane.prepared_origin, resource.path)
            if (
                observed["status"] != resource.status
                or base64.b64decode(observed["body"]) != base64.b64decode(resource.body_base64)
                or observed["headers"].get("x-launchplane-public-copy") != site.content_digest
                or observed["headers"].get("x-launchplane-pause-id") != pause_id
            ):
                raise ValueError("prepared server did not read the staged immutable bytes")
        self.probes.browser(
            self.lane.prepared_origin,
            tuple((path, site.plan.notice) for path in site.plan.public_routes),
            notice=site.plan.notice,
        )
        return self.lane.prepared_origin

    def load_prepared(self, pause_id: str) -> PreparedPublicSite:
        return self.prepared_store().load(pause_id)

    def owns_prepared(self, pause_id: str) -> bool:
        record = self.record()
        if record is None or record.public_site is None:
            return False
        pause = (
            record.pause
            if backup_recovery_eligible(record) or maintenance_reconciliation_eligible(record)
            else publication_recovery_pause(record)
        )
        if pause is None or pause.pause_id != pause_id:
            return False
        directory = self.prepared_store()
        return (
            directory.active_pause() == pause_id
            and directory.load(pause_id) == record.public_site
            and self.route.observed_target() == self.lane.prepared_origin
            and yaml.safe_load(self.route.target.route_file.read_text())
            == json.loads(json.dumps(route_payload(self.route.target, self.lane.prepared_origin)))
        )

    def observe_prepared(self, pause_id: str) -> str:
        store = self.prepared_store()
        if store.active_pause() != pause_id:
            return ""
        site = store.load(pause_id)
        resource = self.probes.private_resource(
            self.lane.prepared_origin, site.plan.public_routes[0]
        )
        return (
            site.content_digest
            if resource["headers"].get("x-launchplane-public-copy") == site.content_digest
            and resource["headers"].get("x-launchplane-pause-id") == pause_id
            and resource["status"] in {200, 301, 302, 307, 308}
            else ""
        )

    def observe_public(
        self, plan: OdooCutoverPlan, target: str
    ) -> tuple[str, PublicSiteBinding, HealthcheckEvidence | None]:
        for slot in (plan.old, plan.new):
            if target == slot.private_url:
                return (
                    slot.slot_id,
                    slot.binding,
                    self.probes.public_health(self.runtime.public_origin, slot),
                )
        record = self.record()
        if (
            target == self.lane.prepared_origin
            and record is not None
            and record.public_site is not None
        ):
            for pause_id in (
                cutover_record_id(plan),
                rollback_pause_identity(cutover_record_id(plan)),
            ):
                if self.observe_prepared(pause_id) == record.public_site.content_digest:
                    resource = self.probes.public_resource(
                        self.runtime.public_origin, record.public_site.plan.public_routes[0]
                    )
                    if (
                        resource["headers"].get("x-launchplane-public-copy")
                        == record.public_site.content_digest
                        and resource["headers"].get("x-launchplane-pause-id") == pause_id
                    ):
                        return "prepared", plan.old.binding, None
        raise ValueError("public observation has no bound applied runtime")

    def maintenance_checkpoint(self, receipt: OdooMaintenanceReceipt) -> None:
        self.guard()
        deployment = self.store.read_deployment_record(
            self.plan.new.runtime_identity.deployment_record_id
        )
        record = deployment.odoo_cutover
        if (
            record is None
            or record.plan != self.plan
            or receipt.binding != self.plan.new.binding
            or receipt.evidence_id != payload_digest(maintenance_payload(self.plan))
        ):
            raise ValueError("startup checkpoint has no original passing maintenance receipt")
        self.store.write_deployment_record(
            deployment.model_copy(
                update={"odoo_cutover": record.model_copy(update={"maintenance": receipt})}
            )
        )

    def verify_recovery_guard(self, plan: OdooCutoverPlan) -> None:
        self.guard()
        if plan != self.plan:
            raise ValueError("startup recovery crossed its original plan")
        self.verify_recovery_pause()

    def verify_recovery_pause(self) -> None:
        record = self.record()
        pause = startup_recovery_pause(record) if record else None
        if record is None or record.public_site is None or pause is None:
            raise ValueError("startup recovery lacks its original maintenance and public pause")
        try:
            observed = self.observe_prepared(pause.pause_id)
        except (ValueError, OSError):
            observed = ""
        if (
            self.route.observed_target() != self.lane.prepared_origin
            or observed != record.public_site.content_digest
            and not self.owns_prepared(pause.pause_id)
        ):
            raise ValueError("startup recovery requires its original published drained pause")

    def configure_startup(
        self, plan: OdooCutoverPlan, slot: OdooSlot, intent: OdooWebsiteSettingsIntent
    ) -> StartupSettingsReadback:
        self.guard()
        recovery = self.startup_inventory_interrupted()
        if recovery:
            self.verify_recovery_guard(plan)
            return self.startup.recover(
                plan,
                slot,
                intent,
                verify_guard=self.verify_recovery_guard,
                active_jobs=self.observer.active_jobs,
            )
        return self.startup(plan, slot, intent)

    def startup_inventory_interrupted(self) -> bool:
        return any(
            len(self.containers(current, service)) != 1
            for current in (self.plan.old, self.plan.new)
            for service in {writer_service(writer) for writer in self.plan.writers}
        )

    def readiness(self, plan: OdooCutoverPlan, slot: OdooSlot) -> OdooSlotReadiness:
        self.guard()
        record = self.record()
        if record is None or slot == plan.new and record.maintenance is None:
            raise ValueError("native readiness lacks the passing original maintenance")
        self.native_writer_capability.preflight(slot)
        targets = tuple(
            target
            for target in self.writers.targets
            if target.slot_id == slot.slot_id and target.writer.kind != "web"
        )
        for target in {target.container: target for target in targets}.values():
            self.writers._stop(target)
        self.writers.require_drained(targets)
        web_writer = next(writer for writer in plan.writers if writer.kind == "web")
        web = self.target(slot, web_writer)
        if not self.writers._running(web):
            self.transport(("docker", "start", web.container), None)
        started = time.monotonic()
        started_at = datetime.now(UTC).isoformat()
        deadline = started + self.runtime.readiness_timeout_seconds
        attempts = 0
        while True:
            attempts += 1
            try:
                self.probes.private_health(slot)
                break
            except (ValueError, OSError) as error:
                now = time.monotonic()
                if now >= deadline:
                    failure: dict[str, object] = {
                        "kind": "io_unavailable"
                        if isinstance(error, OSError)
                        else "invalid_readback"
                    }
                    if isinstance(error, NativeSourceUnavailable):
                        failure = {"kind": "transport_unavailable"}
                    if isinstance(error, (PrivateHealthFailed, CutoverCommandFailed)):
                        failure = {"kind": error.kind}
                        if isinstance(error, PrivateHealthFailed):
                            failure["status"] = error.status
                        else:
                            failure["returncode"] = error.returncode
                    diagnostic = {
                        "kind": "readiness_deadline_exceeded",
                        "stage": "private_health",
                        "started_at": started_at,
                        "attempts": attempts,
                        "elapsed_seconds": round(now - started, 3),
                        "timeout_seconds": self.runtime.readiness_timeout_seconds,
                        "failure": failure,
                        "containers": readiness_container_states(
                            self.transport, web.container, lambda: self.runner(plan.old)
                        ),
                    }
                    raise ValueError(json.dumps(diagnostic, sort_keys=True)) from None
                time.sleep(1)
        self.observer.native_ready(web)
        fenced = all(not self.writers._running(target) for target in targets)
        if not fenced:
            raise ValueError("candidate background side effects remain unfenced")
        identity = slot.runtime_identity.model_dump(mode="json")
        identity["source_revision"] = identity.pop("source_git_ref")
        maintenance = record.maintenance
        release = {
            "runtime_identity": identity,
            "update_id": maintenance.evidence_id
            if maintenance
            else slot.runtime_identity.deployment_record_id,
            "updated_modules": list(maintenance.update_modules)
            if maintenance and slot == plan.new
            else [],
            "status": "pass",
            "fences": {
                "credentials_committed": True,
                "mail_disabled": fenced,
                "integrations_disabled": fenced,
            },
        }
        request = {
            "database": slot.binding.database,
            "database_uuid": slot.runtime_identity.database_uuid,
            "credential_commit_id": maintenance.credential_commit_id if maintenance else "",
            "modules": list(self.runtime.modules),
            "release": release,
        }
        observed = json.loads(
            self.transport(
                (
                    "docker",
                    "exec",
                    "-i",
                    self.runner(slot),
                    "/venv/bin/python",
                    "-c",
                    READINESS_RELEASE_PROGRAM,
                ),
                json.dumps(request),
            )
        )
        if observed != {"release": release}:
            raise ValueError("native readiness marker was not committed")
        contract = {
            "schema_version": 1,
            "database": slot.binding.database,
            "database_uuid": slot.runtime_identity.database_uuid,
            "host": plan.host,
            "website_id": self.runtime.website_id,
            "runtime_identity": identity,
            "modules": list(self.runtime.modules),
            "release": release,
            "routes": [route.model_dump(mode="json") for route in self.runtime.readiness_routes],
        }
        browser = self.probes.browser(
            slot.private_url,
            tuple((route.path, route.contains) for route in self.runtime.readiness_routes),
            contract=contract,
        )
        previous = self.state.authority("retained-assets")
        if previous is None or set(previous) != set(self.runtime.retained_assets):
            raise ValueError("native readiness lacks original retained asset coverage")
        retained = True
        for path, digest in previous.items():
            response = self.probes.private_resource(slot.private_url, path)
            if (
                response["status"] != 200
                or hashlib.sha256(base64.b64decode(response["body"])).hexdigest() != digest
            ):
                retained = False
        return verify_odoo_slot_readiness(
            plan,
            slot,
            web_container=web.container,
            contract=contract,
            browser_receipt=browser,
            execute=self.transport,
            retained_assets_verified=retained,
            writers_fenced=fenced,
        )

    def drain_old(self, plan: OdooCutoverPlan) -> bool:
        self.guard()
        for target in physical_writer_targets(plan, self.writers.targets):
            if target.slot_id == plan.old.slot_id:
                self.writers._stop(target)
        return all(self.observer.active_jobs(plan.old, writer) == 0 for writer in plan.writers)

    def backup(self, plan: OdooCutoverPlan) -> OdooRecoveryTuple:
        self.guard()
        record = self.record()
        if record is None:
            raise ValueError("coherent backup requires its persisted original cutover")
        promotion_id = "promotion-cutover-" + cutover_record_id(plan)
        admissions = [
            item
            for item in self.store.list_promotion_records(
                context_name=plan.new.runtime_identity.context,
                to_instance_name=plan.new.binding.environment_id,
            )
            if item.record_id == promotion_id
        ]
        if len(admissions) != 1:
            raise ValueError("coherent backup lacks its admitted infrastructure guard")
        infrastructure_id = admissions[0].backup_gate.evidence.get(
            "infrastructure_backup_record_id", ""
        )
        require_production_promotion_backup(
            record_store=self.store,
            product=plan.new.binding.product_id,
            context=plan.new.runtime_identity.context,
            instance=plan.new.binding.environment_id,
            promotion_action=ODOO_PROMOTION_BACKUP_ACTION,
            backup_record_id=infrastructure_id,
            active_promotion_record_id=promotion_id,
            cutover_recovery=record,
        )
        backup_id = cutover_record_id(plan) + "-online"
        binding = self.state.authority("backup")
        original_attempt = binding is not None
        if binding is None:
            binding = {
                "record_id": backup_id,
                "nonce": secrets.token_hex(32),
                "verification_nonce": secrets.token_hex(32),
            }
            if self.state.authority("backup", binding) != binding:
                raise ValueError("coherent backup intent has no durable readback")
        directory = (
            self.runtime.backup_root.rstrip("/") + "/" + plan.old.binding.database + "/" + backup_id
        )
        manifest = json.loads(
            self.transport.read(
                (
                    "docker",
                    "exec",
                    "-i",
                    self.runner(plan.old),
                    "/venv/bin/python",
                    "-c",
                    BACKUP_MANIFEST_PROGRAM,
                ),
                json.dumps({"directory": directory}),
            )
        )
        if manifest.get("state") == "captured":
            if not original_attempt or manifest.get("claim") != {
                "record_id": backup_id,
                "nonce": binding["nonce"],
            }:
                raise ValueError("captured backup does not belong to the original admitted attempt")
            captured = {
                key: manifest["manifest"][key] for key in OdooProdBackupCaptureEvidence.model_fields
            }
        elif (
            manifest.get("state") == "failed"
            and original_attempt
            and manifest.get("claim") == {"record_id": backup_id, "nonce": binding["nonce"]}
        ):
            raise BackupVerificationRejected("original native capture terminated unsuccessfully")
        elif manifest.get("state") != "absent":
            raise ValueError("original coherent capture is uncertain; it cannot be repeated")
        else:
            script = post_deploy._build_dokploy_odoo_backup_gate_script(
                compose_app_name=str(self.payloads[plan.old.slot_id]["appName"]),
                backup_nonce=binding["nonce"],
                database_name=plan.old.binding.database,
                filestore_path=self.runtime.filestore_path,
                backup_root=self.runtime.backup_root,
                backup_record_id=backup_id,
                expected_web_state="exited" if record.pause else "running",
                cutover_capture_claim=True,
            )
            captured = post_deploy.extract_odoo_backup_gate_result(
                self.transport(("bash", "-s"), script)
            )
        capture = OdooProdBackupCaptureEvidence.model_validate(captured)
        image_id = self.transport.read(
            (
                "docker",
                "inspect",
                "--format",
                "{{.Image}}",
                self.target(
                    plan.old, next(writer for writer in plan.writers if writer.kind == "web")
                ).container,
            ),
            None,
        ).strip()
        if (
            capture.backup_record_id != backup_id
            or capture.backup_nonce != binding["nonce"]
            or capture.database_name != plan.old.binding.database
            or capture.image_id != image_id
        ):
            raise ValueError("coherent capture differs from its original image/database request")
        verification_script = post_deploy._build_dokploy_odoo_backup_verification_script(
            compose_app_name=str(self.payloads[plan.old.slot_id]["appName"]),
            verification_nonce=binding["verification_nonce"],
            backup_record_id=backup_id,
            database_name=plan.old.binding.database,
            filestore_path=self.runtime.filestore_path,
            backup_dir=directory,
            database_dump_path=directory + "/" + plan.old.binding.database + ".dump",
            filestore_archive_path=directory
            + "/"
            + plan.old.binding.database
            + "-filestore.tar.gz",
            manifest_path=directory + "/manifest.json",
            capture_evidence={
                key: str(value)
                for key, value in capture.model_dump().items()
                if key != "backup_nonce"
            },
        )
        verified = post_deploy.extract_odoo_backup_verification_result(
            self.transport(("bash", "-s"), verification_script)
        )
        if (
            verified.pop("verification_nonce", None) != binding["verification_nonce"]
            or verified.pop("backup_record_id", None) != backup_id
            or verified.pop("database_name", None) != plan.old.binding.database
        ):
            raise ValueError("coherent verification differs from its original capture")
        verification = OdooProdBackupVerificationEvidence.model_validate(verified)
        if (
            verification.verification_status != "pass"
            or verification.database_dump_sha256 != capture.database_dump_sha256
            or verification.filestore_archive_sha256 != capture.filestore_archive_sha256
        ):
            self.cancel_backup_restore(plan)
            raise BackupVerificationRejected(
                "coherent tuple integrity/restore-read verification failed"
            )
        restored = verify_disposable_database_restore(
            self.transport,
            image=self.runtime.restore_image,
            runner=self.runner(plan.old),
            record_id=backup_id,
            dump=directory + "/" + plan.old.binding.database + ".dump",
            database_uuid=plan.old.runtime_identity.database_uuid,
            dump_sha256=capture.database_dump_sha256,
            database=plan.old.binding.database,
            filestore=directory + "/" + plan.old.binding.database + "-filestore.tar.gz",
            filestore_sha256=capture.filestore_archive_sha256,
            timeout_seconds=self.runtime.operation_timeout_seconds,
            authority_directory=self.runtime.authority_directory,
            capture_nonce=binding["nonce"],
        )
        self.guard()
        backup_record = BackupGateRecord(
            record_id=backup_id,
            context=plan.old.runtime_identity.context,
            instance=plan.old.binding.environment_id,
            created_at=capture.recovery_point_at,
            source="launchplane-odoo-cutover-backup",
            status="pass",
            evidence={
                "cutover_record_id": cutover_record_id(plan),
                "infrastructure_backup_record_id": infrastructure_id,
                **{
                    key: str(value)
                    for key, value in capture.model_dump().items()
                    if key != "backup_nonce"
                },
                **{key: str(value) for key, value in verification.model_dump().items()},
                **restored,
            },
        )
        self.store.write_backup_gate_record(backup_record)
        if self.store.read_backup_gate_record(backup_id) != backup_record:
            raise ValueError(
                "coherent backup has no durable capture and archive-verification readback"
            )
        return OdooRecoveryTuple(
            binding=plan.old.binding,
            image=plan.old.image,
            logical_record_id=backup_id,
            infrastructure_record_id=infrastructure_id,
            database_dump_sha256=capture.database_dump_sha256,
            filestore_archive_sha256=capture.filestore_archive_sha256,
            recovery_point_at=datetime.fromisoformat(capture.recovery_point_at),
            coherence_verified=True,
            restore_verified=True,
            independent_backup_verified=True,
        )

    def cancel_backup_restore(self, plan: OdooCutoverPlan) -> None:
        self.guard()
        record = self.record()
        if plan != self.plan or record is None or not backup_recovery_eligible(record):
            raise ValueError("backup cancellation lacks its original pre-maintenance operation")
        backup_id = cutover_record_id(plan) + "-online"
        binding = self.state.authority("backup")
        if binding is None:
            binding = self.state.authority(
                "backup",
                {
                    "record_id": backup_id,
                    "nonce": secrets.token_hex(32),
                    "verification_nonce": secrets.token_hex(32),
                },
            )
        if binding is None or binding.get("record_id") != backup_id:
            raise ValueError("backup cancellation lacks its original capture intent")
        directory = (
            self.runtime.backup_root.rstrip("/") + "/" + plan.old.binding.database + "/" + backup_id
        )
        manifest = json.loads(
            self.transport(
                (
                    "docker",
                    "exec",
                    "-i",
                    self.runner(plan.old),
                    "/venv/bin/python",
                    "-c",
                    BACKUP_MANIFEST_PROGRAM,
                ),
                json.dumps(
                    {
                        "directory": directory,
                        "cancel_absent": {"record_id": backup_id, "nonce": binding["nonce"]},
                    }
                ),
            )
        )
        if manifest.get("claim") != {"record_id": backup_id, "nonce": binding["nonce"]}:
            raise ValueError("backup cancellation lacks its exact native capture claim")
        if manifest.get("state") == "failed":
            return
        if manifest.get("state") != "captured":
            raise ValueError("live or unknown native capture cannot be cancelled")
        capture = OdooProdBackupCaptureEvidence.model_validate(
            {key: manifest["manifest"][key] for key in OdooProdBackupCaptureEvidence.model_fields}
        )
        if (
            capture.backup_record_id != backup_id
            or capture.backup_nonce != binding["nonce"]
            or capture.database_name != plan.old.binding.database
        ):
            raise ValueError("backup cancellation crossed its original captured tuple")
        verify_disposable_database_restore(
            self.transport,
            image=self.runtime.restore_image,
            runner=self.runner(plan.old),
            record_id=backup_id,
            dump=directory + "/" + plan.old.binding.database + ".dump",
            database_uuid=plan.old.runtime_identity.database_uuid,
            dump_sha256=capture.database_dump_sha256,
            database=plan.old.binding.database,
            filestore=directory + "/" + plan.old.binding.database + "-filestore.tar.gz",
            filestore_sha256=capture.filestore_archive_sha256,
            timeout_seconds=self.runtime.operation_timeout_seconds,
            authority_directory=self.runtime.authority_directory,
            capture_nonce=binding["nonce"],
            cancel_only=True,
        )

    def capabilities(self) -> OdooHostCapabilities:
        driver = OdooSlotDriver(
            exclusive=self.exclusive,
            prepare_candidate=self.prepare_candidate,
            prepare_public=self.prepare_public,
            backup=self.backup,
            maintenance=None,
            readiness=self.readiness,
            observe_public=self.observe_public,
            stage_public=self.stage_public,
            observe_prepared=self.observe_prepared,
            fence_and_drain=self.writers.fence,
            handoff_writers=lambda plan, slot: self.writers.handoff(slot),
            drain_old=self.drain_old,
            load_prepared=self.load_prepared,
            owns_prepared=self.owns_prepared,
            cancel_backup_restore=self.cancel_backup_restore,
            lane=self.lane,
            verify_endpoint=lambda plan, slot: self.probes.private_health(slot),
        )
        return OdooHostCapabilities(
            driver=driver,
            writers=self.writers,
            execute=self.transport,
            runner=self.runner,
            startup=self.configure_startup,
            startup_preflight=self.preflight,
            maintenance_checkpoint=self.maintenance_checkpoint,
        )
