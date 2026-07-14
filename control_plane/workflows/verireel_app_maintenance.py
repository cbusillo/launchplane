from __future__ import annotations

from pathlib import Path
import json
import re
import time
from typing import Literal, cast
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

import click
from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane import runtime_environments as control_plane_runtime_environments
from control_plane.workflows.ship import utc_now_timestamp
from control_plane.dokploy import api as dokploy_api
from control_plane.dokploy import source as dokploy_source
from control_plane.dokploy import post_deploy as dokploy_post_deploy
from control_plane.dokploy.api import JsonObject


VeriReelAppMaintenanceAction = Literal[
    "migrate",
    "grant-sponsored",
    "promote-owner",
    "delete-user",
    "reset-testing",
]
VeriReelAppMaintenanceIntent = Literal[
    "stable-testing-migration",
    "stable-testing-reset",
    "stable-testing-remote-e2e-grant-sponsored",
    "remote-e2e-grant-sponsored",
    "remote-e2e-delete-user",
    "owner-route-promote-owner",
    "owner-route-delete-user",
]

APP_MAINTENANCE_INTENT_REQUIREMENTS: dict[
    str,
    tuple[VeriReelAppMaintenanceAction, str],
] = {
    "stable-testing-migration": ("migrate", "verireel"),
    "stable-testing-reset": ("reset-testing", "verireel"),
    "stable-testing-remote-e2e-grant-sponsored": ("grant-sponsored", "verireel"),
    "remote-e2e-grant-sponsored": ("grant-sponsored", "verireel-testing"),
    "remote-e2e-delete-user": ("delete-user", "verireel-testing"),
    "owner-route-promote-owner": ("promote-owner", "verireel"),
    "owner-route-delete-user": ("delete-user", "verireel"),
}

DEFAULT_TIMEOUT_SECONDS = 300
DEFAULT_ATTEMPTS = 4
DEFAULT_RETRY_DELAY_SECONDS = 5.0
PREVIEW_APPLICATION_NAME_PATTERN = re.compile(r"^ver-preview-pr-[1-9][0-9]*-app$")
PREVIEW_SLUG_PATTERN = re.compile(r"^pr-[1-9][0-9]*$")
SMOKE_MAINTENANCE_SECRET_KEY = "VERIREEL_SMOKE_MAINTENANCE_SECRET"
SMOKE_MAINTENANCE_PATH = "/api/internal/smoke-maintenance"
SMOKE_MAINTENANCE_ACTIONS = frozenset({"grant-sponsored", "promote-owner", "delete-user"})


class VeriReelAppMaintenanceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    context: Literal["verireel", "verireel-testing"] = "verireel"
    instance: Literal["testing", "preview"] = "testing"
    action: VeriReelAppMaintenanceAction
    intent: VeriReelAppMaintenanceIntent
    email: str = ""
    application_name: str = ""
    preview_slug: str = ""
    timeout_seconds: int = Field(default=DEFAULT_TIMEOUT_SECONDS, ge=1)

    @model_validator(mode="after")
    def _validate_request(self) -> "VeriReelAppMaintenanceRequest":
        self.email = self.email.strip()
        self.intent = cast(VeriReelAppMaintenanceIntent, self.intent.strip())
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
                raise ValueError(
                    "VeriReel preview app maintenance requires a pr-*-shaped preview_slug."
                )
            if not self.application_name and not self.preview_slug:
                raise ValueError("VeriReel preview app maintenance requires preview_slug.")
        if self.context == "verireel" and self.application_name:
            raise ValueError(
                "VeriReel stable app maintenance resolves application_name from Launchplane records."
            )
        if self.context == "verireel" and self.preview_slug:
            raise ValueError("VeriReel stable app maintenance does not accept preview_slug.")
        if self.action in {"migrate", "reset-testing"} and self.context != "verireel":
            raise ValueError(
                f"VeriReel app maintenance action '{self.action}' is only supported for the stable testing instance."
            )
        if self.action in {"migrate", "reset-testing"} and self.email:
            raise ValueError(
                f"VeriReel app maintenance action '{self.action}' does not accept email."
            )
        if self.action not in {"migrate", "reset-testing"} and not self.email:
            raise ValueError(f"VeriReel app maintenance action '{self.action}' requires email.")
        required_action, required_context = APP_MAINTENANCE_INTENT_REQUIREMENTS[self.intent]
        if self.action != required_action or self.context != required_context:
            raise ValueError(
                "VeriReel app maintenance intent "
                f"'{self.intent}' requires action '{required_action}' "
                f"in context '{required_context}'."
            )
        return self


class VeriReelAppMaintenanceResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    maintenance_status: Literal["pass", "fail"]
    action: VeriReelAppMaintenanceAction
    intent: str = ""
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


def _uses_internal_smoke_maintenance_api(request: VeriReelAppMaintenanceRequest) -> bool:
    return request.action in SMOKE_MAINTENANCE_ACTIONS


def _command_for_request(request: VeriReelAppMaintenanceRequest) -> tuple[str, str]:
    if request.action == "migrate":
        return "ver-apply-prisma-migrations", _migration_command()
    if request.action == "reset-testing":
        return "ver-testing-reset", _reset_testing_command()
    if _uses_internal_smoke_maintenance_api(request):
        raise click.ClickException(
            f"VeriReel app maintenance action '{request.action}' uses the internal smoke maintenance API."
        )
    raise click.ClickException(f"Unsupported VeriReel app maintenance action '{request.action}'.")


def _smoke_maintenance_operation_name(request: VeriReelAppMaintenanceRequest) -> str:
    if request.action == "grant-sponsored":
        return "ver-internal-smoke-grant-sponsored"
    if request.action == "promote-owner":
        return "ver-internal-smoke-promote-owner"
    if request.action == "delete-user":
        return "ver-internal-smoke-delete-user"
    raise click.ClickException(f"Unsupported VeriReel smoke maintenance action '{request.action}'.")


def _resolve_stable_testing_application(
    *,
    control_plane_root: Path,
) -> tuple[str, str]:
    source_of_truth = dokploy_source.read_control_plane_dokploy_source_of_truth(
        control_plane_root=control_plane_root,
    )
    target_definition = dokploy_source.find_dokploy_target_definition(
        source_of_truth,
        context_name="verireel",
        instance_name="testing",
    )
    if target_definition is None:
        raise click.ClickException("No Dokploy target definition found for verireel/testing.")
    if target_definition.target_type != "application" or not target_definition.target_id.strip():
        raise click.ClickException(
            "VeriReel app maintenance requires verireel/testing to resolve to an application target."
        )
    target_name = target_definition.target_name.strip() or "ver-testing-app"
    return target_name, target_definition.target_id.strip()


def _find_application_by_name(*, host: str, token: str, application_name: str) -> JsonObject | None:
    raw_projects = dokploy_api.dokploy_request(
        host=host,
        token=token,
        path="/api/project.all",
    )
    if not isinstance(raw_projects, list):
        return None
    for raw_project in raw_projects:
        project = dokploy_api.as_json_object(raw_project)
        if project is None:
            continue
        raw_environments = project.get("environments")
        if not isinstance(raw_environments, list):
            continue
        for raw_environment in raw_environments:
            environment = dokploy_api.as_json_object(raw_environment)
            if environment is None:
                continue
            raw_applications = environment.get("applications")
            if not isinstance(raw_applications, list):
                continue
            for raw_application in raw_applications:
                application = dokploy_api.as_json_object(raw_application)
                if application is None:
                    continue
                if str(application.get("name") or "").strip() == application_name:
                    return application
    return None


def _preview_application_name(preview_slug: str) -> str:
    return f"ver-preview-{preview_slug}-app"


def _validate_base_url(raw_base_url: str, *, label: str) -> str:
    base_url = raw_base_url.strip().rstrip("/")
    if not base_url:
        raise click.ClickException(f"{label} requires a base URL.")
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise click.ClickException(f"{label} requires an http or https base URL.")
    if parsed.path not in {"", "/"} or parsed.params or parsed.query or parsed.fragment:
        raise click.ClickException(
            f"{label} requires a root base URL without path, query, or fragment."
        )
    return base_url


