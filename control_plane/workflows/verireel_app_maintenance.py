from __future__ import annotations

from pathlib import Path
import re
import shlex
import time
from typing import Literal

import click
from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane import dokploy as control_plane_dokploy
from control_plane.dokploy import JsonObject
from control_plane.workflows.ship import utc_now_timestamp


VeriReelAppMaintenanceAction = Literal[
    "migrate",
    "grant-sponsored",
    "promote-owner",
    "delete-user",
    "reset-testing",
]

DEFAULT_TIMEOUT_SECONDS = 300
DEFAULT_ATTEMPTS = 4
DEFAULT_RETRY_DELAY_SECONDS = 5.0
PREVIEW_APPLICATION_NAME_PATTERN = re.compile(r"^ver-preview-pr-[1-9][0-9]*-app$")
PREVIEW_SLUG_PATTERN = re.compile(r"^pr-[1-9][0-9]*$")


class VeriReelAppMaintenanceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    context: Literal["verireel", "verireel-testing"] = "verireel"
    instance: Literal["testing", "preview"] = "testing"
    action: VeriReelAppMaintenanceAction
    email: str = ""
    application_name: str = ""
    preview_slug: str = ""
    timeout_seconds: int = Field(default=DEFAULT_TIMEOUT_SECONDS, ge=1)

    @model_validator(mode="after")
    def _validate_request(self) -> "VeriReelAppMaintenanceRequest":
        self.email = self.email.strip()
        self.application_name = self.application_name.strip()
        self.preview_slug = self.preview_slug.strip()

        if self.context == "verireel" and self.instance != "testing":
            raise ValueError("VeriReel stable app maintenance only supports instance 'testing'.")
        if self.context == "verireel-testing" and self.instance != "preview":
            raise ValueError("VeriReel preview app maintenance requires instance 'preview'.")
        if self.context == "verireel-testing":
            if self.application_name and self.preview_slug:
                raise ValueError(
                    "VeriReel preview app maintenance accepts preview_slug or application_name, not both."
                )
            if self.application_name and not PREVIEW_APPLICATION_NAME_PATTERN.fullmatch(
                self.application_name
            ):
                raise ValueError(
                    "VeriReel preview app maintenance requires a ver-preview-pr-*-app application_name."
                )
            if self.preview_slug and not PREVIEW_SLUG_PATTERN.fullmatch(self.preview_slug):
                raise ValueError("VeriReel preview app maintenance requires a pr-*-shaped preview_slug.")
            if not self.application_name and not self.preview_slug:
                raise ValueError("VeriReel preview app maintenance requires preview_slug.")
        if self.context == "verireel" and self.application_name:
            raise ValueError("VeriReel stable app maintenance resolves application_name from Launchplane records.")
        if self.context == "verireel" and self.preview_slug:
            raise ValueError("VeriReel stable app maintenance does not accept preview_slug.")
        if self.action in {"migrate", "reset-testing"} and self.context != "verireel":
            raise ValueError(
                f"VeriReel app maintenance action '{self.action}' is only supported for the stable testing instance."
            )
        if self.action in {"migrate", "reset-testing"} and self.email:
            raise ValueError(f"VeriReel app maintenance action '{self.action}' does not accept email.")
        if self.action not in {"migrate", "reset-testing"} and not self.email:
            raise ValueError(f"VeriReel app maintenance action '{self.action}' requires email.")
        return self


class VeriReelAppMaintenanceResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    maintenance_status: Literal["pass", "fail"]
    action: VeriReelAppMaintenanceAction
    context: str
    instance: str
    application_name: str
    application_id: str
    schedule_name: str
    started_at: str
    finished_at: str
    error_message: str = ""


def _migration_command() -> str:
    return "npx prisma migrate deploy --config prisma.config.ts"


def _reset_testing_command() -> str:
    return "node prisma/reset-testing-job.mjs && npx prisma migrate deploy --schema prisma/schema.prisma && node prisma/seed.mjs"


def _remote_owner_admin_command(*, action: str, email: str) -> str:
    return f"node scripts/ops/remote-owner-admin.mjs --action {shlex.quote(action)} --email {shlex.quote(email)}"


