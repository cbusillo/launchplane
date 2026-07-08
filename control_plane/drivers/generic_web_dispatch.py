from __future__ import annotations

from typing import Protocol, cast

import click
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from control_plane.contracts.generic_web_rollback import (
    GenericWebRollbackPlanRequest,
)
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.promotion_record import HealthcheckEvidence, ReleaseStatus
from control_plane.contracts.runtime_identity import health_payload_runtime_identity_status
from control_plane.contracts.structured_health import structured_health_evidence_from_payload
from control_plane.drivers.dispatch import (
    _DriverRouteExecutionMetadata,
    _ProductRouteEnvelope,
    _normalize_preview_verification_checked_urls,
    _normalize_release_status,
    _validate_driver_envelope_product,
)
from control_plane.workflows.evidence_ingestion import (
    EvidenceIngestionStore,
    apply_deployment_evidence,
    apply_promotion_evidence,
)
from control_plane.workflows.generic_web_deploy import (
    GenericWebDeployRequest,
    GenericWebPostDeployExecutor,
)
from control_plane.workflows.generic_web_promotion import (
    GenericWebProdPromotionRequest,
)
from control_plane.workflows.generic_web_promotion_workflow import (
    GenericWebPromotionWorkflowRequest,
)
from control_plane.workflows.odoo_generic_web_post_deploy import (
    generic_web_post_deploy_executor_for_driver_id,
)


class _StableVerificationRequest(Protocol):
    context: str
    instance: str
    deployment_record_id: str
    verification_status: ReleaseStatus
    verified_at: str
    checked_urls: tuple[str, ...]
    timeout_seconds: int | None
    health_payload: object | None


def _validate_stable_verification_request(
    request: _StableVerificationRequest,
    *,
    label: str,
) -> None:
    if not request.context.strip():
        raise ValueError(f"{label} requires context.")
    if not request.instance.strip():
        raise ValueError(f"{label} requires instance.")
    if not request.deployment_record_id.strip():
        raise ValueError(f"{label} requires deployment_record_id.")
    if request.verification_status not in {"pass", "fail"}:
        raise ValueError(f"{label} status must be pass or fail.")
    if not request.verified_at.strip():
        raise ValueError(f"{label} requires verified_at.")
    if request.checked_urls and request.timeout_seconds is None:
        raise ValueError(f"{label} checked_urls require timeout_seconds.")


class GenericWebStableVerificationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    context: str
    instance: str
    deployment_record_id: str
    promotion_record_id: str = ""
    verification_status: ReleaseStatus
    verified_at: str
    checked_urls: tuple[str, ...] = ()
    timeout_seconds: int | None = Field(default=None, ge=1)
    health_payload: object | None = None
    failure_summary: str = ""

    @field_validator("verification_status", mode="before")
    @classmethod
    def _normalize_status(cls, value: object) -> ReleaseStatus:
        return _normalize_release_status(value, label="Generic web stable verification status")

    @field_validator("checked_urls", mode="before")
    @classmethod
    def _normalize_checked_urls(cls, value: object) -> tuple[str, ...]:
        return _normalize_preview_verification_checked_urls(
            value, label="Generic web stable verification"
        )

    @model_validator(mode="after")
    def _validate_request(self) -> "GenericWebStableVerificationRequest":
        _validate_stable_verification_request(self, label="Generic web stable verification")
        return self


class GenericWebStableVerificationEnvelope(_ProductRouteEnvelope):
    schema_version: int = Field(default=1, ge=1)
    verification: GenericWebStableVerificationRequest

    @model_validator(mode="after")
    def _validate_alignment(self) -> "GenericWebStableVerificationEnvelope":
        _validate_driver_envelope_product(self.product, label="Generic web stable verification")
        return self


_GENERIC_WEB_STABLE_VERIFICATION_ROUTE = _DriverRouteExecutionMetadata(
    route_path="/v1/drivers/generic-web/stable-verification",
    envelope_model=GenericWebStableVerificationEnvelope,
    denial_message=(
        "Workflow cannot write generic web stable verification for the requested product/context."
    ),
)


class GenericWebDeployEnvelope(_ProductRouteEnvelope):
    schema_version: int = Field(default=1, ge=1)
    deploy: GenericWebDeployRequest

    @model_validator(mode="after")
    def _validate_alignment(self) -> "GenericWebDeployEnvelope":
        if not self.product.strip():
            raise ValueError("generic web deploy requires product")
        if self.product.strip() != self.deploy.product.strip():
            raise ValueError("generic web deploy requires matching product values")
        return self


