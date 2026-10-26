import base64
import hashlib
import json
from pathlib import Path
from typing import Any

from control_plane.host_package import HOST_PACKAGE_CHUNK_BYTES, host_invocation, host_package
from control_plane.host_package_installer import HOST_PACKAGE_INSTALLED_MARKER


def installation_requests(version: str) -> list[dict[str, object]]:
    package = host_package()
    if version != package.version:
        return [{"mode": "require", "version": version}]
    chunks = [
        package.content[offset : offset + HOST_PACKAGE_CHUNK_BYTES]
        for offset in range(0, len(package.content), HOST_PACKAGE_CHUNK_BYTES)
    ]
    requests: list[dict[str, object]] = [
        {"version": version, "mode": "chunk", "content": base64.b64encode(chunk).decode()}
        for chunk in chunks
    ]
    requests.append(
        {
            "version": version,
            "mode": "commit",
            "chunks": [hashlib.sha256(chunk).hexdigest() for chunk in chunks],
        }
    )
    return requests


def installer_program() -> str:
    return Path(__file__).parents[1].joinpath("host_package_installer.py").read_text()


def bootstrap_script(request: dict[str, object]) -> str:
    encoded = base64.b64encode(json.dumps(request, sort_keys=True).encode()).decode()
    return (
        "#!/usr/bin/env bash\nset -euo pipefail\npython3 - "
        + encoded
        + " LP_HOST_PACKAGE: <<'LP_HOST_PACKAGE'\n"
        + installer_program()
        + "\nLP_HOST_PACKAGE\n"
    )


def validate_bootstrap_scripts(payload: dict[str, Any]) -> None:
    from control_plane.dokploy import api

    invocation = host_invocation(payload.get("script"))
    if invocation is None:
        return
    for request in installation_requests(invocation.package_version):
        api.validate_dokploy_schedule_script({**payload, "script": bootstrap_script(request)})


def ensure_schedule_host_package(
    *,
    host: str,
    token: str,
    target_id: str,
    schedule_type: str,
    app_name: str,
    payload: dict[str, Any],
) -> None:
    invocation = host_invocation(payload.get("script"))
    if invocation is None:
        return
    server = payload.get("serverId")
    if isinstance(server, str) and server:
        ensure_remote_package(
            host=host, token=token, server_id=server, version=invocation.package_version
        )
        return
    from control_plane.dokploy import api, post_deploy

    def request(spec: dict[str, object]) -> bool:
        script = bootstrap_script(spec)
        name = "Launchplane host package " + hashlib.sha256(script.encode()).hexdigest()
        bootstrap_app = "lp-host-package-" + invocation.package_version[:24]
        existing = api.find_matching_dokploy_schedule(
            host=host,
            token=token,
            target_id=target_id,
            schedule_type=schedule_type,
            schedule_name=name,
            app_name=bootstrap_app,
        )
        if existing is not None:
            if existing.get("script") != script or existing.get("enabled") is not False:
                raise ValueError("Host package bootstrap provenance differs.")
            history = api.read_dokploy_schedule_baseline(
                host=host,
                token=token,
                schedule_id=api.schedule_key(existing),
            )
            if any(not api.deployment_is_terminal(row) for row in history):
                raise ValueError("Host package bootstrap is not quiescent.")
        schedule = api.upsert_dokploy_schedule(
            host=host,
            token=token,
            target_id=target_id,
            schedule_type=schedule_type,
            schedule_name=name,
            app_name=bootstrap_app,
            schedule_payload={
                **payload,
                "name": name,
                "appName": bootstrap_app,
                "script": script,
                "enabled": False,
            },
        )
        schedule_id = api.schedule_key(schedule)
        deployment_id = api.deployment_key_from_wait_result(
            api.run_dokploy_schedule(
                host=host,
                token=token,
                schedule_id=schedule_id,
                timeout_seconds=120,
            )
        )
        if not deployment_id:
            raise ValueError("Host package bootstrap completion is unavailable.")
        evidence = post_deploy._read_odoo_post_deploy_log_markers(
            host=host,
            token=token,
            schedule_id=schedule_id,
            schedule_deployment_key=deployment_id,
            deployment_id=deployment_id,
        )
        installed = evidence.get(HOST_PACKAGE_INSTALLED_MARKER)
        if installed not in {"true", "false"}:
            raise ValueError("Host package installation witness is unavailable.")
        api.dokploy_request(
            host=host,
            token=token,
            path="/api/schedule.delete",
            method="POST",
            payload={"scheduleId": schedule_id},
        )
        return installed == "true"

    inspect: dict[str, object] = {
        "version": invocation.package_version,
        "mode": "inspect" if invocation.package_version == host_package().version else "require",
    }
    if request(inspect):
        return
    installed = False
    for spec in installation_requests(invocation.package_version):
        installed = request(spec)
    if not installed:
        raise ValueError("Host package installation is unverified.")


def ensure_remote_package(*, host: str, token: str, server_id: str, version: str) -> None:
    from control_plane.dokploy.native_copy_stream import _read_remote, NativeSourceUnavailable

    def request(spec: dict[str, object]) -> bool:
        frames: list[dict[str, object]] = []
        _read_remote(
            host=host,
            token=token,
            server_id=server_id,
            program=installer_program(),
            payload=spec,
            receive=frames.append,
            timeout_seconds=120,
        )
        if len(frames) != 1 or frames[0].get("version") != version:
            raise NativeSourceUnavailable("Host package installation has no version witness.")
        return frames[0].get("installed") is True

    if request(
        {"version": version, "mode": "inspect" if version == host_package().version else "require"}
    ):
        return
    installed = False
    for spec in installation_requests(version):
        installed = request(spec)
    if not installed:
        raise NativeSourceUnavailable("Host package installation is unverified.")
