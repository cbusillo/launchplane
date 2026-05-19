from __future__ import annotations

from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.odoo_preview_runtime_plan import (
    OdooPreviewRuntimeOperation,
    OdooPreviewRuntimePlan,
    OdooPreviewRuntimePlanStatus,
)


OdooPreviewDokployDryRunStatus = OdooPreviewRuntimePlanStatus
OdooPreviewDokployDryRunBlockerCode = Literal[
    "endpoint_path_missing",
    "preview_url_invalid",
    "runtime_plan_not_ready",
]
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


class OdooPreviewDokployEndpointSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    compose_create_path: str = ""
    compose_update_path: str = "/api/compose.update"
    compose_deploy_path: str = "/api/compose.deploy"
    compose_redeploy_path: str = "/api/compose.redeploy"
    compose_delete_path: str = ""
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
        self.blockers = tuple(sorted(self.blockers, key=lambda blocker: blocker.code))
        self.summary = _required_text(self.summary, "Odoo preview dry-run plan requires summary")
        if self.status == "ready" and self.blockers:
            raise ValueError("ready Odoo preview dry-run plan cannot include blockers")
        if self.status == "blocked" and not self.blockers:
            raise ValueError("blocked Odoo preview dry-run plan requires blockers")
        if self.status == "blocked" and (self.operations or self.rollback_operations):
            raise ValueError("blocked Odoo preview dry-run plan cannot include operations")
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
        blockers=tuple(blockers),
        operations=operations,
        rollback_operations=rollback_operations,
        summary=(
            "Odoo preview Dokploy dry-run plan is ready"
            if status == "ready"
            else "Odoo preview Dokploy dry-run plan is blocked"
        ),
    )


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
                payload_keys=("composeId",),
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
                payload_keys=("name", "environmentId"),
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
                path=f"{spec.domain_create_path}|{spec.domain_update_path}",
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
            payload_keys=("composeId",),
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
