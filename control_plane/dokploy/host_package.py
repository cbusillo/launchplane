import base64
from collections.abc import Callable
from pathlib import Path
import re
from typing import Any

from control_plane.host_package import HostPackage, host_package


def ensure_host_package(
    *,
    host: str,
    token: str,
    server_id: str,
    package: HostPackage,
    dispatch_guard: Callable[[Callable[[], None]], None] | None = None,
) -> None:
    from control_plane.dokploy.native_copy_stream import _read_remote, NativeSourceUnavailable

    program = Path(__file__).parents[1].joinpath("host_package_installer.py").read_text()

    def request(payload: dict[str, object]) -> bool:
        frames: list[dict[str, object]] = []
        _read_remote(
            host=host,
            token=token,
            server_id=server_id,
            program=program,
            payload={"version": package.version, **payload},
            receive=frames.append,
            timeout_seconds=120,
            dispatch_guard=dispatch_guard,
        )
        if len(frames) != 1 or frames[0].get("version") != package.version:
            raise NativeSourceUnavailable("Host package installation has no version witness.")
        return frames[0].get("installed") is True

    if request({"mode": "inspect"}):
        return
    chunks = [
        package.content[offset : offset + 24 * 1024]
        for offset in range(0, len(package.content), 24 * 1024)
    ]
    for index, content in enumerate(chunks):
        request({"mode": "chunk", "index": index, "content": base64.b64encode(content).decode()})
    if not request({"mode": "commit", "count": len(chunks)}):
        raise NativeSourceUnavailable("Host package installation is unverified.")


def ensure_schedule_host_package(
    *,
    host: str,
    token: str,
    target_id: str,
    schedule_type: str,
    app_name: str,
    payload: dict[str, Any],
) -> None:
    script = payload.get("script")
    if not isinstance(script, str) or not script.startswith(
        '#!/usr/bin/env bash\nset -euo pipefail\nexec python3 "$HOME/.local/state/launchplane-host/'
    ):
        return
    version = re.search(r'/launchplane-host/([a-f0-9]{64})\.pyz"', script)
    if version is None or version.group(1) != host_package().version:
        raise ValueError("Schedule requires an unavailable host package version.")
    from control_plane.dokploy import api
    import json

    package = host_package()
    installer = Path(__file__).parents[1].joinpath("host_package_installer.py").read_text()
    chunks = [
        package.content[offset : offset + 24 * 1024]
        for offset in range(0, len(package.content), 24 * 1024)
    ]
    requests: list[dict[str, object]] = [
        {"mode": "chunk", "index": index, "content": base64.b64encode(content).decode()}
        for index, content in enumerate(chunks)
    ] + [{"mode": "commit", "count": len(chunks)}]
    for index, request in enumerate(requests):
        name = "Launchplane host package " + package.version + " " + str(index)
        encoded = base64.b64encode(
            json.dumps({"version": package.version, **request}).encode()
        ).decode()
        bootstrap = (
            "#!/usr/bin/env bash\nset -euo pipefail\npython3 - "
            + encoded
            + " LP_HOST_PACKAGE: <<'LP_HOST_PACKAGE'\n"
            + installer
            + "\nLP_HOST_PACKAGE\n"
        )
        existing = api.find_matching_dokploy_schedule(
            host=host,
            token=token,
            target_id=target_id,
            schedule_type=schedule_type,
            schedule_name=name,
            app_name=app_name,
        )
        if existing is not None:
            history = api.read_dokploy_schedule_baseline(
                host=host, token=token, schedule_id=api.schedule_key(existing)
            )
            if any(not api.deployment_is_terminal(row) for row in history):
                raise ValueError("Host package bootstrap is not quiescent.")
        schedule = api.upsert_dokploy_schedule(
            host=host,
            token=token,
            target_id=target_id,
            schedule_type=schedule_type,
            schedule_name=name,
            app_name=app_name,
            schedule_payload={**payload, "name": name, "script": bootstrap},
        )
        api.run_dokploy_schedule(
            host=host,
            token=token,
            schedule_id=api.schedule_key(schedule),
            timeout_seconds=120,
        )
