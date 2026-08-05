from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol, cast

import click
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from control_plane.contracts.data_provenance import FreshnessStatus
from control_plane.contracts.environment_inventory import EnvironmentInventory
from control_plane.contracts.authz_policy_record import LaunchplaneAuthzPolicyRecord
from control_plane.contracts.lane_summary import LaunchplaneLaneSummary
from control_plane.contracts.manager_preview_approval import ManagerPreviewApprovalDecision
from control_plane.contracts.outbox_delivery import OutboxDeliveryRecord
from control_plane.contracts.preview_generation_record import PreviewGenerationRecord
from control_plane.contracts.preview_record import PreviewRecord
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductLaneProfile,
)
from control_plane.contracts.promotion_record import ReleaseStatus
from control_plane.contracts.runtime_identity import RuntimeIdentity, RuntimeIdentityStatus
from control_plane.contracts.deploy_reference import validate_provider_deploy_reference
from control_plane.drivers.generic_web_dispatch import (
    GenericWebProdPromotionEnvelope,
    GenericWebPromotionWorkflowEnvelope,
)
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.generic_web_promotion_http import (
    GenericWebProdPromotionRecords,
    GenericWebProdPromotionResponseResult,
    GenericWebPromotionWorkflowResponseResult,
)
from control_plane.manager_preview_approval import (
    build_current_manager_preview_approval_binding,
    evaluate_manager_preview_approval,
    manager_preview_approval_required,
)
from control_plane.workflows.generic_web_deploy import (
    normalize_generic_web_artifact_id,
    product_profile_uses_generic_web_base,
)
from control_plane.workflows.generic_web_promotion import GenericWebProdPromotionRequest
from control_plane.workflows.generic_web_promotion_workflow import (
    BumpLevel,
    GenericWebPromotionWorkflowRequest,
)


PRODUCT_PROMOTION_INVENTORY_STALE_AFTER = timedelta(minutes=30)
PRODUCT_PROMOTION_INVENTORY_CLOCK_SKEW = timedelta(minutes=5)
PRODUCT_PROMOTION_DRY_RUN_ROUTE = (
    "/v1/products/{product}/environments/{environment}/promotion/dry-run"
)
PRODUCT_PROMOTION_DRY_RUN_MARKER_ROUTE = f"{PRODUCT_PROMOTION_DRY_RUN_ROUTE}/accepted"
PRODUCT_PROMOTION_STATUS_ROUTE = (
    "/v1/products/{product}/environments/{environment}/promotion-status"
)
PRODUCT_PROMOTION_WORKFLOW_ROUTE = (
    "/v1/products/{product}/environments/{environment}/promotion/workflow-dispatch"
)
PRODUCT_PROMOTION_WORKFLOW_STATUS_ROUTE = (
    "/v1/products/{product}/environments/{environment}/promotion/workflow-deliveries/{delivery_id}"
)

ProductPromotionOperationKind = Literal[
    "direct_dry_run",
    "workflow_dry_run",
    "workflow_live",
]


class ProductPromotionStatusStore(Protocol):
    def read_product_profile_record(self, product: str) -> LaunchplaneProductProfileRecord: ...

    def read_lane_summary(
        self,
        *,
        context_name: str,
        instance_name: str,
    ) -> LaunchplaneLaneSummary: ...


class ProductPromotionEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    environment: str
    artifact_id: str = ""
    deploy_reference: str = ""
    source_git_ref: str = ""
    deployment_record_id: str = ""
    inventory_updated_at: str = ""
    inventory_stale_after: str = ""
    deployment_status: ReleaseStatus = "skipped"
    health_status: ReleaseStatus = "skipped"
    runtime_identity_status: RuntimeIdentityStatus = "unchecked"
    runtime_identity_detail: str = ""
    trust_state: FreshnessStatus = "missing"


