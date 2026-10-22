import inspect
import json
from pathlib import Path

import click

from control_plane.dokploy import native_copy_stream
from control_plane.runtime_environments import (
    RuntimeEnvironmentRecordStore,
    load_optional_runtime_environment_definition_from_store,
    load_runtime_environment_definition,
)

PREVIEW_MEMORY_FLOORS_KEY = "LAUNCHPLANE_PREVIEW_HOST_MEMORY_FLOORS"


class PreviewMemoryCapacityRefused(ValueError):
    pass


def observe_memory_headroom(proc_root: Path, cgroup_root: Path) -> int:
    available = None
    for line in (proc_root / "meminfo").read_text().splitlines():
        fields = line.split()
        if fields[0] == "MemAvailable:" and len(fields) == 3 and fields[2] == "kB":
            available = int(fields[1]) * 1024
    if available is None or available < 0:
        raise ValueError("Available host memory is unknown.")
    groups = [
        line[3:]
        for line in (proc_root / "self/cgroup").read_text().splitlines()
        if line.startswith("0::")
    ]
    if len(groups) != 1:
        raise ValueError("Unified memory accounting is unavailable.")
    group = groups[0]
    if not group.startswith("/") or ".." in Path(group).parts:
        raise ValueError("Memory cgroup is unavailable.")
    directory = cgroup_root / group.lstrip("/")
    while True:
        maximum = (directory / "memory.max").read_text().strip()
        current = int((directory / "memory.current").read_text())
        if current < 0:
            raise ValueError("Memory accounting is invalid.")
        if maximum != "max":
            limit = int(maximum)
            if limit <= 0:
                raise ValueError("Memory limit is invalid.")
            available = min(available, max(0, limit - current))
        if directory == cgroup_root:
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
        definition = (
            load_optional_runtime_environment_definition_from_store(record_store=record_store)
            if record_store is not None
            else load_runtime_environment_definition(
                control_plane_root=Path("."), database_url=database_url
            )
        )
        raw = definition.shared_env.get(PREVIEW_MEMORY_FLOORS_KEY, "{}") if definition else "{}"
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
    except (ValueError, click.ClickException, OSError):
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