_GENERIC_WEB_DEPLOY_ROUTE = _DriverRouteExecutionMetadata(
    route_path="/v1/drivers/generic-web/deploy",
    envelope_model=GenericWebDeployEnvelope,
    denial_message=(
        "Workflow cannot execute the generic web deploy driver for the requested product/context."
    ),
)


class GenericWebProdPromotionEnvelope(_ProductRouteEnvelope):
    schema_version: int = Field(default=1, ge=1)
    promotion: GenericWebProdPromotionRequest

    @model_validator(mode="after")
    def _validate_alignment(self) -> "GenericWebProdPromotionEnvelope":
        if not self.product.strip():
            raise ValueError("generic web prod promotion requires product")
        if self.product.strip() != self.promotion.product.strip():
            raise ValueError("generic web prod promotion requires matching product values")
        return self


_GENERIC_WEB_PROD_PROMOTION_ROUTE = _DriverRouteExecutionMetadata(
    route_path="/v1/drivers/generic-web/prod-promotion",
    envelope_model=GenericWebProdPromotionEnvelope,
    denial_message=(
        "Workflow cannot execute the generic web prod promotion driver"
        " for the requested product/context."
    ),
)


class GenericWebRollbackPlanEnvelope(_ProductRouteEnvelope):
    schema_version: int = Field(default=1, ge=1)
    rollback_plan: GenericWebRollbackPlanRequest

    @model_validator(mode="after")
    def _validate_alignment(self) -> "GenericWebRollbackPlanEnvelope":
        if not self.product.strip():
            raise ValueError("generic web rollback plan requires product")
        if self.product.strip() != self.rollback_plan.product.strip():
            raise ValueError("generic web rollback plan requires matching product values")
        return self


_GENERIC_WEB_ROLLBACK_PLAN_ROUTE = _DriverRouteExecutionMetadata(
    route_path="/v1/drivers/generic-web/prod-rollback-plan",
    envelope_model=GenericWebRollbackPlanEnvelope,
    denial_message=(
        "Workflow cannot plan the generic web prod rollback for the requested product/context."
    ),
)


class GenericWebRollbackEnvelope(_ProductRouteEnvelope):
    schema_version: int = Field(default=1, ge=1)
    rollback: GenericWebRollbackPlanRequest

    @model_validator(mode="after")
    def _validate_alignment(self) -> "GenericWebRollbackEnvelope":
        if not self.product.strip():
            raise ValueError("generic web rollback requires product")
        if self.product.strip() != self.rollback.product.strip():
            raise ValueError("generic web rollback requires matching product values")
        return self


_GENERIC_WEB_ROLLBACK_ROUTE = _DriverRouteExecutionMetadata(
    route_path="/v1/drivers/generic-web/prod-rollback",
    envelope_model=GenericWebRollbackEnvelope,
    denial_message=(
        "Workflow cannot execute the generic web prod rollback for the requested product/context."
    ),
)


class GenericWebPromotionWorkflowEnvelope(_ProductRouteEnvelope):
    schema_version: int = Field(default=1, ge=1)
    workflow: GenericWebPromotionWorkflowRequest

    @model_validator(mode="after")
    def _validate_alignment(self) -> "GenericWebPromotionWorkflowEnvelope":
        if not self.product.strip():
            raise ValueError("generic web promotion workflow requires product")
        if self.product.strip() != self.workflow.product.strip():
            raise ValueError("generic web promotion workflow requires matching product values")
        return self


_GENERIC_WEB_PROD_PROMOTION_WORKFLOW_ROUTE = _DriverRouteExecutionMetadata(
    route_path="/v1/drivers/generic-web/prod-promotion-workflow",
    envelope_model=GenericWebPromotionWorkflowEnvelope,
    denial_message=(
        "Caller cannot dispatch the generic web prod promotion workflow"
        " for the requested product/context."
    ),
)


def _generic_web_post_deploy_executor_for_profile(
    profile: LaunchplaneProductProfileRecord,
) -> GenericWebPostDeployExecutor | None:
    return generic_web_post_deploy_executor_for_driver_id(profile.driver_id)