class ProductPromotionOperationAvailability(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: ProductPromotionOperationKind
    authz_action: str
    enabled: bool
    disabled_reasons: tuple[str, ...] = ()
    requires_reason: bool = True
    requires_idempotency_key: bool = True
    requires_matching_direct_dry_run: bool = False
    requires_confirmation: bool = False
    consequences: tuple[str, ...] = ()
    trust_state: FreshnessStatus = "recorded"


class ProductPromotionLiveConfirmations(BaseModel):
    model_config = ConfigDict(extra="forbid")

    patch: str
    minor: str
    major: str


class ProductPromotionStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    product: str
    display_name: str
    driver_id: str
    base_driver_id: str = ""
    repository: str
    workflow_id: str
    workflow_ref: str
    context: str
    source_environment: str = "testing"
    destination_environment: str = "prod"
    source: ProductPromotionEvidence
    destination: ProductPromotionEvidence
    manager_preview_approval: ManagerPreviewApprovalDecision | None = Field(
        default=None,
        json_schema_extra={"x-launchplane-optional-response": True},
    )
    evidence_fingerprint: str
    default_bump: BumpLevel
    bump_options: tuple[BumpLevel, ...] = ("patch", "minor", "major")
    direct_dry_run: ProductPromotionOperationAvailability
    workflow_dry_run: ProductPromotionOperationAvailability
    workflow_live: ProductPromotionOperationAvailability
    live_confirmations: ProductPromotionLiveConfirmations
    trust_state: FreshnessStatus


class ProductPromotionDryRunEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    reason: str
    evidence_fingerprint: str
    bump: BumpLevel = "patch"

    @field_validator("bump", mode="before")
    @classmethod
    def _normalize_bump(cls, value: object) -> BumpLevel:
        normalized = str(value).strip().lower()
        if normalized not in {"patch", "minor", "major"}:
            raise ValueError("Product promotion bump must be patch, minor, or major.")
        return cast(BumpLevel, normalized)

    @model_validator(mode="after")
    def _validate_request(self) -> "ProductPromotionDryRunEnvelope":
        self.reason = self.reason.strip()
        self.evidence_fingerprint = self.evidence_fingerprint.strip()
        if not self.reason:
            raise ValueError("Product promotion dry-run requires a reason.")
        if not self.evidence_fingerprint:
            raise ValueError("Product promotion dry-run requires an evidence fingerprint.")
        return self


class ProductPromotionWorkflowDispatchEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    dry_run: bool = True
    reason: str
    evidence_fingerprint: str
    bump: BumpLevel = "patch"
    confirmation: str = ""

    @field_validator("bump", mode="before")
    @classmethod
    def _normalize_bump(cls, value: object) -> BumpLevel:
        normalized = str(value).strip().lower()
        if normalized not in {"patch", "minor", "major"}:
            raise ValueError("Product promotion bump must be patch, minor, or major.")
        return cast(BumpLevel, normalized)

    @model_validator(mode="after")
    def _validate_request(self) -> "ProductPromotionWorkflowDispatchEnvelope":
        self.reason = self.reason.strip()
        self.evidence_fingerprint = self.evidence_fingerprint.strip()
        self.confirmation = self.confirmation.strip()
        if not self.reason:
            raise ValueError("Product promotion workflow dispatch requires a reason.")
        if not self.evidence_fingerprint:
            raise ValueError(
                "Product promotion workflow dispatch requires an evidence fingerprint."
            )
        return self


class ProductPromotionDryRunResult(GenericWebProdPromotionResponseResult):
    model_config = ConfigDict(extra="forbid")

    evidence_fingerprint: str
    bump: BumpLevel


class ProductPromotionDryRunResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["accepted"] = "accepted"
    trace_id: str
    records: GenericWebProdPromotionRecords
    result: ProductPromotionDryRunResult
    replayed: bool | None = Field(
        default=None,
        json_schema_extra={"x-launchplane-optional-response": True},
    )
    original_trace_id: str | None = Field(
        default=None,
        json_schema_extra={"x-launchplane-optional-response": True},
    )


class ProductPromotionWorkflowDispatchResult(GenericWebPromotionWorkflowResponseResult):
    model_config = ConfigDict(extra="forbid")

    evidence_fingerprint: str
    delivery_id: str
    artifact_id: str
    source_git_ref: str


class ProductPromotionWorkflowDispatchResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["accepted"] = "accepted"
    trace_id: str
    records: dict[str, str] = Field(default_factory=dict)
    result: ProductPromotionWorkflowDispatchResult
    replayed: bool | None = Field(
        default=None,
        json_schema_extra={"x-launchplane-optional-response": True},
    )
    original_trace_id: str | None = Field(
        default=None,
        json_schema_extra={"x-launchplane-optional-response": True},
    )


class ProductPromotionWorkflowDeliveryStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    delivery_id: str
    state: Literal["pending", "running", "delivered", "failed", "reconcile_required"]
    dispatch_status: Literal["pending", "dispatched", "failed", "reconcile_required"]
    run_id: int = Field(default=0, ge=0)
    run_url: str = ""
    run_status: str = "pending"
    run_conclusion: str = ""
    run_observation_status: Literal["pending", "observed"] = "pending"
    error_code: str = ""
    observed_at: str


class ProductPromotionWorkflowDeliveryStatusResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    delivery: ProductPromotionWorkflowDeliveryStatus


def build_product_promotion_status(
    *,
    record_store: object,
    product: str,
    destination_environment: str,
    action_allowed: Callable[[str, str, str, tuple[str, ...]], bool],
    workflow_credentials_ready: Callable[[str], bool],
    now: datetime | None = None,
) -> tuple[LaunchplaneProductProfileRecord, ProductLaneProfile, ProductPromotionStatus]:
    profile, destination_lane = resolve_product_promotion_target(
        record_store=record_store,
        product=product,
        destination_environment=destination_environment,
    )
    source_lane = _find_lane(profile, "testing")
    source_summary = _read_lane_summary(record_store, source_lane)
    destination_summary = _read_lane_summary(record_store, destination_lane)
    observed_at = now or datetime.now(UTC)
    source_evidence = _promotion_evidence(
        profile=profile,
        lane=source_lane,
        summary=source_summary,
        now=observed_at,
    )
    destination_evidence = _promotion_evidence(
        profile=profile,
        lane=destination_lane,
        summary=destination_summary,
        now=observed_at,
    )
    manager_preview_approval = current_product_promotion_manager_preview_approval(
        record_store=record_store,
        profile=profile,
        source=source_evidence,
        evaluated_at=observed_at.isoformat().replace("+00:00", "Z"),
    )
    common_blockers = _common_promotion_blockers(
        record_store=record_store,
        profile=profile,
        source_lane=source_lane,
        destination_lane=destination_lane,
        source_summary=source_summary,
        destination_summary=destination_summary,
        source=source_evidence,
        destination=destination_evidence,
    )
    workflow_blockers = list(common_blockers)
    if not _repository_is_owner_repo(profile.repository):
        workflow_blockers.append("Product repository is not configured as owner/repo.")
    if not workflow_credentials_ready(destination_lane.context):
        workflow_blockers.append("Managed GitHub workflow credentials are unavailable.")
    direct_blockers = list(common_blockers)
    if not action_allowed(
        "generic_web_prod_promotion.execute",
        profile.product,
        destination_lane.context,
        (source_lane.instance, destination_lane.instance),
    ):
        direct_blockers.append("Caller is not authorized to dry-run generic-web promotion.")
    if not action_allowed(
        "generic_web_prod_promotion.dispatch",
        profile.product,
        destination_lane.context,
        (source_lane.instance, destination_lane.instance),
    ):
        workflow_blockers.append("Caller is not authorized to dispatch the promotion workflow.")
    workflow_live_blockers = list(workflow_blockers)
    if manager_preview_approval is not None and manager_preview_approval.status != "approved":
        workflow_live_blockers.append(
            "Manager preview approval is not valid for the exact testing artifact: "
            f"{manager_preview_approval.reason}"
        )
    trust_state = _promotion_trust_state(
        source_evidence,
        destination_evidence,
        common_blockers,
    )
    evidence_fingerprint = _promotion_evidence_fingerprint(
        profile=profile,
        source=source_evidence,
        destination=destination_evidence,
        destination_summary=destination_summary,
        manager_preview_approval=manager_preview_approval,
    )
    return (
        profile,
        destination_lane,
        ProductPromotionStatus(
            product=profile.product,
            display_name=profile.display_name,
            driver_id=profile.driver_id,
            base_driver_id="generic-web" if product_profile_uses_generic_web_base(profile) else "",
            repository=profile.repository,
            workflow_id=profile.promotion_workflow.workflow_id,
            workflow_ref=profile.promotion_workflow.ref,
            context=destination_lane.context,
            source_environment=source_lane.instance,
            destination_environment=destination_lane.instance,
            source=source_evidence,
            destination=destination_evidence,
            manager_preview_approval=manager_preview_approval,
            evidence_fingerprint=evidence_fingerprint,
            default_bump=cast(BumpLevel, profile.promotion_workflow.default_bump.strip()),
            direct_dry_run=_operation_availability(
                operation="direct_dry_run",
                authz_action="generic_web_prod_promotion.execute",
                blockers=direct_blockers,
                trust_state=trust_state,
                consequences=(
                    "This operation validates current evidence and writes no promotion records.",
                    "No GitHub release or production deployment is created.",
                ),
            ),
            workflow_dry_run=_operation_availability(
                operation="workflow_dry_run",
                authz_action="generic_web_prod_promotion.dispatch",
                blockers=workflow_blockers,
                trust_state=trust_state,
                requires_matching_direct_dry_run=True,
                consequences=(
                    "The product-owned GitHub workflow is queued with dry_run=true.",
                    "Dispatch acceptance does not mean the GitHub run completed.",
                ),
            ),
            workflow_live=_operation_availability(
                operation="workflow_live",
                authz_action="generic_web_prod_promotion.dispatch",
                blockers=workflow_live_blockers,
                trust_state=trust_state,
                requires_matching_direct_dry_run=True,
                requires_confirmation=True,
                consequences=(
                    "The product workflow may create a release and tag.",
                    "The product workflow may deploy the reviewed artifact to production.",
                    "Launchplane reports dispatch and observed run state, not workflow completion.",
                ),
            ),
            live_confirmations=ProductPromotionLiveConfirmations(
                patch=_product_promotion_confirmation(
                    product=profile.product,
                    artifact_id=source_evidence.artifact_id,
                    source_git_ref=source_evidence.source_git_ref,
                    destination_environment=destination_lane.instance,
                    bump="patch",
                ),
                minor=_product_promotion_confirmation(
                    product=profile.product,
                    artifact_id=source_evidence.artifact_id,
                    source_git_ref=source_evidence.source_git_ref,
                    destination_environment=destination_lane.instance,
                    bump="minor",
                ),
                major=_product_promotion_confirmation(
                    product=profile.product,
                    artifact_id=source_evidence.artifact_id,
                    source_git_ref=source_evidence.source_git_ref,
                    destination_environment=destination_lane.instance,
                    bump="major",
                ),
            ),
            trust_state=trust_state,
        ),
    )


def resolve_product_promotion_target(
    *,
    record_store: object,
    product: str,
    destination_environment: str,
) -> tuple[LaunchplaneProductProfileRecord, ProductLaneProfile]:
    profile = cast(ProductPromotionStatusStore, record_store).read_product_profile_record(
        product.strip()
    )
    destination_lane = _find_lane(profile, destination_environment.strip())
    if destination_lane.instance != "prod":
        raise FileNotFoundError("Product promotion is available only for the prod environment.")
    return profile, destination_lane


def product_promotion_direct_request(
    *,
    profile: LaunchplaneProductProfileRecord,
    status: ProductPromotionStatus,
) -> GenericWebProdPromotionEnvelope:
    return GenericWebProdPromotionEnvelope(
        product=profile.product,
        promotion=GenericWebProdPromotionRequest(
            product=profile.product,
            artifact_id=status.source.artifact_id,
            deploy_reference=status.source.deploy_reference,
            source_git_ref=status.source.source_git_ref,
            from_instance=status.source_environment,
            to_instance=status.destination_environment,
            dry_run=True,
        ),
    )


def product_promotion_workflow_request(
    *,
    profile: LaunchplaneProductProfileRecord,
    status: ProductPromotionStatus,
    request: ProductPromotionWorkflowDispatchEnvelope,
) -> GenericWebPromotionWorkflowEnvelope:
    return GenericWebPromotionWorkflowEnvelope(
        product=profile.product,
        workflow=GenericWebPromotionWorkflowRequest(
            product=profile.product,
            context=status.context,
            dry_run=request.dry_run,
            bump=request.bump,
            artifact_id=status.source.artifact_id,
            deploy_reference=status.source.deploy_reference,
            source_git_ref=status.source.source_git_ref,
            evidence_fingerprint=status.evidence_fingerprint,
        ),
    )


def product_promotion_confirmation(
    *,
    status: ProductPromotionStatus,
    bump: BumpLevel,
) -> str:
    return _product_promotion_confirmation(
        product=status.product,
        artifact_id=status.source.artifact_id,
        source_git_ref=status.source.source_git_ref,
        destination_environment=status.destination_environment,
        bump=bump,
    )


def product_promotion_intent_matches(
    *,
    record: OutboxDeliveryRecord,
    profile: LaunchplaneProductProfileRecord,
    status: ProductPromotionStatus,
    request: GenericWebProdPromotionRequest,
) -> bool:
    intent_id = request.promotion_intent_id.strip()
    if not intent_id or record.delivery_id != intent_id:
        return False
    if not record.provider_operation_key or record.provider_id != "github":
        return False
    if record.kind != "github_workflow_dispatch" or record.state == "failed":
        return False
    if record.aggregate_type != "generic_web_promotion_workflow":
        return False
    if record.aggregate_id != f"{profile.product}:{status.context}":
        return False
    payload = record.payload
    if payload.get("repository") != profile.repository:
        return False
    if payload.get("workflow_id") != profile.promotion_workflow.workflow_id.strip():
        return False
    if payload.get("ref") != profile.promotion_workflow.ref.strip():
        return False
    if payload.get("credential_context") != status.context:
        return False
    if payload.get("promotion_evidence_fingerprint") != status.evidence_fingerprint:
        return False
    raw_inputs = payload.get("inputs")
    if not isinstance(raw_inputs, dict):
        return False
    inputs = {str(key): str(value) for key, value in raw_inputs.items()}
    workflow = profile.promotion_workflow
    return (
        inputs.get(workflow.promotion_intent_input.strip()) == intent_id
        and inputs.get(workflow.dry_run_input.strip()) == "false"
        and inputs.get(workflow.artifact_id_input.strip()) == status.source.artifact_id
        and inputs.get(workflow.deploy_reference_input.strip(), "")
        == status.source.deploy_reference
        and inputs.get(workflow.source_git_ref_input.strip()) == status.source.source_git_ref
        and request.product == profile.product
        and request.artifact_id == status.source.artifact_id
        and request.deploy_reference == status.source.deploy_reference
        and request.source_git_ref == status.source.source_git_ref
        and request.from_instance == status.source_environment
        and request.to_instance == status.destination_environment
        and not request.dry_run
    )


def product_promotion_continuity_payload(
    *,
    status: ProductPromotionStatus,
    bump: BumpLevel,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "product": status.product,
        "context": status.context,
        "source_environment": status.source_environment,
        "destination_environment": status.destination_environment,
        "evidence_fingerprint": status.evidence_fingerprint,
        "bump": bump,
    }


def product_promotion_dry_run_key(*, status: ProductPromotionStatus, bump: BumpLevel) -> str:
    fingerprint = _canonical_sha256(product_promotion_continuity_payload(status=status, bump=bump))
    return f"product-promotion-dry-run:{fingerprint}"


def product_promotion_request_payload(
    *,
    product: str,
    destination_environment: str,
    request: ProductPromotionDryRunEnvelope | ProductPromotionWorkflowDispatchEnvelope,
) -> dict[str, object]:
    return {
        "product": product.strip(),
        "destination_environment": destination_environment.strip(),
        **request.model_dump(mode="json"),
    }


def product_promotion_delivery_status(
    record: OutboxDeliveryRecord,
) -> ProductPromotionWorkflowDeliveryStatus:
    run_status = str(record.payload.get("run_status") or "pending").strip() or "pending"
    run_conclusion = str(record.payload.get("run_conclusion") or "").strip()
    if record.state == "delivered":
        dispatch_status: Literal["pending", "dispatched", "failed", "reconcile_required"] = (
            "dispatched"
        )
    elif record.state == "failed":
        dispatch_status = "failed"
    elif record.state == "reconcile_required":
        dispatch_status = "reconcile_required"
    else:
        dispatch_status = "pending"
    return ProductPromotionWorkflowDeliveryStatus(
        delivery_id=record.delivery_id,
        state=record.state,
        dispatch_status=dispatch_status,
        run_id=int(record.external_id) if record.external_id.isdigit() else 0,
        run_url=record.external_url,
        run_status=run_status,
        run_conclusion=run_conclusion,
        run_observation_status=("observed" if record.external_id else "pending"),
        error_code=record.error_code,
        observed_at=record.updated_at,
    )


def _find_lane(
    profile: LaunchplaneProductProfileRecord,
    environment: str,
) -> ProductLaneProfile:
    lane = next(
        (candidate for candidate in profile.lanes if candidate.instance == environment), None
    )
    if lane is None:
        raise FileNotFoundError(f"Product {profile.product!r} has no environment {environment!r}.")
    return lane


def _read_lane_summary(
    record_store: object, lane: ProductLaneProfile
) -> LaunchplaneLaneSummary | None:
    try:
        return cast(ProductPromotionStatusStore, record_store).read_lane_summary(
            context_name=lane.context,
            instance_name=lane.instance,
        )
    except (AttributeError, FileNotFoundError):
        return None


def _promotion_evidence(
    *,
    profile: LaunchplaneProductProfileRecord,
    lane: ProductLaneProfile,
    summary: LaunchplaneLaneSummary | None,
    now: datetime,
) -> ProductPromotionEvidence:
    inventory = summary.inventory if summary is not None else None
    if inventory is None:
        return ProductPromotionEvidence(environment=lane.instance)
    runtime_identity = inventory.runtime_identity
    artifact_id = _runtime_artifact_id(profile, inventory)
    deploy_reference = _runtime_deploy_reference(
        inventory=inventory,
        artifact_id=artifact_id,
    )
    source_git_ref = runtime_identity.source_git_ref.strip() if runtime_identity is not None else ""
    try:
        inventory_updated_at = _parse_timestamp(inventory.updated_at)
        if inventory_updated_at > now + PRODUCT_PROMOTION_INVENTORY_CLOCK_SKEW:
            raise ValueError("inventory timestamp is too far in the future")
        stale_after = inventory_updated_at + PRODUCT_PROMOTION_INVENTORY_STALE_AFTER
    except ValueError:
        return ProductPromotionEvidence(
            environment=lane.instance,
            artifact_id=artifact_id,
            deploy_reference=deploy_reference,
            source_git_ref=source_git_ref,
            deployment_record_id=(
                runtime_identity.deployment_record_id if runtime_identity is not None else ""
            ),
            inventory_updated_at=inventory.updated_at,
            deployment_status=inventory.deploy.status,
            health_status=inventory.destination_health.status,
            runtime_identity_status=inventory.destination_health.runtime_identity_status,
            runtime_identity_detail="Inventory timestamp is invalid.",
        )
    trust_state: FreshnessStatus = "stale" if now >= stale_after else "recorded"
    runtime_identity_status = inventory.destination_health.runtime_identity_status
    if (
        inventory.deploy.status == "pass"
        and inventory.destination_health.verified
        and inventory.destination_health.status == "pass"
        and runtime_identity_status == "match"
        and _runtime_identity_matches_inventory(profile=profile, lane=lane, inventory=inventory)
        and _health_identity_matches_inventory(profile=profile, lane=lane, inventory=inventory)
    ):
        trust_state = "verified" if now < stale_after else "stale"
    return ProductPromotionEvidence(
        environment=lane.instance,
        artifact_id=artifact_id,
        deploy_reference=deploy_reference,
        source_git_ref=source_git_ref,
        deployment_record_id=(
            runtime_identity.deployment_record_id if runtime_identity is not None else ""
        ),
        inventory_updated_at=inventory.updated_at,
        inventory_stale_after=stale_after.isoformat().replace("+00:00", "Z"),
        deployment_status=inventory.deploy.status,
        health_status=inventory.destination_health.status,
        runtime_identity_status=runtime_identity_status,
        runtime_identity_detail=inventory.destination_health.runtime_identity_detail,
        trust_state=trust_state,
    )


def _runtime_artifact_id(
    profile: LaunchplaneProductProfileRecord,
    inventory: EnvironmentInventory,
) -> str:
    if inventory.runtime_identity is None:
        return ""
    try:
        artifact_id = normalize_generic_web_artifact_id(
            profile=profile,
            artifact_id=inventory.runtime_identity.artifact_id,
        )
        if not _immutable_artifact_id(artifact_id):
            return ""
        return artifact_id
    except (ValueError, click.ClickException):
        return ""


def _runtime_deploy_reference(*, inventory: EnvironmentInventory, artifact_id: str) -> str:
    if inventory.runtime_identity is None:
        return ""
    image_reference = inventory.runtime_identity.image_reference.strip()
    if not image_reference or image_reference == inventory.runtime_identity.artifact_id.strip():
        return ""
    try:
        return validate_provider_deploy_reference(
            artifact_id=artifact_id,
            deploy_reference=image_reference,
            label="Product promotion source evidence",
        )
    except ValueError:
        return ""


def _common_promotion_blockers(
    *,
    record_store: object,
    profile: LaunchplaneProductProfileRecord,
    source_lane: ProductLaneProfile,
    destination_lane: ProductLaneProfile,
    source_summary: LaunchplaneLaneSummary | None,
    destination_summary: LaunchplaneLaneSummary | None,
    source: ProductPromotionEvidence,
    destination: ProductPromotionEvidence,
) -> list[str]:
    blockers: list[str] = []
    if not isinstance(record_store, PostgresRecordStore):
        blockers.append("DB-backed promotion and idempotency storage is unavailable.")
    if not product_profile_uses_generic_web_base(profile):
        blockers.append("Product driver does not support generic-web promotion.")
    if source_lane.context != destination_lane.context:
        blockers.append("Testing and production lanes do not share one promotion context.")
    blockers.extend(
        _environment_evidence_blockers(
            label="Testing",
            profile=profile,
            lane=source_lane,
            summary=source_summary,
            evidence=source,
        )
    )
    blockers.extend(
        _environment_evidence_blockers(
            label="Production",
            profile=profile,
            lane=destination_lane,
            summary=destination_summary,
            evidence=destination,
        )
    )
    if not _destination_target_ready(destination_summary, destination_lane):
        blockers.append("Production provider target authority is unavailable.")
    return blockers


def _environment_evidence_blockers(
    *,
    label: str,
    profile: LaunchplaneProductProfileRecord,
    lane: ProductLaneProfile,
    summary: LaunchplaneLaneSummary | None,
    evidence: ProductPromotionEvidence,
) -> list[str]:
    blockers: list[str] = []
    if summary is None or summary.inventory is None:
        return [f"Current {label.lower()} inventory is unavailable."]
    inventory = summary.inventory
    if inventory.runtime_identity is None:
        blockers.append(f"{label} inventory does not contain generated runtime identity evidence.")
    elif not _runtime_identity_matches_inventory(
        profile=profile,
        lane=lane,
        inventory=inventory,
    ):
        blockers.append(f"{label} runtime identity does not match current inventory evidence.")
    if not evidence.artifact_id:
        blockers.append(f"{label} runtime identity does not contain a valid immutable artifact.")
    if not _immutable_source_git_ref(evidence.source_git_ref):
        blockers.append(f"{label} runtime identity does not contain an immutable source commit.")
    if evidence.inventory_updated_at and not evidence.inventory_stale_after:
        blockers.append(f"{label} inventory timestamp is invalid.")
    if evidence.deployment_status != "pass":
        blockers.append(f"{label} deployment evidence is not passing.")
    if not inventory.destination_health.verified:
        blockers.append(f"{label} health evidence is not verified.")
    if evidence.health_status != "pass":
        blockers.append(f"{label} health evidence is not passing.")
    if evidence.runtime_identity_status != "match":
        blockers.append(f"{label} health did not verify the generated runtime identity.")
    if not _health_identity_matches_inventory(
        profile=profile,
        lane=lane,
        inventory=inventory,
    ):
        blockers.append(f"{label} observed health identity does not match current inventory.")
    if evidence.trust_state == "stale":
        blockers.append(f"{label} inventory evidence is stale.")
    return blockers


def _destination_target_ready(
    summary: LaunchplaneLaneSummary | None,
    lane: ProductLaneProfile,
) -> bool:
    if summary is None or summary.provider_target is None:
        return False
    return (
        summary.provider_target.context == lane.context
        and summary.provider_target.instance == lane.instance
    )


def _operation_availability(
    *,
    operation: ProductPromotionOperationKind,
    authz_action: str,
    blockers: list[str],
    trust_state: FreshnessStatus,
    consequences: tuple[str, ...],
    requires_matching_direct_dry_run: bool = False,
    requires_confirmation: bool = False,
) -> ProductPromotionOperationAvailability:
    unique_blockers = tuple(
        dict.fromkeys(blocker.strip() for blocker in blockers if blocker.strip())
    )
    return ProductPromotionOperationAvailability(
        operation=operation,
        authz_action=authz_action,
        enabled=not unique_blockers,
        disabled_reasons=unique_blockers,
        requires_matching_direct_dry_run=requires_matching_direct_dry_run,
        requires_confirmation=requires_confirmation,
        consequences=consequences,
        trust_state=trust_state,
    )


def _promotion_trust_state(
    source: ProductPromotionEvidence,
    destination: ProductPromotionEvidence,
    blockers: list[str],
) -> FreshnessStatus:
    if "stale" in {source.trust_state, destination.trust_state}:
        return "stale"
    if (
        not source.artifact_id
        or not source.source_git_ref
        or not destination.artifact_id
        or not destination.source_git_ref
    ):
        return "missing"
    if blockers:
        return "recorded"
    if source.trust_state == destination.trust_state == "verified":
        return "verified"
    return "recorded"


def _promotion_evidence_fingerprint(
    *,
    profile: LaunchplaneProductProfileRecord,
    source: ProductPromotionEvidence,
    destination: ProductPromotionEvidence,
    destination_summary: LaunchplaneLaneSummary | None,
    manager_preview_approval: ManagerPreviewApprovalDecision | None,
) -> str:
    return _canonical_sha256(
        {
            "schema_version": 1,
            "product": profile.product,
            "profile_updated_at": profile.updated_at,
            "repository": profile.repository,
            "workflow": profile.promotion_workflow.model_dump(mode="json"),
            "source": source.model_dump(mode="json"),
            "destination": destination.model_dump(mode="json"),
            "manager_preview_approval": (
                manager_preview_approval.model_dump(
                    mode="json",
                    exclude={"evaluated_at"},
                )
                if manager_preview_approval is not None
                else None
            ),
            "destination_provider_target": (
                destination_summary.provider_target.model_dump(mode="json")
                if destination_summary is not None
                and destination_summary.provider_target is not None
                else None
            ),
        }
    )


def current_product_promotion_manager_preview_approval(
    *,
    record_store: object,
    profile: LaunchplaneProductProfileRecord,
    source: ProductPromotionEvidence,
    evaluated_at: str,
) -> ManagerPreviewApprovalDecision | None:
    list_policy_records = getattr(record_store, "list_authz_policy_records", None)
    if not callable(list_policy_records):
        return None
    policy_records = tuple(list_policy_records(status="active", limit=2))
    if not policy_records:
        return None
    if len(policy_records) != 1:
        return _manager_preview_approval_unavailable(
            evaluated_at=evaluated_at,
            reason="Launchplane could not resolve exactly one active manager authorization policy.",
        )
    policy_record = policy_records[0]
    if not isinstance(policy_record, LaunchplaneAuthzPolicyRecord):
        policy_record = LaunchplaneAuthzPolicyRecord.model_validate(policy_record)
    preview_context = profile.preview.context.strip()
    if not manager_preview_approval_required(
        policy_record=policy_record,
        product=profile.product,
        context=preview_context,
    ):
        return None
    if not source.artifact_id or not source.source_git_ref:
        return _manager_preview_approval_unavailable(
            evaluated_at=evaluated_at,
            reason="Testing artifact and source identity are unavailable for manager approval.",
        )
    list_previews = getattr(record_store, "list_preview_records", None)
    read_generation = getattr(record_store, "read_preview_generation_record", None)
    list_events = getattr(record_store, "list_manager_preview_approval_event_records", None)
    if not callable(list_previews) or not callable(read_generation) or not callable(list_events):
        return _manager_preview_approval_unavailable(
            evaluated_at=evaluated_at,
            reason="Manager preview approval evidence storage is unavailable.",
        )
    owner, separator, anchor_repo = profile.repository.strip().partition("/")
    if not separator or not owner or not anchor_repo:
        return _manager_preview_approval_unavailable(
            evaluated_at=evaluated_at,
            reason="The product repository is not configured as owner/repo.",
        )
    source_digest = _immutable_artifact_digest(source.artifact_id)
    candidates: list[tuple[PreviewRecord, PreviewGenerationRecord]] = []
    for raw_preview in list_previews(
        context_name=preview_context,
        anchor_repo=anchor_repo,
        limit=200,
    ):
        preview = (
            raw_preview
            if isinstance(raw_preview, PreviewRecord)
            else PreviewRecord.model_validate(raw_preview)
        )
        if preview.state != "active" or not preview.serving_generation_id.strip():
            continue
        try:
            raw_generation = read_generation(preview.serving_generation_id)
            generation = (
                raw_generation
                if isinstance(raw_generation, PreviewGenerationRecord)
                else PreviewGenerationRecord.model_validate(raw_generation)
            )
            binding = build_current_manager_preview_approval_binding(
                product=profile.product,
                preview=preview,
                generation=generation,
            )
        except (
            FileNotFoundError,
            LookupError,
            ValueError,
        ):
            continue
        if (
            binding.head_sha == source.source_git_ref.strip().lower()
            and source_digest
            and binding.artifact_image_digest == source_digest
        ):
            candidates.append((preview, generation))
    if len(candidates) != 1:
        reason = (
            "No active serving preview matches the exact testing artifact and source."
            if not candidates
            else "Multiple active serving previews match the exact testing artifact and source."
        )
        return _manager_preview_approval_unavailable(
            evaluated_at=evaluated_at,
            reason=reason,
        )
    preview, generation = candidates[0]
    raw_events = list_events(
        product=profile.product,
        context=preview_context,
        repository=preview.anchor_repo,
        pr_number=preview.anchor_pr_number,
        limit=200,
    )
    return evaluate_manager_preview_approval(
        product=profile.product,
        preview=preview,
        generation=generation,
        policy_record=policy_record,
        events=tuple(raw_events),
        evaluated_at=evaluated_at,
    )


def _manager_preview_approval_unavailable(
    *,
    evaluated_at: str,
    reason: str,
) -> ManagerPreviewApprovalDecision:
    return ManagerPreviewApprovalDecision(
        status="unavailable",
        reason_code="preview_inactive",
        reason=reason,
        evaluated_at=evaluated_at,
    )


def _immutable_artifact_digest(value: str) -> str:
    normalized = value.strip().lower()
    if re.fullmatch(r"sha256:[0-9a-f]{64}", normalized):
        return normalized
    _repository, separator, digest = normalized.partition("@sha256:")
    if separator and re.fullmatch(r"[0-9a-f]{64}", digest):
        return f"sha256:{digest}"
    return ""


def _canonical_sha256(payload: dict[str, object]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _parse_timestamp(value: str) -> datetime:
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _repository_is_owner_repo(value: str) -> bool:
    owner, separator, repo = value.strip().partition("/")
    return bool(separator and owner.strip() and repo.strip() and "/" not in repo)


def _immutable_artifact_id(value: str) -> bool:
    repository, separator, digest = value.strip().partition("@sha256:")
    return bool(
        separator and repository.strip() and re.fullmatch(r"[0-9a-fA-F]{64}", digest.strip())
    )


def _immutable_source_git_ref(value: str) -> bool:
    return re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", value.strip()) is not None


def _runtime_identity_matches_inventory(
    *,
    profile: LaunchplaneProductProfileRecord,
    lane: ProductLaneProfile,
    inventory: EnvironmentInventory,
) -> bool:
    identity = inventory.runtime_identity
    if identity is None:
        return False
    return _runtime_identity_matches_expected(
        profile=profile,
        lane=lane,
        inventory=inventory,
        identity=identity,
    )


def _health_identity_matches_inventory(
    *,
    profile: LaunchplaneProductProfileRecord,
    lane: ProductLaneProfile,
    inventory: EnvironmentInventory,
) -> bool:
    health = inventory.destination_health
    identity = health.observed_runtime_identity
    if not health.verified or identity is None:
        return False
    return _runtime_identity_matches_expected(
        profile=profile,
        lane=lane,
        inventory=inventory,
        identity=identity,
    )


def _runtime_identity_matches_expected(
    *,
    profile: LaunchplaneProductProfileRecord,
    lane: ProductLaneProfile,
    inventory: EnvironmentInventory,
    identity: RuntimeIdentity,
) -> bool:
    if identity.product.strip() != profile.product:
        return False
    if identity.context.strip() != lane.context:
        return False
    if identity.instance.strip() != lane.instance:
        return False
    if identity.environment_kind.strip() != "stable":
        return False
    if identity.deployment_record_id.strip() != inventory.deployment_record_id:
        return False
    if identity.source_git_ref.strip() != inventory.source_git_ref.strip():
        return False
    if inventory.artifact_identity is None:
        return False
    try:
        runtime_artifact = normalize_generic_web_artifact_id(
            profile=profile,
            artifact_id=identity.artifact_id,
        )
        inventory_artifact = normalize_generic_web_artifact_id(
            profile=profile,
            artifact_id=inventory.artifact_identity.artifact_id,
        )
    except (ValueError, click.ClickException):
        return False
    return runtime_artifact == inventory_artifact


def _product_promotion_confirmation(
    *,
    product: str,
    artifact_id: str,
    source_git_ref: str,
    destination_environment: str,
    bump: BumpLevel,
) -> str:
    return " ".join(
        (
            "PROMOTE",
            product,
            artifact_id,
            source_git_ref,
            "TO",
            destination_environment,
            "BUMP",
            bump,
            "CREATE",
            "RELEASE",
            "TAG",
            "AND",
            "DEPLOY",
            "PRODUCTION",
        )
    )
