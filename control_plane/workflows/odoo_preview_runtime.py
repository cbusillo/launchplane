from __future__ import annotations

import time
from pathlib import Path
from typing import Literal
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

import click
from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane import dokploy as control_plane_dokploy
from control_plane.dokploy import JsonObject
from control_plane.contracts.odoo_preview_runtime_plan import (
    OdooPreviewRuntimeOperation,
    OdooPreviewRuntimePlan,
    OdooPreviewRuntimePlanStatus,
)


OdooPreviewDokployDryRunStatus = OdooPreviewRuntimePlanStatus
OdooPreviewDokployDryRunBlockerCode = Literal[
    "endpoint_path_missing",
    "environment_id_missing",
    "preview_url_invalid",
    "runtime_plan_not_ready",
]
OdooPreviewDokployApplyStatus = Literal["pass", "blocked", "fail"]
OdooPreviewDokployOperationName = Literal[
    "compose_create",
    "compose_update_raw_source",
    "compose_update_env",
    "domain_lookup",
    "domain_create_or_update",
    "compose_deploy",
    "smoke_check",
    "domain_delete",
    "compose_delete",
]

ODOO_PREVIEW_REQUIRED_ENV_KEYS = (
    "ODOO_DB_NAME",
    "ODOO_DB_USER",
    "ODOO_DB_PASSWORD",
    "ODOO_DATA_VOLUME",
    "ODOO_LOG_VOLUME",
    "ODOO_DB_VOLUME",
    "ODOO_MASTER_PASSWORD",
    "ODOO_ADMIN_PASSWORD",
)


class OdooPreviewDokployEndpointSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    compose_create_path: str = "/api/compose.create"
    compose_update_path: str = "/api/compose.update"
    compose_deploy_path: str = "/api/compose.deploy"
    compose_redeploy_path: str = "/api/compose.redeploy"
    compose_delete_path: str = "/api/compose.delete"
    domain_by_compose_path: str = "/api/domain.byComposeId"
    domain_create_path: str = "/api/domain.create"
    domain_update_path: str = "/api/domain.update"
    domain_delete_path: str = "/api/domain.delete"

    @model_validator(mode="after")
    def _normalize_paths(self) -> "OdooPreviewDokployEndpointSpec":
        for field_name in type(self).model_fields:
            raw_value = getattr(self, field_name)
            value = raw_value.strip()
            if value and not value.startswith("/api/"):
                raise ValueError(f"Dokploy endpoint path must start with /api/: {field_name}")
            setattr(self, field_name, value)
        return self


class OdooPreviewDokployDryRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    runtime_plan: OdooPreviewRuntimePlan
    endpoint_spec: OdooPreviewDokployEndpointSpec = Field(
        default_factory=OdooPreviewDokployEndpointSpec
    )
    no_cache: bool = False
    delete_volumes: bool = True
    runtime_port: int = Field(default=8069, ge=1)
    compose_name: str = ""
    environment_id: str = ""

    @model_validator(mode="after")
    def _normalize_request(self) -> "OdooPreviewDokployDryRunRequest":
        self.compose_name = self.compose_name.strip()
        self.environment_id = self.environment_id.strip()
        return self