def _command_for_request(request: VeriReelAppMaintenanceRequest) -> tuple[str, str]:
    if request.action == "migrate":
        return "ver-apply-prisma-migrations", _migration_command()
    if request.action == "reset-testing":
        return "ver-testing-reset", _reset_testing_command()
    if request.action == "grant-sponsored":
        return "ver-remote-e2e-grant-sponsored", _remote_owner_admin_command(
            action=request.action,
            email=request.email,
        )
    if request.action == "promote-owner":
        return "ver-owner-route-promote-owner", _remote_owner_admin_command(
            action=request.action,
            email=request.email,
        )
    if request.action == "delete-user":
        schedule_name = (
            "ver-owner-route-delete-user" if request.context == "verireel" else "ver-remote-e2e-delete-user"
        )
        return schedule_name, _remote_owner_admin_command(
            action=request.action,
            email=request.email,
        )
    raise click.ClickException(f"Unsupported VeriReel app maintenance action '{request.action}'.")


def _resolve_stable_testing_application(
    *,
    control_plane_root: Path,
) -> tuple[str, str]:
    source_of_truth = control_plane_dokploy.read_control_plane_dokploy_source_of_truth(
        control_plane_root=control_plane_root,
    )
    target_definition = control_plane_dokploy.find_dokploy_target_definition(
        source_of_truth,
        context_name="verireel",
        instance_name="testing",
    )
    if target_definition is None:
        raise click.ClickException("No Dokploy target definition found for verireel/testing.")
    if target_definition.target_type != "application" or not target_definition.target_id.strip():
        raise click.ClickException("VeriReel app maintenance requires verireel/testing to resolve to an application target.")
    target_name = target_definition.target_name.strip() or "ver-testing-app"
    return target_name, target_definition.target_id.strip()


def _find_application_by_name(*, host: str, token: str, application_name: str) -> JsonObject | None:
    raw_projects = control_plane_dokploy.dokploy_request(
        host=host,
        token=token,
        path="/api/project.all",
    )
    if not isinstance(raw_projects, list):
        return None
    for raw_project in raw_projects:
        project = control_plane_dokploy.as_json_object(raw_project)
        if project is None:
            continue
        raw_environments = project.get("environments")
        if not isinstance(raw_environments, list):
            continue
        for raw_environment in raw_environments:
            environment = control_plane_dokploy.as_json_object(raw_environment)
            if environment is None:
                continue
            raw_applications = environment.get("applications")
            if not isinstance(raw_applications, list):
                continue
            for raw_application in raw_applications:
                application = control_plane_dokploy.as_json_object(raw_application)
                if application is None:
                    continue
                if str(application.get("name") or "").strip() == application_name:
                    return application
    return None


def _resolve_preview_application(*, host: str, token: str, application_name: str) -> tuple[str, str]:
    application = _find_application_by_name(
        host=host,
        token=token,
        application_name=application_name,
    )
    if application is None:
        raise click.ClickException(f"No Dokploy application found for preview app {application_name!r}.")
    application_id = str(application.get("applicationId") or application.get("id") or "").strip()
    if not application_id:
        raise click.ClickException(f"Preview app {application_name!r} did not expose an application id.")
    return application_name, application_id


def _preview_application_name(preview_slug: str) -> str:
    return f"ver-preview-{preview_slug}-app"


def _find_application_schedule(*, host: str, token: str, application_id: str, schedule_name: str) -> JsonObject | None:
    for schedule in control_plane_dokploy.list_dokploy_schedules(
        host=host,
        token=token,
        target_id=application_id,
        schedule_type="application",
    ):
        if str(schedule.get("name") or "").strip() == schedule_name:
            return dict(schedule)
    return None


