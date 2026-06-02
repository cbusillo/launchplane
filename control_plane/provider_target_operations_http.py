from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.service_auth import (
    LaunchplaneAuthzPolicy,
    LaunchplaneIdentity,
    LocalAdminIdentity,
    LocalOperatorIdentity,
)
from control_plane.workflows.provider_target_audit import (
    ProviderTargetAuditRecordStore,
    audit_provider_targets,
)
from control_plane.workflows.provider_target_backfill import (
    ProviderTargetBackfillStore,
    backfill_provider_targets,
)


LAUNCHPLANE_SERVICE_CONTEXT = "launchplane"
PROVIDER_TARGET_OPERATIONS_ROUTE = "/v1/provider-targets/operations"

ProviderTargetOperationMode = Literal["audit", "backfill-dry-run", "backfill-apply"]


class ProviderTargetOperationStore(
    ProviderTargetAuditRecordStore,
    ProviderTargetBackfillStore,
    Protocol,
):
    pass


class ProviderTargetOperationEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    mode: ProviderTargetOperationMode
    product: str = "launchplane"
    provider_id: str = "dokploy"
    context: str
    instance: str
    reason: str = ""

    @model_validator(mode="after")
    def _validate_request(self) -> "ProviderTargetOperationEnvelope":
        self.product = self.product.strip()
        self.provider_id = self.provider_id.strip().lower()
        self.context = self.context.strip()
        self.instance = self.instance.strip()
        self.reason = self.reason.strip()
        if self.product != "launchplane":
            raise ValueError("provider-target operations require product 'launchplane'")
        if not self.provider_id:
            raise ValueError("provider-target operations require provider_id")
        if not self.context:
            raise ValueError("provider-target operations require context")
        if not self.instance:
            raise ValueError("provider-target operations require instance")
        if self.mode == "backfill-apply" and not self.reason:
            raise ValueError("provider-target backfill apply requires reason")
        return self


@dataclass(frozen=True)
class ProviderTargetOperationRouteResult:
    result: dict[str, object]
    driver_result: dict[str, object]


def provider_target_operation_authorized(
    *,
    authz_policy: LaunchplaneAuthzPolicy,
    identity: LaunchplaneIdentity,
    request: ProviderTargetOperationEnvelope,
) -> bool:
    action = (
        "provider_target.backfill"
        if request.mode == "backfill-apply"
        else "provider_target.audit"
    )
    return authz_policy.allows(
        identity=identity,
        action=action,
        product=request.product,
        context=LAUNCHPLANE_SERVICE_CONTEXT,
    )


def provider_target_operation_requires_reason(
    *, identity: LaunchplaneIdentity, request: ProviderTargetOperationEnvelope
) -> bool:
    return (
        isinstance(identity, (LocalOperatorIdentity, LocalAdminIdentity))
        and request.mode == "backfill-apply"
        and not request.reason
    )


def execute_provider_target_operation_route(
    *,
    record_store: ProviderTargetOperationStore,
    request: ProviderTargetOperationEnvelope,
) -> ProviderTargetOperationRouteResult:
    if request.mode == "audit":
        audit_result = audit_provider_targets(
            record_store,
            provider_id=request.provider_id,
            context_name=request.context,
            instance_name=request.instance,
        )
        report = audit_result.to_report_payload()
    else:
        backfill_result = backfill_provider_targets(
            record_store,
            apply=request.mode == "backfill-apply",
            provider_id=request.provider_id,
            context_name=request.context,
            instance_name=request.instance,
        )
        report = backfill_result.to_report_payload()
    result = {
        "mode": request.mode,
        "provider_id": request.provider_id,
        "context": request.context,
        "instance": request.instance,
        "operation_status": str(report.get("status", "")),
        "blocked_count": _report_int(report.get("blocked_count")),
        "warning_count": _report_int(report.get("warning_count")),
        "inspected_route_count": _report_int(report.get("inspected_route_count")),
    }
    return ProviderTargetOperationRouteResult(
        result=result,
        driver_result={
            **result,
            "reason": request.reason,
            "report": report,
        },
    )


def _report_int(value: object) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return 0
