from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path

from control_plane.dokploy import api


MAINTENANCE_UNTIL_ENV = "LAUNCHPLANE_DATABASE_MAINTENANCE_UNTIL"


def require_deployment_available(environment: Mapping[str, str]) -> None:
    raw = environment.get(MAINTENANCE_UNTIL_ENV, "").strip()
    if not raw:
        return
    try:
        expires = datetime.fromisoformat(raw)
        if expires.tzinfo is None:
            raise ValueError("Missing timezone.")
    except ValueError:
        raise ValueError("The database maintenance deployment deadline is invalid.") from None
    if expires > datetime.now(timezone.utc):
        raise ValueError("Launchplane deployment is held for database maintenance.")


class DeploymentMaintenance:
    def __init__(self, *, target_id: str, database_url: str, image: str, root: Path) -> None:
        from control_plane.dokploy import api, source
        from control_plane.workflows.launchplane import resolve_launchplane_github_token
        from control_plane.workflows.launchplane_self_deploy import (
            _validate_launchplane_compose_target,
        )
        import re

        self.database_url = database_url
        self.image = image
        self.target_id = target_id
        self.host, self.token = source.read_dokploy_config(control_plane_root=root)
        self.payload = api.fetch_dokploy_target_payload(
            host=self.host, token=self.token, target_type="compose", target_id=target_id
        )
        environment = api.parse_dokploy_env_text(str(self.payload.get("env") or ""))
        _validate_launchplane_compose_target(target_payload=self.payload, env_map=environment)
        if (
            not database_url
            or not image
            or environment.get("LAUNCHPLANE_DATABASE_URL") != database_url
            or environment.get("DOCKER_IMAGE_REFERENCE") != image
            or self.payload.get("autoDeploy") is not False
        ):
            raise ValueError(
                "The target is not the running Launchplane database/image with auto-deploy off."
            )
        origin = str(self.payload.get("customGitUrl") or "")
        match = re.fullmatch(
            r"(?:git@github\.com:|https://github\.com/)([\w.-]+/[\w.-]+?)(?:\.git)?", origin
        )
        if not match:
            raise ValueError("The source repository cannot be proved from the deployment target.")
        self.repository = match[1]
        self.github_token = resolve_launchplane_github_token(
            control_plane_root=root, context_name="launchplane", repository=self.repository
        )
        if not self.github_token:
            raise ValueError("Workflow observation has no supported source-control reader.")

    def read_target(self) -> api.JsonObject:
        from control_plane.dokploy import api

        target = api.fetch_dokploy_target_payload(
            host=self.host, token=self.token, target_type="compose", target_id=self.target_id
        )
        environment = api.parse_dokploy_env_text(str(target.get("env") or ""))
        if (
            environment.get("LAUNCHPLANE_DATABASE_URL") != self.database_url
            or environment.get("DOCKER_IMAGE_REFERENCE") != self.image
            or target.get("customGitUrl") != self.payload.get("customGitUrl")
        ):
            raise ValueError("The running Launchplane target binding changed.")
        return target

    def observe(self, *, deadline: str | None) -> None:
        from control_plane.dokploy import api
        from control_plane.workflows.launchplane import github_api_request

        target = self.read_target()
        if target.get("autoDeploy") is not False:
            raise ValueError("Automatic deployment changed during maintenance.")
        environment = api.parse_dokploy_env_text(str(target.get("env") or ""))
        if deadline is None:
            require_deployment_available(environment)
        elif environment.get(MAINTENANCE_UNTIL_ENV) != deadline:
            raise ValueError("The deployment maintenance marker changed.")
        history = api.list_deployments_for_target(
            host=self.host,
            token=self.token,
            target_type="compose",
            target_id=self.target_id,
            require_complete=True,
        )
        if any(row.get("status") not in {"done", "error", "cancelled"} for row in history):
            raise ValueError("A deployment is active or unknown.")
        queue = api.dokploy_request(
            host=self.host, token=self.token, path="/api/deployment.queueList"
        )
        if not isinstance(queue, list):
            raise ValueError("The deployment queue observation is incomplete.")
        for row in queue:
            if not isinstance(row, dict) or not isinstance(row.get("data"), dict):
                raise ValueError("The deployment queue contains an unknown job.")
            data = row["data"]
            if isinstance(data, dict) and data.get("composeId") == self.target_id:
                raise ValueError("An own-service deployment is queued.")
        for state in ("queued", "in_progress", "waiting", "pending", "requested"):
            runs = github_api_request(
                path=f"/repos/{self.repository}/actions/workflows/deploy-launchplane.yml/runs?status={state}&per_page=100",
                token=self.github_token,
            )
            if (
                not isinstance(runs, dict)
                or runs.get("total_count") != 0
                or runs.get("workflow_runs") != []
            ):
                raise ValueError("A self-deploy workflow is active or its absence is unproved.")

    def marker(self, deadline: str, *, remove: bool = False) -> None:
        from control_plane.dokploy import api

        target = self.read_target()
        environment = api.parse_dokploy_env_text(str(target.get("env") or ""))
        if remove and MAINTENANCE_UNTIL_ENV not in environment:
            return
        if remove and environment.get(MAINTENANCE_UNTIL_ENV) != deadline:
            raise ValueError("Maintenance marker custody changed; release will not overwrite it.")
        if not remove:
            require_deployment_available(environment)
        rendered = api.render_dokploy_env_text_with_overrides(
            str(target.get("env") or ""),
            updates={} if remove else {MAINTENANCE_UNTIL_ENV: deadline},
            removals=(MAINTENANCE_UNTIL_ENV,) if remove else (),
            target_type="compose",
            compose_type="docker-compose",
        )
        api.update_dokploy_target_env(
            host=self.host,
            token=self.token,
            target_type="compose",
            target_id=self.target_id,
            target_payload=target,
            env_text=rendered,
        )
        observed = api.parse_dokploy_env_text(str(self.read_target().get("env") or ""))
        if (
            (MAINTENANCE_UNTIL_ENV in observed)
            if remove
            else (observed.get(MAINTENANCE_UNTIL_ENV) != deadline)
        ):
            raise ValueError("The deployment marker readback disagrees.")
