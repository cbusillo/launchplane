import inspect
import json
from pathlib import Path

import click
from sqlalchemy.exc import SQLAlchemyError

from control_plane.dokploy import native_copy_stream
from control_plane.runtime_environments import RuntimeEnvironmentRecordStore
from control_plane.storage.factory import resolve_database_url
from control_plane.storage.postgres import PostgresRecordStore

PREVIEW_MEMORY_FLOORS_KEY = "LAUNCHPLANE_PREVIEW_HOST_MEMORY_FLOORS"


class PreviewMemoryCapacityRefused(ValueError):
    pass


def observe_memory_headroom(proc_root: Path, cgroup_root: Path) -> int:
    memory = {}
    for line in (proc_root / "meminfo").read_text().splitlines():
        fields = line.split()
        if len(fields) == 3 and fields[2] == "kB":
            memory[fields[0].removesuffix(":")] = int(fields[1]) * 1024
    available = memory.get("MemAvailable")
    if available is None or available < 0:
        raise ValueError("Available host memory is unknown.")
    groups = [
        line[3:]
        for line in (proc_root / "self/cgroup").read_text().splitlines()
        if line.startswith("0::")
    ]
    if len(groups) != 1 or not groups[0].startswith("/") or ".." in Path(groups[0]).parts:
        raise ValueError("Unified memory accounting is unavailable.")
    directory = cgroup_root / groups[0].lstrip("/")
    while True:
        limit_file = directory / "memory.max"
        usage_file = directory / "memory.current"
        if directory == cgroup_root and not limit_file.exists() and not usage_file.exists():
            return available
        maximum = limit_file.read_text().strip()
        current = int(usage_file.read_text())
        if current < 0:
            raise ValueError("Memory accounting is invalid.")
        if maximum != "max":
            limit = int(maximum)
            if limit <= 0:
                raise ValueError("Memory limit is invalid.")
            available = min(available, max(0, limit - current))
        if directory == cgroup_root:
            if maximum == "max":
                mounts = [
                    line.split(" - ", 1)
                    for line in (proc_root / "self/mountinfo").read_text().splitlines()
                ]
                virtual_memory = any(
                    len(parts) == 2
                    and parts[0].split()[4] == str(proc_root / "meminfo")
                    and parts[1].split()[0] == "fuse.lxcfs"
                    for parts in mounts
                )
                total = memory.get("MemTotal", 0)
                free = memory.get("MemFree", -1)
                if not virtual_memory or total <= 0 or not 0 <= free <= total:
                    raise ValueError("Effective host memory limit is unavailable.")
                available = min(available, free, max(0, total - current))
            return available
        directory = directory.parent


MEMORY_CAPACITY_PROGRAM = (
    "from pathlib import Path\n"
    + inspect.getsource(observe_memory_headroom)
    + "\nimport json, sys\n"
    + "try:\n"
    + "    frame = {'type': 'complete', 'result': observe_memory_headroom(Path('/proc'), Path('/sys/fs/cgroup'))}\n"
    + "except (ValueError, OSError, IndexError):\n"
    + "    frame = {'type': 'failed'}\n"
    + "print(sys.argv[2] + json.dumps(frame), flush=True)\n"
)


def read_host_memory_headroom(*, host: str, token: str, server_id: str) -> int:
    frames: list[dict[str, object]] = []
    try:
        native_copy_stream._read_remote(
            host=host,
            token=token,
            server_id=server_id,
            program=MEMORY_CAPACITY_PROGRAM,
            payload={},
            receive=frames.append,
            timeout_seconds=60,
        )
    except native_copy_stream.NativeSourceUnavailable:
        raise PreviewMemoryCapacityRefused(
            "Preview start refused: host memory headroom is unavailable."
        ) from None
    if len(frames) == 1 and frames[0].get("type") == "complete":
        headroom = frames[0].get("result")
        if type(headroom) is int and headroom >= 0:
            return headroom
    raise PreviewMemoryCapacityRefused(
        "Preview start refused: host memory headroom is unavailable."
    )


def require_preview_host_memory_capacity(
    *,
    host: str,
    token: str,
    server_id: str,
    database_url: str | None = None,
    record_store: RuntimeEnvironmentRecordStore | None = None,
) -> None:
    try:
        if record_store is None:
            selected_database = resolve_database_url(database_url)
            if not selected_database:
                raise ValueError("Service-record storage is unavailable.")
            store = PostgresRecordStore(database_url=selected_database)
            try:
                records = store.list_runtime_environment_records(context_name="launchplane")
            finally:
                store.close()
        else:
            records = record_store.list_runtime_environment_records(context_name="launchplane")
        selected = [
            record
            for record in records
            if record.scope == "context" and record.context == "launchplane"
        ]
        if len(selected) > 1:
            raise ValueError("Service memory policy is ambiguous.")
        raw = selected[0].env.get(PREVIEW_MEMORY_FLOORS_KEY, "{}") if selected else "{}"
        if not isinstance(raw, str):
            raise ValueError("Invalid memory floors.")
        floors = json.loads(raw)
        if not isinstance(floors, dict) or any(
            not isinstance(key, str)
            or not key.strip()
            or key != key.strip()
            or type(value) is not int
            or value <= 0
            for key, value in floors.items()
        ):
            raise ValueError("Invalid memory floors.")
    except (ValueError, click.ClickException, OSError, SQLAlchemyError):
        raise PreviewMemoryCapacityRefused(
            "Preview start refused: service-record memory policy is unavailable or invalid."
        ) from None
    if server_id not in floors:
        return
    floor = floors[server_id]
    try:
        headroom = read_host_memory_headroom(host=host, token=token, server_id=server_id)
    except PreviewMemoryCapacityRefused:
        raise PreviewMemoryCapacityRefused(
            f"Preview start refused on host {server_id}: floor {floor} bytes; current headroom unavailable."
        ) from None
    if headroom < floor:
        raise PreviewMemoryCapacityRefused(
            f"Preview start refused on host {server_id}: floor {floor} bytes; current headroom {headroom} bytes. Retry when headroom recovers."
        )
