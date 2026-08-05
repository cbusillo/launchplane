from __future__ import annotations

from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.backup_gate_record import BackupGateRecord
from control_plane.contracts.deployment_record import DeploymentRecord
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductLaneProfile,
)
from control_plane.contracts.promotion_record import (
    ArtifactIdentityReference,
    BackupGateEvidence,
    HealthcheckEvidence,
)
from control_plane.drivers.registry import read_driver_descriptor
from control_plane.contracts.deploy_reference import is_non_floating_tag_reference
from control_plane.workflows.ship import utc_now_timestamp

GenericWebRollbackPlanStatus = Literal["ready", "blocked"]
GenericWebRollbackBlockerCode = Literal[
    "backup_gate_missing",
    "backup_gate_not_passed",
    "backup_gate_scope_mismatch",
    "deployment_scope_mismatch",
    "health_evidence_failed",
    "missing_deploy_reference",
    "missing_rollback_target",
    "mutable_artifact_reference",
    "target_deploy_not_passed",
]


class GenericWebRollbackPlanReader(Protocol):
    def read_product_profile_record(self, product: str) -> LaunchplaneProductProfileRecord: ...

    def read_deployment_record(self, record_id: str) -> DeploymentRecord: ...

    def read_backup_gate_record(self, record_id: str) -> BackupGateRecord: ...


class GenericWebRollbackPlanStore(GenericWebRollbackPlanReader, Protocol):
    def write_generic_web_rollback_plan_record(
        self, record: "GenericWebRollbackPlanRecord"
    ) -> object: ...


class GenericWebRollbackPlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    product: str
    instance: str = "prod"
    rollback_deployment_record_id: str
    backup_record_id: str = ""
    backup_required: bool = False
    verify_health: bool = True
    timeout_seconds: int | None = Field(default=None, ge=1)
    no_cache: bool = False

    @model_validator(mode="after")
    def _validate_request(self) -> "GenericWebRollbackPlanRequest":
        self.product = self.product.strip()
        self.instance = self.instance.strip().lower()
        self.rollback_deployment_record_id = self.rollback_deployment_record_id.strip()
        self.backup_record_id = self.backup_record_id.strip()
        if not self.product:
            raise ValueError("generic web rollback plan requires product")
        if not self.instance:
            raise ValueError("generic web rollback plan requires instance")
        if not self.rollback_deployment_record_id:
            raise ValueError("generic web rollback plan requires rollback_deployment_record_id")
        if self.backup_required and not self.backup_record_id:
            raise ValueError(
                "generic web rollback plan requires backup_record_id when backup_required=true"
            )
        return self