def _upsert_application_schedule(
    *,
    host: str,
    token: str,
    application_id: str,
    schedule_name: str,
    command: str,
) -> str:
    existing_schedule = _find_application_schedule(
        host=host,
        token=token,
        application_id=application_id,
        schedule_name=schedule_name,
    )
    payload: dict[str, object] = {
        "name": schedule_name,
        "cronExpression": control_plane_dokploy.DOKPLOY_MANUAL_ONLY_CRON_EXPRESSION,
        "scheduleType": "application",
        "shellType": "sh",
        "command": command,
        "applicationId": application_id,
        "enabled": False,
        "timezone": "UTC",
    }
    if existing_schedule is None:
        control_plane_dokploy.dokploy_request(
            host=host,
            token=token,
            path="/api/schedule.create",
            method="POST",
            payload=payload,
        )
    else:
        control_plane_dokploy.dokploy_request(
            host=host,
            token=token,
            path="/api/schedule.update",
            method="POST",
            payload={"scheduleId": control_plane_dokploy.schedule_key(existing_schedule), **payload},
        )
    resolved_schedule = _find_application_schedule(
        host=host,
        token=token,
        application_id=application_id,
        schedule_name=schedule_name,
    )
    if resolved_schedule is None:
        raise click.ClickException(
            f"Dokploy schedule {schedule_name!r} for application {application_id!r} could not be resolved."
        )
    schedule_id = control_plane_dokploy.schedule_key(resolved_schedule)
    if not schedule_id:
        raise click.ClickException(
            f"Dokploy schedule {schedule_name!r} for application {application_id!r} did not expose a schedule id."
        )
    return schedule_id


def _run_application_command_with_retries(
    *,
    host: str,
    token: str,
    application_id: str,
    schedule_name: str,
    command: str,
    timeout_seconds: int,
) -> None:
    for attempt in range(1, DEFAULT_ATTEMPTS + 1):
        try:
            schedule_id = _upsert_application_schedule(
                host=host,
                token=token,
                application_id=application_id,
                schedule_name=schedule_name,
                command=command,
            )
            latest_before = control_plane_dokploy.latest_deployment_for_schedule(
                host=host,
                token=token,
                schedule_id=schedule_id,
            )
            control_plane_dokploy.dokploy_request(
                host=host,
                token=token,
                path="/api/schedule.runManually",
                method="POST",
                payload={"scheduleId": schedule_id},
                timeout_seconds=timeout_seconds,
            )
            control_plane_dokploy.wait_for_dokploy_schedule_deployment(
                host=host,
                token=token,
                schedule_id=schedule_id,
                before_key=control_plane_dokploy.deployment_key(latest_before),
                timeout_seconds=timeout_seconds,
            )
            return
        except click.ClickException:
            if attempt >= DEFAULT_ATTEMPTS:
                raise
            time.sleep(DEFAULT_RETRY_DELAY_SECONDS)


def _trigger_application_deploy(*, host: str, token: str, application_id: str) -> None:
    control_plane_dokploy.dokploy_request(
        host=host,
        token=token,
        path="/api/application.deploy",
        method="POST",
        payload={"applicationId": application_id},
    )


def execute_verireel_app_maintenance(
    *,
    control_plane_root: Path,
    request: VeriReelAppMaintenanceRequest,
) -> VeriReelAppMaintenanceResult:
    started_at = utc_now_timestamp()
    application_name = ""
    application_id = ""
    schedule_name = ""
    try:
        host, token = control_plane_dokploy.read_dokploy_config(control_plane_root=control_plane_root)
        if request.context == "verireel":
            application_name, application_id = _resolve_stable_testing_application(
                control_plane_root=control_plane_root,
            )
        else:
            application_name, application_id = _resolve_preview_application(
                host=host,
                token=token,
                application_name=request.application_name or _preview_application_name(request.preview_slug),
            )
        schedule_name, command = _command_for_request(request)
        _run_application_command_with_retries(
            host=host,
            token=token,
            application_id=application_id,
            schedule_name=schedule_name,
            command=command,
            timeout_seconds=request.timeout_seconds,
        )
        if request.action == "reset-testing":
            _trigger_application_deploy(
                host=host,
                token=token,
                application_id=application_id,
            )
        return VeriReelAppMaintenanceResult(
            maintenance_status="pass",
            action=request.action,
            context=request.context,
            instance=request.instance,
            application_name=application_name,
            application_id=application_id,
            schedule_name=schedule_name,
            started_at=started_at,
            finished_at=utc_now_timestamp(),
        )
    except Exception as error:
        return VeriReelAppMaintenanceResult(
            maintenance_status="fail",
            action=request.action,
            context=request.context,
            instance=request.instance,
            application_name=application_name,
            application_id=application_id,
            schedule_name=schedule_name,
            started_at=started_at,
            finished_at=utc_now_timestamp(),
            error_message=str(error),
        )