def _stable_verification_health_evidence(
    *,
    request: GenericWebStableVerificationRequest,
    deployment_record: object,
) -> HealthcheckEvidence:
    status = request.verification_status
    structured_health = structured_health_evidence_from_payload(request.health_payload)
    if structured_health.status in {"fail", "malformed"}:
        status = "fail"
    runtime_identity_status = "unchecked"
    runtime_identity_detail = ""
    observed_runtime_identity = None
    expected_runtime_identity = getattr(deployment_record, "runtime_identity", None)
    runtime_identity_status, runtime_identity_detail, observed_runtime_identity = (
        health_payload_runtime_identity_status(
            expected=expected_runtime_identity,
            payload=request.health_payload,
            json_parse_failed=False,
        )
    )
    if runtime_identity_status in {"malformed", "mismatch", "missing", "unverifiable"}:
        status = "fail"
    return HealthcheckEvidence(
        verified=bool(request.checked_urls),
        urls=request.checked_urls,
        timeout_seconds=request.timeout_seconds if request.checked_urls else None,
        status=status,
        runtime_identity_status=runtime_identity_status,
        runtime_identity_detail=runtime_identity_detail,
        observed_runtime_identity=observed_runtime_identity,
        structured_health=structured_health,
    )


def _validate_stable_verification_health_payload_identity(
    *, request: GenericWebStableVerificationRequest, deployment_record: object
) -> None:
    if request.health_payload is None:
        return
    structured_health = structured_health_evidence_from_payload(request.health_payload)
    if structured_health.status in {"fail", "malformed"}:
        return
    expected_runtime_identity = getattr(deployment_record, "runtime_identity", None)
    if expected_runtime_identity is None:
        raise click.ClickException(
            "Generic web stable verification health payload cannot be verified because "
            "the deployment record does not include an expected runtime identity."
        )
    runtime_identity_status, runtime_identity_detail, _observed_runtime_identity = (
        health_payload_runtime_identity_status(
            expected=expected_runtime_identity,
            payload=request.health_payload,
            json_parse_failed=False,
        )
    )
    if runtime_identity_status != "match":
        raise click.ClickException(
            "Generic web stable verification health payload did not match the expected "
            f"runtime identity: {runtime_identity_detail}"
        )


def _apply_generic_web_stable_verification_records(
    *,
    record_store: object,
    request: GenericWebStableVerificationRequest,
    label: str = "Generic web stable verification",
) -> dict[str, object]:
    evidence_store = cast(EvidenceIngestionStore, record_store)
    try:
        deployment_record = evidence_store.read_deployment_record(request.deployment_record_id)
    except FileNotFoundError as exc:
        raise click.ClickException(
            f"No Launchplane deployment record found for {request.deployment_record_id}."
        ) from exc
    if deployment_record.context != request.context:
        raise click.ClickException(f"{label} context does not match deployment record context.")
    if deployment_record.instance != request.instance:
        raise click.ClickException(f"{label} instance does not match deployment record instance.")

    _validate_stable_verification_health_payload_identity(
        request=request,
        deployment_record=deployment_record,
    )
    health_evidence = _stable_verification_health_evidence(
        request=request,
        deployment_record=deployment_record,
    )
    promotion_record_id = request.promotion_record_id.strip()
    promotion_record = None
    if promotion_record_id:
        try:
            promotion_record = evidence_store.read_promotion_record(promotion_record_id)
        except FileNotFoundError as exc:
            raise click.ClickException(
                f"No Launchplane promotion record found for {promotion_record_id}."
            ) from exc
        if promotion_record.context != request.context:
            raise click.ClickException(f"{label} context does not match promotion record context.")
        if promotion_record.to_instance != request.instance:
            raise click.ClickException(
                f"{label} instance does not match promotion destination instance."
            )
        if promotion_record.deployment_record_id.strip() != request.deployment_record_id:
            raise click.ClickException(
                f"{label} deployment_record_id does not match linked promotion record."
            )

    updated_deployment = deployment_record.model_copy(
        update={"destination_health": health_evidence}
    )
    result: dict[str, object] = dict(
        apply_deployment_evidence(
            record_store=evidence_store,
            deployment_record=updated_deployment,
        )
    )
    result["deployment_health_status"] = health_evidence.status

    if promotion_record is not None:
        updated_promotion = promotion_record.model_copy(
            update={"destination_health": health_evidence}
        )
        result.update(
            apply_promotion_evidence(
                record_store=evidence_store,
                promotion_record=updated_promotion,
            )
        )
        result["promotion_health_status"] = health_evidence.status

    return result