def _first_application_base_url(application: JsonObject) -> str:
    raw_domains = application.get("domains")
    domains = raw_domains if isinstance(raw_domains, list) else []
    for raw_domain in domains:
        domain = dokploy_api.as_json_object(raw_domain)
        if domain is None:
            continue
        raw_host = str(domain.get("host") or "").strip()
        if not raw_host:
            continue
        return _validate_base_url(
            raw_host if urlparse(raw_host).scheme else f"https://{raw_host}",
            label="VeriReel preview smoke maintenance",
        )
    raise click.ClickException("VeriReel preview smoke maintenance requires a Dokploy domain.")


def _resolve_application_base_url(*, host: str, token: str, application_id: str) -> str:
    raw_domains = dokploy_api.dokploy_request(
        host=host,
        token=token,
        path="/api/domain.byApplicationId",
        query={"applicationId": application_id},
    )
    application: JsonObject = {"domains": raw_domains if isinstance(raw_domains, list) else []}
    return _first_application_base_url(application)


def _fetch_application_by_id(*, host: str, token: str, application_id: str) -> JsonObject:
    return dokploy_api.fetch_dokploy_target_payload(
        host=host,
        token=token,
        target_type="application",
        target_id=application_id,
    )


def _resolve_stable_testing_base_url(*, control_plane_root: Path) -> str:
    source_of_truth = dokploy_source.read_control_plane_dokploy_source_of_truth(
        control_plane_root=control_plane_root,
    )
    target_definition = dokploy_source.find_dokploy_target_definition(
        source_of_truth,
        context_name="verireel",
        instance_name="testing",
    )
    if target_definition is None:
        raise click.ClickException("No Dokploy target definition found for verireel/testing.")
    base_urls = dokploy_source.resolve_healthcheck_base_urls(
        target_definition=target_definition,
        environment_values={},
    )
    if not base_urls:
        raise click.ClickException("No base URL configured for verireel/testing.")
    return _validate_base_url(
        base_urls[0],
        label="VeriReel stable smoke maintenance",
    )


def _resolve_stable_testing_smoke_maintenance_secret(*, control_plane_root: Path) -> str:
    environment_values = control_plane_runtime_environments.resolve_runtime_environment_values(
        control_plane_root=control_plane_root,
        context_name="verireel",
        instance_name="testing",
    )
    secret = str(environment_values.get(SMOKE_MAINTENANCE_SECRET_KEY) or "").strip()
    if not secret:
        raise click.ClickException(
            f"VeriReel stable smoke maintenance requires {SMOKE_MAINTENANCE_SECRET_KEY}."
        )
    return secret


def _preview_application_environment_map(application: JsonObject) -> dict[str, str]:
    return dokploy_api.parse_dokploy_env_text(str(application.get("env") or ""))


def _resolve_preview_smoke_maintenance_target(
    *,
    host: str,
    token: str,
    application_name: str,
) -> tuple[str, str, str, str]:
    application = _find_application_by_name(
        host=host,
        token=token,
        application_name=application_name,
    )
    if application is None:
        raise click.ClickException(
            f"No Dokploy application found for preview app {application_name!r}."
        )
    application_id = str(application.get("applicationId") or application.get("id") or "").strip()
    if not application_id:
        raise click.ClickException(
            f"Preview app {application_name!r} did not expose an application id."
        )
    base_url = _resolve_application_base_url(
        host=host,
        token=token,
        application_id=application_id,
    )
    resolved_application = _fetch_application_by_id(
        host=host,
        token=token,
        application_id=application_id,
    )
    environment_map = _preview_application_environment_map(resolved_application)
    secret = str(environment_map.get(SMOKE_MAINTENANCE_SECRET_KEY) or "").strip()
    if not secret:
        raise click.ClickException(
            f"VeriReel preview smoke maintenance requires {SMOKE_MAINTENANCE_SECRET_KEY}."
        )
    return application_name, application_id, base_url, secret


