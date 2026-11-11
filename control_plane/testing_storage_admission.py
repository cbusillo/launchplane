"""Refuse a migrated testing restore at admission when the receiver could not hold it."""

from dataclasses import dataclass
from pathlib import Path

from control_plane._odoo_snapshot_files import CLEAN_SNAPSHOT_STATE_DIRECTORY
from control_plane.contracts.odoo_testing_drill import OdooTestingDrillStorageBudget
from control_plane.contracts.odoo_testing_source import MigratedOdooTestingSource
from control_plane.dokploy import source
from control_plane.dokploy.native_copy_stream import run_managed_server_program
from control_plane.testing_storage_capacity import storage_capacity_requirements

TESTING_STORAGE_CAPACITY_INSUFFICIENT = "testing_storage_capacity_insufficient"
TESTING_STORAGE_CAPACITY_UNAVAILABLE = "testing_storage_capacity_unavailable"
VOLUME_ROLES = ("database", "data", "data_staging")

TESTING_BACKING_READ_PROGRAM = (
    "CLEAN_SNAPSHOT_STATE_DIRECTORY = "
    + repr(CLEAN_SNAPSHOT_STATE_DIRECTORY)
    + r"""
import base64,json,os,subprocess,sys
from pathlib import Path

prefix=sys.argv[2]

def zfs_pools():
    pools={}
    for mount in Path('/proc/self/mountinfo').read_text().splitlines():
        backing,filesystem=mount.split(' - ',1)
        fields=filesystem.split()
        if fields[0]=='zfs':
            major,minor=backing.split()[2].split(':')
            pools[os.makedev(int(major),int(minor))]=fields[1].split('/')[0]
    return pools

def measured(path,pools):
    path=path.resolve(strict=True)
    device=path.stat().st_dev
    value=os.statvfs(path)
    backing='zfs:'+pools[device] if device in pools else 'device:'+str(device)
    return {'backing':backing,'available':value.f_bavail*value.f_frsize}

def existing_ancestor(path):
    while not path.exists():
        path=path.parent
    return path

try:
    spec=json.loads(base64.b64decode(sys.argv[1],validate=True))
    info=json.loads(subprocess.run(['docker','info','--format','{{json .}}'],
        capture_output=True,check=True,timeout=30).stdout)
    if info.get('Driver')!='overlay2':
        raise RuntimeError('Writable-layer backing is unavailable')
    root=Path(info['DockerRootDir'])
    if not root.is_absolute():
        raise RuntimeError('Docker backing is unavailable')
    pools=zfs_pools()
    roles={'volumes':measured(root/'volumes',pools),'writable':measured(root/'overlay2',pools)}
    if spec.get('clean_snapshot') is True:
        roles['snapshot']=measured(existing_ancestor(Path.home()/CLEAN_SNAPSHOT_STATE_DIRECTORY),pools)
    print(prefix+json.dumps({'type':'complete','roles':roles}),flush=True)
except Exception:
    print(prefix+json.dumps({'type':'failed'}),flush=True)
"""
)


class TestingStorageCapacityRefused(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class BackingMeasurement:
    backing: str
    available: int


@dataclass(frozen=True)
class CapacityShortfall:
    required: int
    available: int

    @property
    def missing(self) -> int:
        return self.required - self.available


def migrated_storage_budget(
    lane_budget: OdooTestingDrillStorageBudget, migrated: MigratedOdooTestingSource
) -> OdooTestingDrillStorageBudget:
    return lane_budget.model_copy(
        update={
            "database_bytes": max(lane_budget.database_bytes, migrated.database.size),
            "filestore_bytes": max(lane_budget.filestore_bytes, migrated.filestore_expanded_bytes),
        }
    )


def migrated_restore_demand(
    budget: OdooTestingDrillStorageBudget,
    migrated: MigratedOdooTestingSource,
    *,
    clean_snapshot: bool,
) -> dict[str, int]:
    staged_pair = migrated.database.size + migrated.filestore.size
    demand = {
        "database": budget.database_bytes,
        "data": budget.filestore_bytes,
        "data_staging": staged_pair,
        "writable": migrated.database.size,
    }
    if clean_snapshot:
        demand["snapshot"] = budget.database_bytes + budget.filestore_bytes
    return demand


def worst_shortfall(
    demand: dict[str, int], roles: dict[str, BackingMeasurement], reserve: int
) -> CapacityShortfall | None:
    backings: dict[str, str] = {}
    available: dict[str, int] = {}
    for role in demand:
        measurement = roles["volumes" if role in VOLUME_ROLES else role]
        if type(measurement.available) is not int or measurement.available < 0:
            raise ValueError("Testing storage capacity is unavailable")
        backings[role] = measurement.backing
        available[measurement.backing] = min(
            available.get(measurement.backing, measurement.available), measurement.available
        )
    required = storage_capacity_requirements(
        demand, backings, reserve, staging_cleanup_before_snapshot=True
    )
    shortfalls = [
        CapacityShortfall(required=size, available=available[backing])
        for backing, size in required.items()
        if size > available[backing]
    ]
    return max(shortfalls, key=lambda item: item.missing, default=None)


def read_testing_backings(
    root: Path, database_url: str, server_id: str, *, clean_snapshot: bool
) -> dict[str, BackingMeasurement]:
    host, token = source.read_dokploy_config(control_plane_root=root, database_url=database_url)
    frames: list[dict[str, object]] = []
    run_managed_server_program(
        host=host,
        token=token,
        server_id=server_id,
        program=TESTING_BACKING_READ_PROGRAM,
        payload={"clean_snapshot": clean_snapshot},
        receive=frames.append,
        timeout_seconds=60,
    )
    if len(frames) != 1 or frames[0].get("type") != "complete":
        raise ValueError("Testing storage backing read is incomplete.")
    roles = frames[0].get("roles")
    expected = {"volumes", "writable"} | ({"snapshot"} if clean_snapshot else set())
    if not isinstance(roles, dict) or set(roles) != expected:
        raise ValueError("Testing storage backing read is malformed.")
    return {
        role: BackingMeasurement(backing=str(value["backing"]), available=int(value["available"]))
        for role, value in roles.items()
    }


def require_migrated_restore_capacity(
    roles: dict[str, BackingMeasurement],
    lane_budget: OdooTestingDrillStorageBudget,
    migrated: MigratedOdooTestingSource,
    *,
    clean_snapshot: bool,
) -> None:
    budget = migrated_storage_budget(lane_budget, migrated)
    demand = migrated_restore_demand(budget, migrated, clean_snapshot=clean_snapshot)
    try:
        shortfall = worst_shortfall(demand, roles, budget.reserve_bytes)
    except (KeyError, ValueError, TypeError) as error:
        raise TestingStorageCapacityRefused(
            TESTING_STORAGE_CAPACITY_UNAVAILABLE,
            "Testing storage backing or capacity is unavailable for this migrated restore.",
        ) from error
    if shortfall is None:
        return
    raise TestingStorageCapacityRefused(
        TESTING_STORAGE_CAPACITY_INSUFFICIENT,
        f"Testing storage is {shortfall.missing} bytes short for this migrated restore: "
        f"it needs {shortfall.required} bytes including the reserve, and "
        f"{shortfall.available} bytes are free.",
    )
