from collections.abc import Iterator, Mapping
from contextlib import contextmanager
import fcntl
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from control_plane.dokploy import api
from control_plane.database_maintenance_host import (
    RecoveryRequired,
    read_marker_transition,
    save,
    DEPLOYMENT_RECOVERY_REQUIRED_EVENT,
)


MAINTENANCE_UNTIL_ENV = "LAUNCHPLANE_DATABASE_MAINTENANCE_UNTIL"
_logger = logging.getLogger(__name__)


class DeploymentHeld(ValueError):
    pass


def recovery_marker(hold_id: str) -> str:
    return "recovery-required:" + hold_id


def require_deployment_available(environment: Mapping[str, str]) -> None:
    raw = environment.get(MAINTENANCE_UNTIL_ENV, "").strip()
    if not raw:
        return
    if raw.startswith("recovery-required:"):
        raise DeploymentHeld("Launchplane database recovery must be verified before deployment.")
    try:
        expires = datetime.fromisoformat(raw)
        if expires.tzinfo is None:
            raise ValueError("Missing timezone.")
    except ValueError:
        raise ValueError("The database maintenance deployment deadline is invalid.") from None
    if expires > datetime.now(timezone.utc):
        raise DeploymentHeld("Launchplane deployment is held for database maintenance.")


class DeploymentMaintenance:
    def __init__(
        self,
        *,
        target_id: str,
        database_url: str,
        image: str,
        root: Path,
        job_directory: Path | None = None,
        hold_id: str = "",
    ) -> None:
        from control_plane.dokploy import api, source
        from control_plane.workflows.launchplane_self_deploy import (
            _validate_launchplane_compose_target,
        )

        self.root = root
        self.database_url = database_url
        self.image = image
        self.target_id = target_id
        self.job_directory = job_directory
        self.hold_id = hold_id
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
            if (
                isinstance(data, dict)
                and data.get("composeId") == self.target_id
                and row.get("state") not in {"completed", "failed"}
            ):
                raise ValueError("An own-service deployment is queued.")

    def require_idle_workflows(self) -> None:
        from control_plane.workflows.launchplane import github_api_request, launchplane_github_token

        with launchplane_github_token(
            control_plane_root=self.root, context_name="launchplane", repository=self.repository
        ) as token:
            if not token:
                raise ValueError("Workflow observation has no supported source-control reader.")
            for state in ("queued", "in_progress", "waiting", "pending", "requested"):
                runs = github_api_request(
                    path=f"/repos/{self.repository}/actions/workflows/deploy-launchplane.yml/runs?status={state}&per_page=100",
                    token=token,
                )
                if (
                    not isinstance(runs, dict)
                    or runs.get("total_count") != 0
                    or runs.get("workflow_runs") != []
                ):
                    raise ValueError("A self-deploy workflow is active or its absence is unproved.")

    @contextmanager
    def fence(self, deadline: str) -> Iterator[None]:
        self.marker(deadline)
        self.require_idle_workflows()
        self.observe(deadline=deadline)
        yield

    @contextmanager
    def marker_lock(self) -> Iterator[Path]:
        if self.job_directory is None or not self.hold_id:
            raise ValueError("A marker mutation requires exact private maintenance custody.")
        with (self.job_directory / "marker.lock").open("a") as lock:
            os.fchmod(lock.fileno(), 0o600)
            deadline = time.monotonic() + 15
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(
                            "The original marker writer still owns custody."
                        ) from None
                    time.sleep(0.05)
            yield self.job_directory / "marker-transition.json"

    def marker(self, deadline: str, *, remove: bool = False, replace: str | None = None) -> None:
        with self.marker_lock() as path:
            self.change_marker(path, deadline, remove=remove, replace=replace)

    def observe_marker_transition(self, path: Path, transition: dict[str, Any]) -> None:
        try:
            observed = api.parse_dokploy_env_text(str(self.read_target().get("env") or "")).get(
                MAINTENANCE_UNTIL_ENV
            )
        except Exception as error:
            raise RecoveryRequired(
                "The original marker request has no available effect readback.",
                event=DEPLOYMENT_RECOVERY_REQUIRED_EVENT,
            ) from error
        if observed != transition["desired"]:
            raise RecoveryRequired(
                "The original marker request remains unresolved; no new write is allowed.",
                event=DEPLOYMENT_RECOVERY_REQUIRED_EVENT,
            )
        transition["phase"] = "observed"
        save(path, transition)

    def change_marker(
        self, path: Path, deadline: str, *, remove: bool, replace: str | None
    ) -> None:
        from control_plane.dokploy import api

        desired = None if remove else deadline
        transition = read_marker_transition(
            path.parent, hold_id=self.hold_id, target_id=self.target_id
        )
        if transition is not None and transition["phase"] != "observed":
            self.observe_marker_transition(path, transition)
            if transition["desired"] == desired:
                return
        target = self.read_target()
        environment = api.parse_dokploy_env_text(str(target.get("env") or ""))
        if remove and MAINTENANCE_UNTIL_ENV not in environment:
            return
        if remove and environment.get(MAINTENANCE_UNTIL_ENV) != deadline:
            raise RecoveryRequired(
                "Maintenance marker custody changed; release will not overwrite it.",
                event=DEPLOYMENT_RECOVERY_REQUIRED_EVENT,
            )
        if replace is not None and environment.get(MAINTENANCE_UNTIL_ENV) != replace:
            raise RecoveryRequired(
                "The owned maintenance marker changed before recovery fencing.",
                event=DEPLOYMENT_RECOVERY_REQUIRED_EVENT,
            )
        if not remove and replace is None:
            require_deployment_available(environment)
        rendered = api.render_dokploy_env_text_with_overrides(
            str(target.get("env") or ""),
            updates={} if remove else {MAINTENANCE_UNTIL_ENV: deadline},
            removals=(MAINTENANCE_UNTIL_ENV,) if remove else (),
            target_type="compose",
        )
        transition = {
            "hold_id": self.hold_id,
            "target_id": self.target_id,
            "action": "remove" if remove else "install" if replace is None else "recovery",
            "before": environment.get(MAINTENANCE_UNTIL_ENV),
            "desired": desired,
            "phase": "dispatching",
        }
        dispatched = False

        def record_dispatch() -> None:
            nonlocal dispatched
            save(path, transition)
            dispatched = True

        try:
            with api.guard_dokploy_mutations(record_dispatch):
                api.update_dokploy_target_env(
                    host=self.host,
                    token=self.token,
                    target_type="compose",
                    target_id=self.target_id,
                    target_payload=target,
                    env_text=rendered,
                )
            transition["phase"] = "confirmed"
            save(path, transition)
        except Exception:
            if not dispatched:
                raise
            _logger.warning("The marker response is unavailable; observing its original effect.")
        self.observe_marker_transition(path, transition)

    def hold_recovery(self, deadline: str, hold_id: str) -> None:
        self.marker(recovery_marker(hold_id), replace=deadline)