def _request_internal_smoke_maintenance_api(
    *,
    base_url: str,
    secret: str,
    request: VeriReelAppMaintenanceRequest,
) -> None:
    endpoint = f"{_validate_base_url(base_url, label='VeriReel smoke maintenance')}{SMOKE_MAINTENANCE_PATH}"
    body = json.dumps(
        {
            "action": request.action,
            "email": request.email,
        }
    ).encode("utf-8")
    http_request = Request(
        endpoint,
        data=body,
        method="POST",
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {secret}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urlopen(http_request, timeout=request.timeout_seconds) as response:
            response.read()
    except HTTPError as error:
        raise click.ClickException(
            f"VeriReel smoke maintenance API returned HTTP {error.code}."
        ) from error
    except URLError as error:
        raise click.ClickException(
            f"VeriReel smoke maintenance API request failed: {error.reason}"
        ) from error


def _find_application_schedule(
    *, host: str, token: str, application_id: str, schedule_name: str
) -> JsonObject | None:
    for schedule in dokploy_api.list_dokploy_schedules(
        host=host,
        token=token,
        target_id=application_id,
        schedule_type="application",
    ):
        if str(schedule.get("name") or "").strip() == schedule_name:
            return schedule
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
    payload: JsonObject = {
        "name": schedule_name,
        "cronExpression": dokploy_post_deploy.DOKPLOY_MANUAL_ONLY_CRON_EXPRESSION,
        "scheduleType": "application",
        "shellType": "sh",
        "command": command,
        "applicationId": application_id,
        "enabled": False,
        "timezone": "UTC",
    }
    if existing_schedule is None:
        dokploy_api.dokploy_request(
            host=host,
            token=token,
            path="/api/schedule.create",
            method="POST",
            payload=payload,
        )
    else:
        update_payload: JsonObject = {
            "scheduleId": dokploy_api.schedule_key(existing_schedule),
            **payload,
        }
        dokploy_api.dokploy_request(
            host=host,
            token=token,
            path="/api/schedule.update",
            method="POST",
            payload=update_payload,
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
    schedule_id = dokploy_api.schedule_key(resolved_schedule)
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
            latest_before = dokploy_api.latest_deployment_for_schedule(
                host=host,
                token=token,
                schedule_id=schedule_id,
            )
            dokploy_api.dokploy_request(
                host=host,
                token=token,
                path="/api/schedule.runManually",
                method="POST",
                payload={"scheduleId": schedule_id},
                timeout_seconds=timeout_seconds,
            )
            dokploy_api.wait_for_dokploy_schedule_deployment(
                host=host,
                token=token,
                schedule_id=schedule_id,
                before_key=dokploy_api.deployment_key(latest_before),
                timeout_seconds=timeout_seconds,
            )
            return
        except click.ClickException:
            if attempt >= DEFAULT_ATTEMPTS:
                raise
            time.sleep(DEFAULT_RETRY_DELAY_SECONDS)


def _trigger_application_deploy(*, host: str, token: str, application_id: str) -> None:
    dokploy_api.dokploy_request(
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
        if _uses_internal_smoke_maintenance_api(request):
            schedule_name = _smoke_maintenance_operation_name(request)
            if request.context == "verireel":
                application_name, application_id = _resolve_stable_testing_application(
                    control_plane_root=control_plane_root,
                )
                base_url = _resolve_stable_testing_base_url(
                    control_plane_root=control_plane_root,
                )
                secret = _resolve_stable_testing_smoke_maintenance_secret(
                    control_plane_root=control_plane_root,
                )
            else:
                host, token = dokploy_source.read_dokploy_config(
                    control_plane_root=control_plane_root
                )
                application_name, application_id, base_url, secret = (
                    _resolve_preview_smoke_maintenance_target(
                        host=host,
                        token=token,
                        application_name=request.application_name
                        or _preview_application_name(request.preview_slug),
                    )
                )
            _request_internal_smoke_maintenance_api(
                base_url=base_url,
                secret=secret,
                request=request,
            )
        else:
            host, token = dokploy_source.read_dokploy_config(control_plane_root=control_plane_root)
            application_name, application_id = _resolve_stable_testing_application(
                control_plane_root=control_plane_root,
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
            intent=request.intent,
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
            intent=request.intent,
            context=request.context,
            instance=request.instance,
            application_name=application_name,
            application_id=application_id,
            schedule_name=schedule_name,
            started_at=started_at,
            finished_at=utc_now_timestamp(),
            error_message=str(error),
        )
