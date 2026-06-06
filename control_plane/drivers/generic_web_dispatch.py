from __future__ import annotations

from pathlib import Path
from typing import Protocol, cast

import click
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from control_plane.contracts.promotion_record import HealthcheckEvidence, ReleaseStatus
from control_plane.drivers.dispatch import (
    _DescriptorDriverDispatchResult,
    _DriverRouteExecutionMetadata,
    _ProductRouteEnvelope,
    _ResolvedProductDriverContext,
    ProductDriverMismatchError,
    _normalize_preview_verification_checked_urls,
    _normalize_release_status,
    _validate_driver_envelope_product,
)
from control_plane.workflows.evidence_ingestion import (
    EvidenceIngestionStore,
    apply_deployment_evidence,
    apply_promotion_evidence,
)


class _StableVerificationRequest(Protocol):
    context: str
    instance: str
    deployment_record_id: str
    verification_status: ReleaseStatus
    verified_at: str
    checked_urls: tuple[str, ...]
    timeout_seconds: int | None


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


def _stable_verification_health_evidence(
    *, request: GenericWebStableVerificationRequest
) -> HealthcheckEvidence:
    return HealthcheckEvidence(
        verified=bool(request.checked_urls),
        urls=request.checked_urls,
        timeout_seconds=request.timeout_seconds if request.checked_urls else None,
        status=request.verification_status,
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

    health_evidence = _stable_verification_health_evidence(request=request)
    updated_deployment = deployment_record.model_copy(
        update={"destination_health": health_evidence}
    )
    result: dict[str, object] = dict(
        apply_deployment_evidence(
            record_store=evidence_store,
            deployment_record=updated_deployment,
        )
    )
    result["deployment_health_status"] = request.verification_status

    promotion_record_id = request.promotion_record_id.strip()
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
        if promotion_record.deployment_record_id.strip() not in {
            "",
            request.deployment_record_id,
        }:
            raise click.ClickException(
                f"{label} deployment_record_id does not match linked promotion record."
            )
        updated_promotion = promotion_record.model_copy(
            update={"destination_health": health_evidence}
        )
        result.update(
            apply_promotion_evidence(
                record_store=evidence_store,
                promotion_record=updated_promotion,
            )
        )
        result["promotion_health_status"] = request.verification_status

    return result


def _handle_generic_web_stable_verification(
    request: GenericWebStableVerificationEnvelope,
    resolved_context: _ResolvedProductDriverContext,
    record_store: object,
    control_plane_root_path: Path,
) -> _DescriptorDriverDispatchResult:
    del control_plane_root_path
    if resolved_context.lane is None:
        raise ProductDriverMismatchError(
            "Generic web stable verification requires a product profile lane."
        )
    return _DescriptorDriverDispatchResult(
        result=_apply_generic_web_stable_verification_records(
            record_store=record_store,
            request=request.verification,
        )
    )