class GenericWebRollbackBlocker(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: GenericWebRollbackBlockerCode
    message: str

    @model_validator(mode="after")
    def _validate_blocker(self) -> "GenericWebRollbackBlocker":
        self.message = self.message.strip()
        if not self.message:
            raise ValueError("generic web rollback blocker requires message")
        return self


class GenericWebRollbackDeployPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product: str
    instance: str
    artifact_id: str
    deploy_reference: str = ""
    source_git_ref: str
    timeout_seconds: int | None = Field(default=None, ge=1)
    no_cache: bool = False

    @model_validator(mode="after")
    def _validate_plan(self) -> "GenericWebRollbackDeployPlan":
        self.product = self.product.strip()
        self.instance = self.instance.strip()
        self.artifact_id = self.artifact_id.strip()
        self.deploy_reference = self.deploy_reference.strip()
        self.source_git_ref = self.source_git_ref.strip()
        if not self.product:
            raise ValueError("generic web rollback deploy plan requires product")
        if not self.instance:
            raise ValueError("generic web rollback deploy plan requires instance")
        if not self.artifact_id:
            raise ValueError("generic web rollback deploy plan requires artifact_id")
        if not self.source_git_ref:
            raise ValueError("generic web rollback deploy plan requires source_git_ref")
        return self


class GenericWebRollbackPlanRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    plan_id: str
    product: str
    context: str
    instance: str
    status: GenericWebRollbackPlanStatus
    rollback_deployment_record_id: str
    artifact_identity: ArtifactIdentityReference | None = None
    source_git_ref: str = ""
    planned_deploy: GenericWebRollbackDeployPlan | None = None
    backup_record_id: str = ""
    backup_gate: BackupGateEvidence = Field(default_factory=BackupGateEvidence)
    target_health: HealthcheckEvidence = Field(default_factory=HealthcheckEvidence)
    blockers: tuple[GenericWebRollbackBlocker, ...] = ()
    created_at: str
    summary: str

    @model_validator(mode="after")
    def _validate_record(self) -> "GenericWebRollbackPlanRecord":
        self.plan_id = _required_text(
            self.plan_id, "generic web rollback plan record requires plan_id"
        )
        self.product = _required_text(
            self.product, "generic web rollback plan record requires product"
        )
        self.context = _required_text(
            self.context, "generic web rollback plan record requires context"
        )
        self.instance = _required_text(
            self.instance, "generic web rollback plan record requires instance"
        )
        self.rollback_deployment_record_id = _required_text(
            self.rollback_deployment_record_id,
            "generic web rollback plan record requires rollback_deployment_record_id",
        )
        self.source_git_ref = self.source_git_ref.strip()
        self.backup_record_id = self.backup_record_id.strip()
        self.created_at = _required_text(
            self.created_at, "generic web rollback plan record requires created_at"
        )
        self.summary = _required_text(
            self.summary, "generic web rollback plan record requires summary"
        )
        self.blockers = tuple(sorted(self.blockers, key=lambda blocker: blocker.code))
        if self.status == "ready":
            if self.blockers:
                raise ValueError("ready generic web rollback plan cannot include blockers")
            if self.artifact_identity is None or self.planned_deploy is None:
                raise ValueError(
                    "ready generic web rollback plan requires artifact identity and deploy plan"
                )
        if self.status == "blocked" and not self.blockers:
            raise ValueError("blocked generic web rollback plan requires blockers")
        return self


def build_generic_web_rollback_plan(
    *,
    record_store: GenericWebRollbackPlanReader,
    request: GenericWebRollbackPlanRequest,
) -> GenericWebRollbackPlanRecord:
    profile, lane = _resolve_generic_web_profile_lane(
        record_store=record_store,
        product=request.product,
        instance=request.instance,
    )
    blockers: list[GenericWebRollbackBlocker] = []
    deployment_record = _read_rollback_deployment_record(
        record_store=record_store,
        request=request,
        blockers=blockers,
    )
    backup_gate = _resolve_backup_gate(
        record_store=record_store,
        request=request,
        lane=lane,
        blockers=blockers,
    )

    artifact_identity: ArtifactIdentityReference | None = None
    planned_deploy: GenericWebRollbackDeployPlan | None = None
    source_git_ref = ""
    target_health = HealthcheckEvidence()
    if deployment_record is not None:
        artifact_id = _immutable_artifact_id(profile=profile, deployment_record=deployment_record)
        if artifact_id:
            artifact_identity = ArtifactIdentityReference(artifact_id=artifact_id)
        else:
            artifact_identity = deployment_record.artifact_identity
        source_git_ref = deployment_record.source_git_ref
        target_health = deployment_record.destination_health
        _validate_rollback_target(
            profile=profile,
            lane=lane,
            deployment_record=deployment_record,
            blockers=blockers,
        )
        if artifact_identity is not None and not blockers:
            planned_deploy = GenericWebRollbackDeployPlan(
                product=profile.product,
                instance=lane.instance,
                artifact_id=artifact_identity.artifact_id,
                deploy_reference=_deploy_reference(deployment_record),
                source_git_ref=source_git_ref,
                timeout_seconds=request.timeout_seconds,
                no_cache=request.no_cache,
            )

    status: GenericWebRollbackPlanStatus = "blocked" if blockers else "ready"
    return GenericWebRollbackPlanRecord(
        plan_id=_generic_web_rollback_plan_id(
            context=lane.context,
            instance=lane.instance,
            deployment_record_id=request.rollback_deployment_record_id,
        ),
        product=profile.product,
        context=lane.context,
        instance=lane.instance,
        status=status,
        rollback_deployment_record_id=request.rollback_deployment_record_id,
        artifact_identity=artifact_identity,
        source_git_ref=source_git_ref,
        planned_deploy=planned_deploy,
        backup_record_id=request.backup_record_id,
        backup_gate=backup_gate,
        target_health=target_health,
        blockers=tuple(blockers),
        created_at=utc_now_timestamp(),
        summary=(
            "generic web rollback plan is ready"
            if status == "ready"
            else "generic web rollback plan is blocked"
        ),
    )


def execute_generic_web_rollback_plan(
    *,
    record_store: GenericWebRollbackPlanStore,
    request: GenericWebRollbackPlanRequest,
) -> GenericWebRollbackPlanRecord:
    plan = build_generic_web_rollback_plan(record_store=record_store, request=request)
    record_store.write_generic_web_rollback_plan_record(plan)
    return plan


def _read_rollback_deployment_record(
    *,
    record_store: GenericWebRollbackPlanReader,
    request: GenericWebRollbackPlanRequest,
    blockers: list[GenericWebRollbackBlocker],
) -> DeploymentRecord | None:
    try:
        return record_store.read_deployment_record(request.rollback_deployment_record_id)
    except FileNotFoundError:
        blockers.append(
            _blocker(
                "missing_rollback_target",
                "generic web rollback target deployment record was not found",
            )
        )
        return None


def _resolve_backup_gate(
    *,
    record_store: GenericWebRollbackPlanReader,
    request: GenericWebRollbackPlanRequest,
    lane: ProductLaneProfile,
    blockers: list[GenericWebRollbackBlocker],
) -> BackupGateEvidence:
    if not request.backup_record_id:
        if request.backup_required:
            blockers.append(
                _blocker(
                    "backup_gate_missing",
                    "generic web rollback requires backup_record_id when backup_required=true",
                )
            )
        return BackupGateEvidence(required=request.backup_required, status="skipped", evidence={})
    try:
        backup_record = record_store.read_backup_gate_record(request.backup_record_id)
    except FileNotFoundError:
        blockers.append(
            _blocker(
                "backup_gate_missing",
                f"generic web rollback backup gate record was not found: {request.backup_record_id}",
            )
        )
        return BackupGateEvidence(required=request.backup_required, status="fail", evidence={})
    if backup_record.context != lane.context or backup_record.instance != lane.instance:
        blockers.append(
            _blocker(
                "backup_gate_scope_mismatch",
                "generic web rollback backup gate record does not match destination lane",
            )
        )
    if backup_record.required and backup_record.status != "pass":
        blockers.append(
            _blocker(
                "backup_gate_not_passed",
                f"generic web rollback backup gate is not passing: {backup_record.record_id}",
            )
        )
    if request.backup_required and not backup_record.required:
        blockers.append(
            _blocker(
                "backup_gate_not_passed",
                f"generic web rollback backup gate is marked required=false: {backup_record.record_id}",
            )
        )
    return BackupGateEvidence(
        required=backup_record.required or request.backup_required,
        status=backup_record.status,
        evidence=dict(backup_record.evidence),
    )


def _validate_rollback_target(
    *,
    profile: LaunchplaneProductProfileRecord,
    lane: ProductLaneProfile,
    deployment_record: DeploymentRecord,
    blockers: list[GenericWebRollbackBlocker],
) -> None:
    if deployment_record.context != lane.context or deployment_record.instance != lane.instance:
        blockers.append(
            _blocker(
                "deployment_scope_mismatch",
                "generic web rollback target deployment record does not match destination lane",
            )
        )
    if deployment_record.deploy.status != "pass":
        blockers.append(
            _blocker(
                "target_deploy_not_passed",
                "generic web rollback target deployment record is not passing",
            )
        )
    if deployment_record.destination_health.status == "fail":
        blockers.append(
            _blocker(
                "health_evidence_failed",
                "generic web rollback target deployment record has failed health evidence",
            )
        )
    if deployment_record.artifact_identity is None:
        blockers.append(
            _blocker(
                "missing_rollback_target",
                "generic web rollback target deployment record has no artifact identity",
            )
        )
        return
    immutable_artifact_id = _immutable_artifact_id(
        profile=profile,
        deployment_record=deployment_record,
    )
    if not immutable_artifact_id:
        blockers.append(
            _blocker(
                "mutable_artifact_reference",
                "generic web rollback target must use an immutable image digest",
            )
        )
    elif _deployment_target_is_application(deployment_record) and not _deploy_reference(
        deployment_record
    ):
        blockers.append(
            _blocker(
                "missing_deploy_reference",
                "generic web application rollback target has no immutable provider deploy reference",
            )
        )


def _resolve_generic_web_profile_lane(
    *,
    record_store: GenericWebRollbackPlanReader,
    product: str,
    instance: str,
) -> tuple[LaunchplaneProductProfileRecord, ProductLaneProfile]:
    profile = record_store.read_product_profile_record(product)
    if not _product_profile_uses_generic_web_base(profile):
        raise ValueError(
            f"Product {profile.product!r} is configured for driver {profile.driver_id!r}, "
            "not generic-web or a generic-web based driver."
        )
    for lane in profile.lanes:
        if lane.instance == instance:
            return profile, lane
    raise ValueError(
        f"Product {profile.product!r} has no generic-web lane for instance {instance!r}."
    )


def _product_profile_uses_generic_web_base(profile: LaunchplaneProductProfileRecord) -> bool:
    driver_id = profile.driver_id.strip()
    if driver_id == "generic-web":
        return True
    try:
        return read_driver_descriptor(driver_id).base_driver_id == "generic-web"
    except FileNotFoundError:
        return False


def _immutable_artifact_id(
    *, profile: LaunchplaneProductProfileRecord, deployment_record: DeploymentRecord
) -> str:
    if deployment_record.artifact_identity is None:
        return ""
    artifact_id = deployment_record.artifact_identity.artifact_id.strip()
    image_repository = profile.image.repository.strip().rstrip("/")
    if artifact_id.startswith("sha256:"):
        return f"{image_repository}@{artifact_id}"
    if artifact_id.startswith(f"{image_repository}@sha256:"):
        return artifact_id
    return ""


def _deploy_reference(deployment_record: DeploymentRecord) -> str:
    if deployment_record.runtime_identity is None:
        return ""
    image_reference = deployment_record.runtime_identity.image_reference.strip()
    if not is_non_floating_tag_reference(image_reference):
        return ""
    return image_reference


def _deployment_target_is_application(deployment_record: DeploymentRecord) -> bool:
    if deployment_record.resolved_target is not None:
        return deployment_record.resolved_target.target_type == "application"
    if deployment_record.deployed_target is not None:
        return (
            deployment_record.deployed_target.target_category == "application"
            or deployment_record.deployed_target.provider_target_type == "application"
        )
    return (
        deployment_record.deploy.target_category == "application"
        or deployment_record.deploy.provider_target_type == "application"
    )


def _generic_web_rollback_plan_id(*, context: str, instance: str, deployment_record_id: str) -> str:
    return "-".join(
        (
            "generic-web-rollback-plan",
            context.strip().lower(),
            instance.strip().lower(),
            deployment_record_id.strip().lower(),
        )
    )


def _blocker(code: GenericWebRollbackBlockerCode, message: str) -> GenericWebRollbackBlocker:
    return GenericWebRollbackBlocker(code=code, message=message)


def _required_text(value: str, message: str) -> str:
    normalized_value = value.strip()
    if not normalized_value:
        raise ValueError(message)
    return normalized_value