class OdooPreviewDokployDryRunBlocker(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: OdooPreviewDokployDryRunBlockerCode
    message: str

    @model_validator(mode="after")
    def _normalize_blocker(self) -> "OdooPreviewDokployDryRunBlocker":
        self.message = _required_text(self.message, "Odoo preview dry-run blocker requires message")
        return self


class OdooPreviewDokployOperation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: OdooPreviewDokployOperationName
    method: Literal["GET", "POST", "LOCAL"]
    path: str = ""
    target: str
    payload_keys: tuple[str, ...] = ()
    secret_payload: bool = False

    @model_validator(mode="after")
    def _normalize_operation(self) -> "OdooPreviewDokployOperation":
        self.path = self.path.strip()
        if self.method != "LOCAL" and not self.path:
            raise ValueError("Dokploy API operation requires path")
        self.target = _required_text(self.target, "Dokploy operation requires target")
        self.payload_keys = _normalized_unique_texts(self.payload_keys)
        return self


class OdooPreviewDokployDryRunPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: OdooPreviewDokployDryRunStatus
    dry_run: Literal[True] = True
    operation: OdooPreviewRuntimeOperation
    product: str
    repository: str
    preview_slug: str
    preview_url: str
    domain_host: str = ""
    compose_ref: str
    compose_name: str
    environment_id: str = ""
    no_cache: bool = False
    delete_volumes: bool = True
    runtime_port: int = Field(default=8069, ge=1)
    blockers: tuple[OdooPreviewDokployDryRunBlocker, ...] = ()
    operations: tuple[OdooPreviewDokployOperation, ...] = ()
    rollback_operations: tuple[OdooPreviewDokployOperation, ...] = ()
    summary: str

    @model_validator(mode="after")
    def _normalize_plan(self) -> "OdooPreviewDokployDryRunPlan":
        self.product = _required_text(self.product, "Odoo preview dry-run plan requires product")
        self.repository = _required_text(
            self.repository, "Odoo preview dry-run plan requires repository"
        )
        self.preview_slug = _required_text(
            self.preview_slug, "Odoo preview dry-run plan requires preview_slug"
        )
        self.preview_url = self.preview_url.strip()
        self.domain_host = self.domain_host.strip().lower()
        self.compose_ref = _required_text(
            self.compose_ref, "Odoo preview dry-run plan requires compose_ref"
        )
        self.compose_name = _required_text(
            self.compose_name, "Odoo preview dry-run plan requires compose_name"
        )
        self.environment_id = self.environment_id.strip()
        self.blockers = tuple(sorted(self.blockers, key=lambda blocker: blocker.code))
        self.summary = _required_text(self.summary, "Odoo preview dry-run plan requires summary")
        if self.status == "ready" and self.blockers:
            raise ValueError("ready Odoo preview dry-run plan cannot include blockers")
        if self.status == "blocked" and not self.blockers:
            raise ValueError("blocked Odoo preview dry-run plan requires blockers")
        if self.status == "blocked" and (self.operations or self.rollback_operations):
            raise ValueError("blocked Odoo preview dry-run plan cannot include operations")
        return self


class OdooPreviewDokployApplyStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: OdooPreviewDokployOperationName
    target: str
    status: Literal["pass", "skipped"] = "pass"
    detail: str = ""

    @model_validator(mode="after")
    def _normalize_step(self) -> "OdooPreviewDokployApplyStep":
        self.target = _required_text(self.target, "Odoo preview apply step requires target")
        self.detail = self.detail.strip()
        return self


class OdooPreviewDokployApplyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dry_run_plan: OdooPreviewDokployDryRunPlan
    image_reference: str
    environment_values: dict[str, str] = Field(default_factory=dict)
    compose_file: str = ""
    health_path: str = "/web/health"
    timeout_seconds: int = Field(default=300, ge=1)
    wait_for_deploy: bool = True
    smoke_check: bool = True

    @model_validator(mode="after")
    def _normalize_request(self) -> "OdooPreviewDokployApplyRequest":
        self.image_reference = _required_text(
            self.image_reference, "Odoo preview apply requires image_reference"
        )
        self.compose_file = self.compose_file.strip()
        self.health_path = self.health_path.strip() or "/web/health"
        if not self.health_path.startswith("/"):
            self.health_path = f"/{self.health_path}"
        self.environment_values = {
            key.strip(): value for key, value in self.environment_values.items() if key.strip()
        }
        return self


class OdooPreviewDokployApplyResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: OdooPreviewDokployApplyStatus
    operation: OdooPreviewRuntimeOperation
    product: str
    repository: str
    preview_slug: str
    preview_url: str
    domain_host: str
    compose_id: str = ""
    compose_name: str
    created_compose: bool = False
    domain_id: str = ""
    steps: tuple[OdooPreviewDokployApplyStep, ...] = ()
    rollback_errors: tuple[str, ...] = ()
    error_message: str = ""

    @model_validator(mode="after")
    def _normalize_result(self) -> "OdooPreviewDokployApplyResult":
        self.error_message = self.error_message.strip()
        self.rollback_errors = tuple(
            error.strip() for error in self.rollback_errors if error.strip()
        )
        if self.status in {"blocked", "fail"} and not self.error_message:
            raise ValueError("blocked or failed Odoo preview apply result requires error_message")
        return self


def build_odoo_preview_dokploy_dry_run(
    *, request: OdooPreviewDokployDryRunRequest
) -> OdooPreviewDokployDryRunPlan:
    runtime_plan = request.runtime_plan
    compose_ref = _compose_ref(runtime_plan=runtime_plan)
    compose_name = _compose_name(runtime_plan=runtime_plan, requested_name=request.compose_name)
    blockers: list[OdooPreviewDokployDryRunBlocker] = []

    if runtime_plan.status != "ready":
        blockers.append(
            _blocker(
                "runtime_plan_not_ready",
                "Odoo preview Dokploy dry-run requires a ready runtime plan.",
            )
        )
    domain_host = _domain_host(runtime_plan.preview_url)
    if not domain_host:
        blockers.append(
            _blocker(
                "preview_url_invalid",
                "Odoo preview Dokploy dry-run requires a preview URL with a hostname.",
            )
        )
    if runtime_plan.operation == "refresh" and runtime_plan.target is None:
        if not request.environment_id:
            blockers.append(
                _blocker(
                    "environment_id_missing",
                    "Odoo preview Dokploy dry-run requires environment_id before creating an isolated compose.",
                )
            )

    missing_paths = _missing_endpoint_paths(request=request)
    if missing_paths:
        blockers.append(
            _blocker(
                "endpoint_path_missing",
                "Odoo preview Dokploy dry-run is missing explicit endpoint paths: "
                + ", ".join(missing_paths),
            )
        )

    status: OdooPreviewDokployDryRunStatus = "blocked" if blockers else "ready"
    operations: tuple[OdooPreviewDokployOperation, ...] = ()
    rollback_operations: tuple[OdooPreviewDokployOperation, ...] = ()
    if status == "ready":
        operations = _operations(request=request, compose_ref=compose_ref, domain_host=domain_host)
        rollback_operations = _rollback_operations(
            request=request,
            compose_ref=compose_ref,
            domain_host=domain_host,
        )

    return OdooPreviewDokployDryRunPlan(
        status=status,
        operation=runtime_plan.operation,
        product=runtime_plan.product,
        repository=runtime_plan.repository,
        preview_slug=runtime_plan.preview_slug,
        preview_url=runtime_plan.preview_url,
        domain_host=domain_host,
        compose_ref=compose_ref,
        compose_name=compose_name,
        environment_id=request.environment_id,
        no_cache=request.no_cache,
        delete_volumes=request.delete_volumes,
        runtime_port=request.runtime_port,
        blockers=tuple(blockers),
        operations=operations,
        rollback_operations=rollback_operations,
        summary=(
            "Odoo preview Dokploy dry-run plan is ready"
            if status == "ready"
            else "Odoo preview Dokploy dry-run plan is blocked"
        ),
    )


def execute_odoo_preview_dokploy_apply(
    *,
    control_plane_root: Path,
    request: OdooPreviewDokployApplyRequest,
    database_url: str | None = None,
) -> OdooPreviewDokployApplyResult:
    plan = request.dry_run_plan
    if plan.status != "ready":
        return _apply_result(
            request=request,
            status="blocked",
            error_message="Odoo preview Dokploy apply requires a ready dry-run plan.",
        )

    missing_env_keys = tuple(
        key
        for key in ODOO_PREVIEW_REQUIRED_ENV_KEYS
        if not request.environment_values.get(key, "").strip()
    )
    if plan.operation != "destroy" and missing_env_keys:
        return _apply_result(
            request=request,
            status="blocked",
            error_message="Odoo preview Dokploy apply is missing runtime env keys: "
            + ", ".join(missing_env_keys),
        )

    host, token = control_plane_dokploy.read_dokploy_config(
        control_plane_root=control_plane_root,
        database_url=database_url,
    )
    if plan.operation == "destroy":
        return _execute_destroy(host=host, token=token, request=request)
    return _execute_refresh(host=host, token=token, request=request)


def _missing_endpoint_paths(*, request: OdooPreviewDokployDryRunRequest) -> tuple[str, ...]:
    spec = request.endpoint_spec
    runtime_plan = request.runtime_plan
    missing: list[str] = []
    if runtime_plan.operation == "refresh":
        if runtime_plan.target is None and not spec.compose_create_path:
            missing.append("compose_create_path")
        if not spec.compose_update_path:
            missing.append("compose_update_path")
        if not spec.domain_by_compose_path:
            missing.append("domain_by_compose_path")
        if not spec.domain_create_path:
            missing.append("domain_create_path")
        if not spec.domain_update_path:
            missing.append("domain_update_path")
        deploy_path = spec.compose_redeploy_path if request.no_cache else spec.compose_deploy_path
        if not deploy_path:
            missing.append("compose_redeploy_path" if request.no_cache else "compose_deploy_path")
        if runtime_plan.target is None and not spec.domain_delete_path:
            missing.append("domain_delete_path")
        if runtime_plan.target is None and not spec.compose_delete_path:
            missing.append("compose_delete_path")
    else:
        if not spec.domain_by_compose_path:
            missing.append("domain_by_compose_path")
        if not spec.domain_delete_path:
            missing.append("domain_delete_path")
        if not spec.compose_delete_path:
            missing.append("compose_delete_path")
    return tuple(missing)


def _execute_refresh(
    *, host: str, token: str, request: OdooPreviewDokployApplyRequest
) -> OdooPreviewDokployApplyResult:
    plan = request.dry_run_plan
    created_compose_id = ""
    domain_id = ""
    steps: list[OdooPreviewDokployApplyStep] = []
    try:
        compose_id = _resolve_or_create_compose(
            host=host,
            token=token,
            plan=plan,
            steps=steps,
        )
        created_compose_id = (
            compose_id if plan.compose_ref.startswith("${created.composeId:") else ""
        )
        target_payload = control_plane_dokploy.fetch_dokploy_target_payload(
            host=host,
            token=token,
            target_type="compose",
            target_id=compose_id,
        )
        compose_file = request.compose_file or control_plane_dokploy.render_odoo_raw_compose_file(
            image_reference=request.image_reference,
            domain_hosts=(plan.domain_host,),
            runtime_port=plan.runtime_port,
        )
        control_plane_dokploy.sync_dokploy_compose_raw_source(
            host=host,
            token=token,
            compose_id=compose_id,
            compose_name=plan.compose_name,
            target_payload=target_payload,
            compose_file=compose_file,
        )
        steps.append(_step("compose_update_raw_source", compose_id))

        env_text = control_plane_dokploy.serialize_dokploy_env_text(request.environment_values)
        control_plane_dokploy.update_dokploy_target_env(
            host=host,
            token=token,
            target_type="compose",
            target_id=compose_id,
            target_payload=target_payload,
            env_text=env_text,
        )
        steps.append(_step("compose_update_env", compose_id))

        domain_id = control_plane_dokploy.ensure_compose_web_domain_route(
            host=host,
            token=token,
            compose_id=compose_id,
            domain_host=plan.domain_host,
            runtime_port=plan.runtime_port,
        )
        steps.append(_step("domain_create_or_update", plan.domain_host))

        latest_before = control_plane_dokploy.latest_deployment_for_target(
            host=host,
            token=token,
            target_type="compose",
            target_id=compose_id,
        )
        control_plane_dokploy.trigger_deployment(
            host=host,
            token=token,
            target_type="compose",
            target_id=compose_id,
            no_cache=plan.no_cache,
        )
        steps.append(_step("compose_deploy", compose_id))
        if request.wait_for_deploy:
            control_plane_dokploy.wait_for_target_deployment(
                host=host,
                token=token,
                target_type="compose",
                target_id=compose_id,
                before_key=control_plane_dokploy.deployment_key(latest_before),
                timeout_seconds=request.timeout_seconds,
            )
        if request.smoke_check:
            _wait_for_smoke_check(
                preview_url=plan.preview_url,
                health_path=request.health_path,
                timeout_seconds=request.timeout_seconds,
            )
            steps.append(_step("smoke_check", plan.preview_url))
        return _apply_result(
            request=request,
            status="pass",
            compose_id=compose_id,
            domain_id=domain_id,
            created_compose=bool(created_compose_id),
            steps=tuple(steps),
        )
    except click.ClickException as exc:
        rollback_errors = _rollback_created_runtime(
            host=host,
            token=token,
            domain_id=domain_id,
            compose_id=created_compose_id,
            delete_volumes=plan.delete_volumes,
        )
        return _apply_result(
            request=request,
            status="fail",
            error_message=str(exc),
            domain_id=domain_id,
            compose_id=created_compose_id,
            created_compose=bool(created_compose_id),
            steps=tuple(steps),
            rollback_errors=rollback_errors,
        )


def _execute_destroy(
    *, host: str, token: str, request: OdooPreviewDokployApplyRequest
) -> OdooPreviewDokployApplyResult:
    plan = request.dry_run_plan
    compose_id = plan.compose_ref
    steps: list[OdooPreviewDokployApplyStep] = []
    domain_ids: list[str] = []
    try:
        for domain in _compose_domains(host=host, token=token, compose_id=compose_id):
            domain_id = str(domain.get("domainId") or "").strip()
            domain_host = str(domain.get("host") or "").strip().lower()
            if domain_id and domain_host == plan.domain_host:
                domain_ids.append(domain_id)
        steps.append(_step("domain_lookup", compose_id))
        for domain_id in domain_ids:
            _delete_domain(host=host, token=token, domain_id=domain_id)
            steps.append(_step("domain_delete", domain_id))
        _delete_compose(
            host=host,
            token=token,
            compose_id=compose_id,
            delete_volumes=plan.delete_volumes,
        )
        steps.append(_step("compose_delete", compose_id))
        return _apply_result(
            request=request,
            status="pass",
            compose_id=compose_id,
            domain_id=domain_ids[0] if domain_ids else "",
            steps=tuple(steps),
        )
    except click.ClickException as exc:
        return _apply_result(
            request=request,
            status="fail",
            error_message=str(exc),
            compose_id=compose_id,
            domain_id=domain_ids[0] if domain_ids else "",
            steps=tuple(steps),
        )


def _resolve_or_create_compose(
    *,
    host: str,
    token: str,
    plan: OdooPreviewDokployDryRunPlan,
    steps: list[OdooPreviewDokployApplyStep],
) -> str:
    if not plan.compose_ref.startswith("${created.composeId:"):
        return plan.compose_ref
    if not plan.environment_id:
        raise click.ClickException("Odoo preview compose create requires environment_id.")
    created = control_plane_dokploy.dokploy_request(
        host=host,
        token=token,
        path="/api/compose.create",
        method="POST",
        payload={
            "name": plan.compose_name,
            "appName": plan.compose_name,
            "description": f"Launchplane Odoo preview {plan.preview_slug}",
            "environmentId": plan.environment_id,
            "composeType": "docker-compose",
        },
    )
    created_compose = control_plane_dokploy.as_json_object(created)
    compose_id = str((created_compose or {}).get("composeId") or "").strip()
    if not compose_id:
        raise click.ClickException(
            f"Dokploy did not return composeId for Odoo preview compose {plan.compose_name!r}."
        )
    steps.append(_step("compose_create", plan.compose_name))
    return compose_id


def _compose_domains(*, host: str, token: str, compose_id: str) -> tuple[JsonObject, ...]:
    raw_domains = control_plane_dokploy.dokploy_request(
        host=host,
        token=token,
        path="/api/domain.byComposeId",
        query={"composeId": compose_id},
    )
    if not isinstance(raw_domains, list):
        return ()
    domains: list[JsonObject] = []
    for raw_domain in raw_domains:
        domain = control_plane_dokploy.as_json_object(raw_domain)
        if domain is not None:
            domains.append(domain)
    return tuple(domains)


def _delete_domain(*, host: str, token: str, domain_id: str) -> None:
    control_plane_dokploy.dokploy_request(
        host=host,
        token=token,
        path="/api/domain.delete",
        method="POST",
        payload={"domainId": domain_id},
    )


def _delete_compose(*, host: str, token: str, compose_id: str, delete_volumes: bool) -> None:
    control_plane_dokploy.dokploy_request(
        host=host,
        token=token,
        path="/api/compose.delete",
        method="POST",
        payload={"composeId": compose_id, "deleteVolumes": delete_volumes},
    )


def _rollback_created_runtime(
    *, host: str, token: str, domain_id: str, compose_id: str, delete_volumes: bool
) -> tuple[str, ...]:
    errors: list[str] = []
    if domain_id:
        try:
            _delete_domain(host=host, token=token, domain_id=domain_id)
        except click.ClickException as exc:
            errors.append(f"domain rollback failed: {exc}")
    if compose_id:
        try:
            _delete_compose(
                host=host,
                token=token,
                compose_id=compose_id,
                delete_volumes=delete_volumes,
            )
        except click.ClickException as exc:
            errors.append(f"compose rollback failed: {exc}")
    return tuple(errors)


def _wait_for_smoke_check(*, preview_url: str, health_path: str, timeout_seconds: int) -> None:
    parsed = urlparse(preview_url.rstrip("/"))
    smoke_url = parsed._replace(path=health_path, params="", query="", fragment="").geturl()
    request = Request(
        smoke_url,
        headers={"Accept": "application/json, text/plain, */*", "Cache-Control": "no-store"},
    )
    deadline = timeout_seconds
    last_http_status: int | None = None
    while deadline > 0:
        try:
            with urlopen(request, timeout=min(15, deadline)) as response:
                response.read()
            if 200 <= response.status < 400:
                return
        except HTTPError as exc:
            last_http_status = exc.code
        except (TimeoutError, URLError, ValueError):
            pass
        sleep_seconds = min(5, deadline)
        time.sleep(sleep_seconds)
        deadline -= sleep_seconds
    if last_http_status is not None:
        raise click.ClickException(
            f"Odoo preview smoke check returned HTTP {last_http_status}."
        )
    raise click.ClickException(f"Timed out waiting for Odoo preview smoke check {smoke_url}.")


def _step(name: OdooPreviewDokployOperationName, target: str) -> OdooPreviewDokployApplyStep:
    return OdooPreviewDokployApplyStep(name=name, target=target)


def _apply_result(
    *,
    request: OdooPreviewDokployApplyRequest,
    status: OdooPreviewDokployApplyStatus,
    compose_id: str = "",
    domain_id: str = "",
    created_compose: bool = False,
    steps: tuple[OdooPreviewDokployApplyStep, ...] = (),
    rollback_errors: tuple[str, ...] = (),
    error_message: str = "",
) -> OdooPreviewDokployApplyResult:
    plan = request.dry_run_plan
    return OdooPreviewDokployApplyResult(
        status=status,
        operation=plan.operation,
        product=plan.product,
        repository=plan.repository,
        preview_slug=plan.preview_slug,
        preview_url=plan.preview_url,
        domain_host=plan.domain_host,
        compose_id=compose_id,
        compose_name=plan.compose_name,
        created_compose=created_compose,
        domain_id=domain_id,
        steps=steps,
        rollback_errors=rollback_errors,
        error_message=error_message,
    )


def _operations(
    *, request: OdooPreviewDokployDryRunRequest, compose_ref: str, domain_host: str
) -> tuple[OdooPreviewDokployOperation, ...]:
    runtime_plan = request.runtime_plan
    spec = request.endpoint_spec
    if runtime_plan.operation == "destroy":
        return (
            OdooPreviewDokployOperation(
                name="domain_lookup",
                method="GET",
                path=spec.domain_by_compose_path,
                target=compose_ref,
                payload_keys=("composeId",),
            ),
            OdooPreviewDokployOperation(
                name="domain_delete",
                method="POST",
                path=spec.domain_delete_path,
                target=domain_host,
                payload_keys=("domainId",),
            ),
            OdooPreviewDokployOperation(
                name="compose_delete",
                method="POST",
                path=spec.compose_delete_path,
                target=compose_ref,
                payload_keys=("composeId", "deleteVolumes"),
            ),
        )

    operations: list[OdooPreviewDokployOperation] = []
    if runtime_plan.target is None:
        operations.append(
            OdooPreviewDokployOperation(
                name="compose_create",
                method="POST",
                path=spec.compose_create_path,
                target=_compose_name(
                    runtime_plan=runtime_plan, requested_name=request.compose_name
                ),
                payload_keys=("name", "appName", "environmentId", "composeType"),
            )
        )
    operations.extend(
        (
            OdooPreviewDokployOperation(
                name="compose_update_raw_source",
                method="POST",
                path=spec.compose_update_path,
                target=compose_ref,
                payload_keys=(
                    "composeId",
                    "name",
                    "environmentId",
                    "sourceType",
                    "composePath",
                    "composeFile",
                ),
            ),
            OdooPreviewDokployOperation(
                name="compose_update_env",
                method="POST",
                path=spec.compose_update_path,
                target=compose_ref,
                payload_keys=("composeId", "env"),
                secret_payload=True,
            ),
            OdooPreviewDokployOperation(
                name="domain_lookup",
                method="GET",
                path=spec.domain_by_compose_path,
                target=compose_ref,
                payload_keys=("composeId",),
            ),
            OdooPreviewDokployOperation(
                name="domain_create_or_update",
                method="POST",
                path=spec.domain_create_path,
                target=domain_host,
                payload_keys=("host", "port", "composeId", "serviceName", "domainType"),
            ),
            OdooPreviewDokployOperation(
                name="compose_deploy",
                method="POST",
                path=spec.compose_redeploy_path if request.no_cache else spec.compose_deploy_path,
                target=compose_ref,
                payload_keys=("composeId",),
            ),
            OdooPreviewDokployOperation(
                name="smoke_check",
                method="LOCAL",
                target=runtime_plan.preview_url,
            ),
        )
    )
    return tuple(operations)


def _rollback_operations(
    *, request: OdooPreviewDokployDryRunRequest, compose_ref: str, domain_host: str
) -> tuple[OdooPreviewDokployOperation, ...]:
    if request.runtime_plan.operation == "destroy" or request.runtime_plan.target is not None:
        return ()
    spec = request.endpoint_spec
    return (
        OdooPreviewDokployOperation(
            name="domain_delete",
            method="POST",
            path=spec.domain_delete_path,
            target=domain_host,
            payload_keys=("domainId",),
        ),
        OdooPreviewDokployOperation(
            name="compose_delete",
            method="POST",
            path=spec.compose_delete_path,
            target=compose_ref,
            payload_keys=("composeId", "deleteVolumes"),
        ),
    )


def _compose_ref(*, runtime_plan: OdooPreviewRuntimePlan) -> str:
    if runtime_plan.target is not None:
        return runtime_plan.target.target_id
    return f"${{created.composeId:{runtime_plan.product}-{runtime_plan.preview_slug}}}"


def _compose_name(*, runtime_plan: OdooPreviewRuntimePlan, requested_name: str) -> str:
    if requested_name.strip():
        return requested_name.strip()
    if runtime_plan.target is not None:
        return runtime_plan.target.target_name
    return f"{runtime_plan.product}-{runtime_plan.preview_slug}"


def _domain_host(preview_url: str) -> str:
    parsed = urlparse(preview_url.strip())
    return (parsed.hostname or "").strip().lower()


def _blocker(
    code: OdooPreviewDokployDryRunBlockerCode, message: str
) -> OdooPreviewDokployDryRunBlocker:
    return OdooPreviewDokployDryRunBlocker(code=code, message=message)


def _required_text(value: str, message: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(message)
    return normalized


def _normalized_unique_texts(values: tuple[str, ...]) -> tuple[str, ...]:
    normalized: list[str] = []
    for raw_value in values:
        value = raw_value.strip()
        if not value:
            raise ValueError("Odoo preview Dokploy operation payload keys must be non-empty")
        if value not in normalized:
            normalized.append(value)
    return tuple(normalized)
