import hashlib
import json
from pathlib import Path
from typing import Any

from control_plane.contracts.odoo_cutover import OdooCutoverPlan, OdooSlot
from control_plane.contracts.prepared_public_site import WriterTarget
from control_plane.dokploy.slot_maintenance import (
    ContainerCommand,
    maintenance_payload,
    payload_digest,
    slot_run_directory,
    slot_run_stamp,
)
from control_plane.dokploy.slot_startup_authority import AUTHORITY_PROGRAM, plan_digest
from control_plane.dokploy.cutover_writer_observer import container_addresses
from control_plane.odoo_cutover_plan import cutover_record_id


ADDRESS_HISTORY_PROGRAM = r"""
import json,os,sys,tempfile
from pathlib import Path
spec=json.load(sys.stdin); root=Path(spec['directory'])
if not root.is_absolute() or root.resolve()!=root or not root.is_dir(): raise ValueError('host evidence directory is unavailable')
path=root/(spec['record']+'-addresses.json')
if path.is_symlink(): raise ValueError('host address evidence cannot be a symlink')
value=json.loads(path.read_text()) if path.exists() else {'plan':spec['plan'],'addresses':{}}
if value['plan']!=spec['plan']: raise ValueError('host address evidence crossed operations')
if 'add' in spec:
    for key,addresses in spec['add'].items():
        value['addresses'][key]=sorted(set(value['addresses'].get(key,[]))|set(addresses))
    descriptor,temporary=tempfile.mkstemp(dir=root,prefix='.cutover-addresses-')
    try:
        with os.fdopen(descriptor,'w') as stream:
            json.dump(value,stream,sort_keys=True);stream.flush();os.fsync(stream.fileno())
        os.replace(temporary,path)
        descriptor=os.open(root,os.O_RDONLY|os.O_DIRECTORY)
        try: os.fsync(descriptor)
        finally: os.close(descriptor)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)
print(json.dumps(value,sort_keys=True))
"""


def writer_service(writer: WriterTarget) -> str:
    return "web" if writer.kind == "web" else "scheduler"


class CutoverHostState:
    def __init__(
        self,
        plan: OdooCutoverPlan,
        directory: str,
        reader: ContainerCommand,
        execute: ContainerCommand,
        placement: dict[str, Any] | None = None,
    ) -> None:
        self.plan, self.directory, self.reader, self.execute = plan, directory, reader, execute
        self.record_id = cutover_record_id(plan)
        self.placement = placement or {}
        self.address_history: dict[str, list[str]] = {}

    def authority(self, key: str, desired: dict[str, Any] | None = None) -> dict[str, Any] | None:
        request: dict[str, Any] = {
            "directory": self.directory,
            "digest": self.record_id + "-" + key,
        }
        command = self.reader
        if desired is not None:
            request["authority"] = desired
            command = self.execute
        value = json.loads(command(("python3", "-c", AUTHORITY_PROGRAM), json.dumps(request)))
        if value is not None and not isinstance(value, dict):
            raise ValueError("original host execution evidence is malformed")
        return value

    def expected_execution(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "placement": self.placement,
            "plan": plan_digest(self.plan),
            "maintenance": payload_digest(maintenance_payload(self.plan)),
            "run_directory": slot_run_directory(self.plan),
            "run_stamp": slot_run_stamp(self.plan),
        }

    def verify_execution(self, *, require_existing: bool) -> None:
        observed = self.authority("execution")
        if observed is None and require_existing:
            raise ValueError("held cutover has no original host execution evidence")
        if observed is not None and observed != self.expected_execution():
            raise ValueError(
                "package changed the original maintenance execution; keep the cutover held"
            )

    def admit_execution(self, *, recovery: bool) -> None:
        self.verify_execution(require_existing=recovery)
        expected = self.expected_execution()
        if self.authority("execution", expected) != expected:
            raise ValueError("original host execution evidence was not read back")
        self._addresses()

    def _addresses(self, add: dict[str, list[str]] | None = None) -> None:
        request: dict[str, Any] = {
            "directory": self.directory,
            "record": self.record_id,
            "plan": plan_digest(self.plan),
        }
        command = self.reader
        if add is not None:
            request["add"] = add
            command = self.execute
        value = json.loads(command(("python3", "-c", ADDRESS_HISTORY_PROGRAM), json.dumps(request)))
        if value.get("plan") != plan_digest(self.plan) or not isinstance(
            value.get("addresses"), dict
        ):
            raise ValueError("host address evidence does not match the original operation")
        self.address_history = value["addresses"]

    def observe_address(
        self, slot: OdooSlot, writer: WriterTarget, container: dict[str, Any]
    ) -> None:
        addresses = container_addresses(container)
        key = slot.slot_id + ":" + writer_service(writer)
        if addresses and not addresses <= set(self.address_history.get(key, ())):
            self._addresses({key: sorted(addresses)})

    def addresses(self, slot: OdooSlot, writer: WriterTarget) -> set[str]:
        return set(self.address_history.get(slot.slot_id + ":" + writer_service(writer), ()))


def verify_shared_watched_directory(route_file: Path, reader: ContainerCommand) -> None:
    program = "import hashlib,json,sys; from pathlib import Path; spec=json.load(sys.stdin); path=Path(spec['path']); metadata=path.stat(); print(json.dumps({'boot_id':Path('/proc/sys/kernel/random/boot_id').read_text().strip(),'device':metadata.st_dev,'inode':metadata.st_ino,'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}))"
    try:
        metadata = route_file.stat()
        expected = {
            "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
            "device": metadata.st_dev,
            "inode": metadata.st_ino,
            "sha256": hashlib.sha256(route_file.read_bytes()).hexdigest(),
        }
    except OSError as error:
        raise ValueError(
            "cutover host cannot prove its co-located watched-directory mount"
        ) from error
    observed = json.loads(reader(("python3", "-c", program), json.dumps({"path": str(route_file)})))
    if observed != expected:
        raise ValueError("API/worker route mount does not share the admitted provider filesystem")
