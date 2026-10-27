from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from pydantic import JsonValue

from control_plane.database_maintenance import DatabaseMaintenanceHeld
from control_plane.dokploy import api, source

MAINTENANCE_DEPLOY_ENV = "LAUNCHPLANE_DATABASE_MAINTENANCE_UNTIL"


def require_deployment_available(environment: Mapping[str, str]) -> None:
    raw = environment.get(MAINTENANCE_DEPLOY_ENV, "").strip()
    if not raw:
        return
    try:
        expires = datetime.fromisoformat(raw)
        if expires.tzinfo is None:
            raise ValueError("Maintenance deadline is missing its timezone.")
    except ValueError:
        raise DatabaseMaintenanceHeld(
            "The database maintenance deployment fence is invalid."
        ) from None
    if expires > datetime.now(timezone.utc):
        raise DatabaseMaintenanceHeld("Launchplane deployment is held for database maintenance.")


@contextmanager
def deployment_window(
    *,
    control_plane_root: Path,
    target_id: str,
    repository: str,
    expires_at: str,
    apply: bool = True,
) -> Iterator[None]:
    from control_plane.workflows.launchplane_self_deploy import _validate_launchplane_compose_target

    host, token = source.read_dokploy_config(control_plane_root=control_plane_root)

    def read_target() -> dict[str, JsonValue]:
        payload = api.fetch_dokploy_target_payload(
            host=host, token=token, target_type="compose", target_id=target_id
        )
        environment = api.parse_dokploy_env_text(str(payload.get("env") or ""))
        _validate_launchplane_compose_target(target_payload=payload, env_map=environment)
        if payload.get("autoDeploy") is not False:
            raise DatabaseMaintenanceHeld(
                "Automatic deployment must already be disabled on this target."
            )
        return payload

    def write_marker(payload: dict[str, JsonValue], *, remove: bool) -> None:
        rendered = api.render_dokploy_env_text_with_overrides(
            str(payload.get("env") or ""),
            updates={} if remove else {MAINTENANCE_DEPLOY_ENV: expires_at},
            removals=(MAINTENANCE_DEPLOY_ENV,) if remove else (),
            target_type="compose",
            compose_type=str(payload.get("composeType") or "docker-compose"),
        )
        api.update_dokploy_target_env(
            host=host,
            token=token,
            target_type="compose",
            target_id=target_id,
            target_payload=payload,
            env_text=rendered,
        )

    payload = read_target()
    require_deployment_available(api.parse_dokploy_env_text(str(payload.get("env") or "")))
    if not apply:
        require_idle_deploy_workflow(control_plane_root=control_plane_root, repository=repository)
        yield
        return
    write_marker(payload, remove=False)
    observed = read_target()
    if (
        api.parse_dokploy_env_text(str(observed.get("env") or "")).get(MAINTENANCE_DEPLOY_ENV)
        != expires_at
    ):
        raise DatabaseMaintenanceHeld("The deployment maintenance fence was not stored.")
    try:
        deployments = api.list_deployments_for_target(
            host=host,
            token=token,
            target_type="compose",
            target_id=target_id,
            require_complete=True,
        )
        if any(item.get("status") not in {"done", "error", "cancelled"} for item in deployments):
            raise DatabaseMaintenanceHeld("A deployment is queued, running or unknown.")
        require_idle_deploy_workflow(control_plane_root=control_plane_root, repository=repository)
        yield
    finally:
        current = read_target()
        if (
            api.parse_dokploy_env_text(str(current.get("env") or "")).get(MAINTENANCE_DEPLOY_ENV)
            != expires_at
        ):
            raise DatabaseMaintenanceHeld(
                "Deployment maintenance custody changed; do not overwrite it."
            )
        write_marker(current, remove=True)
        if MAINTENANCE_DEPLOY_ENV in api.parse_dokploy_env_text(
            str(read_target().get("env") or "")
        ):
            raise DatabaseMaintenanceHeld("Deployment maintenance release was not stored.")


def require_idle_deploy_workflow(*, control_plane_root: Path, repository: str) -> None:
    from control_plane.workflows.launchplane import (
        github_api_request,
        resolve_launchplane_github_token,
    )

    if len(repository.split("/")) != 2 or any(not part for part in repository.split("/")):
        raise ValueError("An explicit source repository is required.")
    token = resolve_launchplane_github_token(
        control_plane_root=control_plane_root, context_name="launchplane", repository=repository
    )
    for state in ("queued", "in_progress", "waiting", "pending", "requested"):
        result = github_api_request(
            path=f"/repos/{repository}/actions/workflows/deploy-launchplane.yml/runs?status={state}&per_page=100",
            token=token,
        )
        if (
            not isinstance(result, dict)
            or result.get("total_count") != 0
            or result.get("workflow_runs") != []
        ):
            raise DatabaseMaintenanceHeld(
                "A self-deploy workflow is active or its absence is unproved."
            )
