from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
from pathlib import Path
import secrets
from threading import Lock
import time
from typing import Any, Literal, NamedTuple, Protocol, TypeVar, cast, overload

from pydantic import BaseModel
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    JSON,
    Index,
    Integer,
    String,
    UniqueConstraint,
    create_engine,
    delete,
    desc,
    false,
    func,
    inspect,
    or_,
    text,
    select,
)
from sqlalchemy.dialects.postgresql import JSONB, insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker
from sqlalchemy.pool import NullPool

from control_plane.contracts.artifact_identity import ArtifactIdentityManifest
from control_plane.contracts.agent_write_intent import AgentWriteIntentRecord
from control_plane.contracts.administrator_enrollment import (
    AdministratorEnrollmentConflictError,
    AdministratorEnrollmentRecord,
    complete_administrator_enrollment,
    expire_administrator_enrollment,
    prove_administrator_enrollment_control,
    withdraw_administrator_enrollment,
)
from control_plane.contracts.solo_administration_confirmation import (
    SoloAdministrationConfirmationConflictError,
    SoloAdministrationConfirmationConsumptionBinding,
    SoloAdministrationConfirmationLifecycleEventRecord,
    SoloAdministrationConfirmationRecord,
    build_solo_administration_confirmation_lifecycle_event,
    consume_solo_administration_confirmation,
    expire_solo_administration_confirmation,
    revoke_solo_administration_confirmation,
)
from control_plane.contracts.authz_denial_record import AuthzDenialRecord
from control_plane.contracts.authz_policy_record import (
    AuthzPolicyCompareWriteResult,
    LaunchplaneAuthzPolicyRecord,
    build_authz_policy_record_id,
)
from control_plane.contracts.backup_gate_record import BackupGateRecord
from control_plane.contracts.change_impact import ChangeImpactPolicyRecord
from control_plane.contracts.change_impact_audit import (
    ChangeImpactPolicyAuditRecord,
    ChangeImpactPolicyAuditedWriteResult,
)
from control_plane.contracts.deployment_record import DeploymentRecord
from control_plane.contracts.deploy_target import ProviderTargetRecord
from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.durable_operation_authorization import DurableOperationAuthorization
from control_plane.contracts.edge_endpoint_record import EdgeEndpointRecord
from control_plane.contracts.environment_inventory import EnvironmentInventory
from control_plane.contracts.every_code_preview_gate_record import EveryCodePreviewGateRecord
from control_plane.contracts.every_code_notifications import (
    EveryCodeNotificationAttemptRecord,
    EveryCodeNotificationPolicyRecord,
)
from control_plane.contracts.every_code_work_request import (
    EveryCodeWorkRequestHeartbeat,
    EveryCodeWorkRequestRecord,
    EveryCodeWorkRequestStatusUpdate,
    apply_every_code_work_request_status,
    claim_every_code_work_request,
    close_every_code_work_request_for_pull_request,
    heartbeat_every_code_work_request,
    recover_stale_every_code_work_request,
)
from control_plane.contracts.engineering_review_run import (
    EngineeringReviewAuthorityRecord,
    EngineeringReviewConflictError,
    EngineeringReviewRunFailure,
    EngineeringReviewRunRecord,
    EngineeringReviewRunSubmission,
    EngineeringReviewSequenceError,
    claim_engineering_review_run,
    expire_engineering_review_run,
    fail_engineering_review_run,
    start_engineering_review_run,
    submit_engineering_review_run,
)
from control_plane.contracts.engineering_review_decision import (
    EngineeringReviewDecisionRecord,
)
from control_plane.contracts.every_code_pr_feedback_record import EveryCodePrFeedbackRecord
from control_plane.contracts.generic_web_rollback import GenericWebRollbackPlanRecord
from control_plane.contracts.idempotency_record import (
    LaunchplaneIdempotencyRecord,
    build_launchplane_mutation_reservation,
    complete_launchplane_mutation_reservation,
    format_launchplane_mutation_timestamp,
    parse_launchplane_mutation_timestamp,
)
from control_plane.contracts.ingress_canary_route_record import IngressCanaryRouteRecord
from control_plane.contracts.ingress_route_audit_record import IngressRouteAuditRecord
from control_plane.contracts.lane_summary import LaunchplaneLaneSummary
from control_plane.contracts.manager_preview_approval import (
    ManagerPreviewApprovalEventRecord,
    ManagerPreviewApprovalEventWriteStatus,
)
from control_plane.contracts.merge_admission_record import (
    MergeAdmissionFenceRejectedError,
    MergeAdmissionRecord,
    MergeLandingOutcomeRecord,
    validate_merge_admission_controller_fence,
    validate_merge_landing_outcome_for_admission,
    validate_merge_landing_outcome_successor,
)
from control_plane.contracts.owner_acceptance import (
    OwnerAcceptanceEventRecord,
    OwnerAcceptanceEventWriteStatus,
    owner_acceptance_event_replay_matches,
    owner_acceptance_subject_key,
    validate_owner_acceptance_event_transition,
)
from control_plane.contracts.owner_control import (
    ChannelBindingRecord,
    OwnerControlConfirmationEnvelope,
    owner_control_channel_binding_sha256,
)
from control_plane.contracts.owner_control_enrollment_provenance import (
    OwnerControlChannelEnrollment,
    OwnerControlEnrollmentProvenanceConflictError,
    OwnerControlEnrollmentProvenanceRecord,
    OwnerControlHostPrincipalClaim,
    build_owner_control_enrollment_provenance_record,
)
from control_plane.contracts.owner_control_shadow_verifier import (
    OWNER_CONTROL_SHADOW_MAX_ATTEMPTS,
    OwnerControlChannelSessionRecord,
    OwnerControlChallengeLifecycleEventRecord,
    OwnerControlChallengeIssueRequest,
    OwnerControlIssuedChallengeRecord,
    OwnerControlShadowVerificationEvaluation,
    OwnerControlShadowVerificationEventRecord,
    OwnerControlShadowVerificationResult,
    OwnerControlShadowVerifierConflictError,
    build_owner_control_channel_session_record,
    evaluate_owner_control_shadow_verification,
    issue_owner_control_challenge_record,
    terminalize_expired_owner_control_challenge_record,
    owner_control_confirmation_envelope_sha256,
    owner_control_verification_event_id,
    revoke_owner_control_channel_session_record,
)
from control_plane.contracts.merge_train_batch import (
    MergeTrainBatchCandidateRecord,
    MergeTrainBatchLandingPlanRecord,
)
from control_plane.contracts.merge_train_controller_state import (
    MergeTrainControllerAdoptionRejectedError,
    MergeTrainControllerLeaseHeldError,
    MergeTrainControllerLeaseLostError,
    MergeTrainControllerStateRecord,
    build_merge_train_controller_key,
    build_merge_train_controller_resume_detail,
    build_merge_train_controller_state_record,
)
from control_plane.contracts.merge_train_stack_collapse import (
    MergeTrainStackCollapsePlanRecord,
)
from control_plane.contracts.merge_train_run_record import MergeTrainRunRecord
from control_plane.contracts.merge_train_policy import (
    MergeTrainPolicyCompareWriteResult,
    MergeTrainPolicyRecord,
)
from control_plane.contracts.merge_train_pr_feedback_record import (
    MergeTrainPrFeedbackRecord,
)
from control_plane.contracts.odoo_instance_override_record import OdooInstanceOverrideRecord
from control_plane.contracts.odoo_prod_backup_restore_operation import (
    ODOO_PROD_BACKUP_RESTORE_OPERATION_PHASE_SEQUENCE,
    OdooProdBackupRestoreCheckpoint,
    OdooProdBackupRestoreOperationPhase,
    OdooProdBackupRestoreOperationRecord,
    odoo_prod_backup_restore_operation_is_verification_replay_claim,
    requeue_odoo_prod_backup_restore_verification_replay,
)
from control_plane.contracts.odoo_prod_retained_volume_backup_import_operation import (
    ODOO_PROD_RETAINED_VOLUME_BACKUP_IMPORT_OPERATION_PHASE_SEQUENCE,
    OdooProdRetainedVolumeBackupImportCheckpoint,
    OdooProdRetainedVolumeBackupImportOperationPhase,
    OdooProdRetainedVolumeBackupImportOperationRecord,
)
from control_plane.contracts.odoo_stable_bootstrap_operation import (
    OdooStableBootstrapOperationRecord,
)
from control_plane.contracts.odoo_stable_target_replacement_operation import (
    OdooStableTargetReplacementOperationRecord,
)
from control_plane.odoo_stable_lane import (
    ODOO_STABLE_LANE_BLOCKING_STATUSES,
    OdooStableLaneOperationConflictError,
    OdooStableLaneOperationKind,
    OdooStableLaneOperationOwner,
    odoo_stable_lane_cancellation_is_allowed,
    odoo_stable_lane_operation_priority,
)
from control_plane.contracts.outbox_delivery import OutboxDeliveryRecord
from control_plane.contracts.product_owner import (
    ProductOwnerPolicyRecord,
    ProductOwnerRequirementRecord,
    ProductOwnerRoutingRecord,
)
from control_plane.contracts.privileged_operation import (
    PrivilegedOperationConflictError,
    PrivilegedOperationEventRecord,
    PrivilegedOperationEventWriteStatus,
    PrivilegedOperationRecord,
    PrivilegedOperationTransitionError,
    privileged_operation_event_replay_digest,
    privileged_operation_plan_replay_digest,
    privileged_operation_record_digest,
    validate_privileged_operation_transition,
)
from control_plane.contracts.privileged_operation_worker_heartbeat import (
    PrivilegedOperationWorkerHeartbeatRecord,
)
from control_plane.owner_control_challenge import (
    OwnerControlChallengeProvenanceError,
    derive_owner_control_approval_request,
    owner_control_challenge_semantics,
)
from control_plane.contracts.preview_desired_state_record import PreviewDesiredStateRecord
from control_plane.contracts.preview_enablement_record import PreviewEnablementRecord
from control_plane.contracts.preview_generation_record import PreviewGenerationRecord
from control_plane.contracts.preview_inventory_scan_record import PreviewInventoryScanRecord
from control_plane.contracts.preview_lifecycle_cleanup_record import PreviewLifecycleCleanupRecord
from control_plane.contracts.preview_lifecycle_plan_record import PreviewLifecyclePlanRecord
from control_plane.contracts.preview_pr_feedback_notifications import (
    PreviewPrFeedbackNotificationAttemptRecord,
    PreviewPrFeedbackNotificationPolicyRecord,
)
from control_plane.contracts.preview_pr_feedback_record import PreviewPrFeedbackRecord
from control_plane.contracts.preview_pr_feedback_remediation import (
    PreviewPrFeedbackRemediationRecord,
)
from control_plane.contracts.preview_record import PreviewRecord
from control_plane.contracts.preview_summary import LaunchplanePreviewSummary
from control_plane.contracts.private_health_endpoint_record import (
    PrivateHealthEndpointRecord,
    private_health_endpoint_record_sha256,
)
from control_plane.manager_preview_approval import ManagerPreviewApprovalEventConflictError
from control_plane.owner_acceptance import OwnerAcceptanceEventConflictError
from control_plane.contracts.route_binding_record import (
    EnvironmentRouteBindingRecord,
    route_binding_record_sha256,
)
from control_plane.contracts.product_health_monitoring_migration import (
    canonical_health_check_record_token,
    migrate_product_profile_health_monitoring_payload,
)
from control_plane.contracts.product_monitoring_intent_migration import (
    migrate_product_profile_monitoring_intent_payload,
)
from control_plane.contracts.product_profile_lifecycle_migration import (
    migrate_product_profile_lifecycle_payload,
)
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    product_profile_record_sha256,
)
from control_plane.contracts.production_backup_authority import (
    ProductionBackupPolicyRecord,
    ProductionBackupTargetRecord,
)
from control_plane.contracts.product_retirement import ProductRetirementRecord
from control_plane.contracts.detached_application_retirement import (
    DetachedApplicationRetirementRecord,
)
from control_plane.contracts.public_ingress_monitoring import (
    PublicIngressIncidentEventRecord,
    PublicIngressIncidentReminderStateRecord,
    PublicIngressNotificationAttemptRecord,
)
from control_plane.contracts.public_ingress_monitoring import (
    PublicIngressNotificationPolicyRecord,
)
from control_plane.contracts.public_ingress_monitoring import PublicIngressIncidentRecord
from control_plane.contracts.public_ingress_monitoring import PublicIngressObservationRecord
from control_plane.contracts.public_ingress_monitoring import (
    public_ingress_incident_record_sha256,
)
from control_plane.contracts.promotion_record import PromotionRecord
from control_plane.contracts.release_tuple_record import ReleaseTupleRecord
from control_plane.contracts.runtime_environment_record import (
    RuntimeEnvironmentDeleteEvent,
    RuntimeEnvironmentRecord,
)
from control_plane.contracts.runtime_key_safety_policy import RuntimeKeySafetyPolicyRecord
from control_plane.contracts.runner_host_hygiene import RunnerHostHygieneApplyAuditRecord
from control_plane.contracts.runner_host_hygiene import (
    sanitize_runner_host_hygiene_audit_record_for_persistence,
)
from control_plane.contracts.runner_lane_registration import RunnerLaneRegistrationAuditRecord
from control_plane.contracts.secret_record import (
    SecretAuditEvent,
    SecretBinding,
    SecretRecord,
    SecretRotationWrite,
    SecretVersion,
)
from control_plane.contracts.tenant_merge_eligibility import (
    TenantMergeCandidate,
    TenantRepositoryClassificationLookup,
    TenantRepositoryClassificationRecord,
)
from control_plane.contracts.repository_inventory import RepositoryInventoryRecord
from control_plane.contracts.repository_human_admission import (
    RepositoryHumanRolePolicyRecord,
    TenantTechnicalHumanWaiverEventRecord,
)
from control_plane.contracts.trusted_maintenance import (
    TrustedMaintenanceEvidenceRecord,
    TrustedMaintenancePolicyRecord,
)
from control_plane.repository_human_admission import (
    RepositoryHumanRolePolicyConflictError,
    RepositoryHumanRolePolicySequenceError,
    TenantTechnicalHumanWaiverApplyEnvelope,
    TenantTechnicalHumanWaiverApplyResult,
    TenantTechnicalHumanWaiverAuthorizationError,
    TenantTechnicalHumanWaiverEventConflictError,
    TenantTechnicalHumanWaiverRevokeCurrentError,
    TenantTechnicalHumanWaiverStaleAuthorityError,
    build_tenant_technical_human_waiver_apply_result,
    capture_tenant_technical_human_waiver_event,
    plan_repository_human_role_policy_apply,
    plan_repository_human_role_policy_append,
    plan_tenant_technical_human_waiver_event_append,
    tenant_technical_human_waiver_current_authority,
)
from control_plane.tenant_repository_classification import (
    TenantRepositoryClassificationConflictError,
    TenantRepositoryClassificationSequenceError,
    plan_tenant_repository_classification_append,
)
from control_plane.repository_inventory import (
    RepositoryInventoryConflictError,
    RepositoryInventorySequenceError,
    plan_repository_inventory_append,
)
from control_plane.production_backup_authority import (
    ProductionBackupAuthorityWritePlan,
    ProductionBackupAuthorityWriteEnvelope,
    ProductionBackupAuthorityWriteResult,
    ProductionBackupPolicyAppendPlan,
    ProductionBackupTargetAppendPlan,
    plan_production_backup_policy_append,
    plan_production_backup_authority_write_from_records,
    plan_production_backup_target_append,
    validate_production_backup_policy_binding,
)
from control_plane.trusted_maintenance import (
    TrustedMaintenanceAuthorityError,
    TrustedMaintenanceExpectedAuthority,
    TrustedMaintenanceGitHubEventFacts,
    TrustedMaintenanceRuleMatchError,
    TrustedMaintenancePolicyConflictError,
    TrustedMaintenancePolicySequenceError,
    capture_trusted_maintenance_evidence,
    plan_trusted_maintenance_evidence_append,
    plan_trusted_maintenance_policy_apply,
    plan_trusted_maintenance_policy_append,
    trusted_maintenance_current_authority,
)
from control_plane.product_owner_service import (
    ProductOwnerPolicyConflictError,
    ProductOwnerPolicySequenceError,
    ProductOwnerRequirementConflictError,
    ProductOwnerRequirementSequenceError,
    ProductOwnerRoutingConflictError,
    ProductOwnerRoutingSequenceError,
)
from control_plane.change_impact_service import (
    ChangeImpactPolicyConflictError,
    ChangeImpactPolicySequenceError,
)
from control_plane.contracts.verireel_prod_backup_gate_operation import (
    VeriReelProdBackupGateOperationRecord,
    build_cancelled_verireel_prod_backup_gate_record,
)
from control_plane.service_auth import GitHubHumanIdentity
from control_plane.service_human_auth import HumanSessionStore, LaunchplaneHumanSession
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.storage.product_authority_bundle import (
    ProductAuthorityBundle,
    ProviderTargetWrite,
)
from control_plane.storage.schema_invariants import (
    RUNTIME_COMPATIBLE_ALEMBIC_REVISIONS,
    verify_postgres_schema_invariants,
)

RecordModel = TypeVar("RecordModel", bound=BaseModel)

_SQLITE_OWNER_ACCEPTANCE_PROJECTION_LOCKS_GUARD = Lock()
_SQLITE_OWNER_ACCEPTANCE_PROJECTION_LOCKS: dict[str, Lock] = {}
ConnectionFactory = Callable[[], Any]
PayloadDict = dict[str, Any]
PayloadJsonType = JSON().with_variant(JSONB(), "postgresql")
RuntimeEnvironmentDeleteStatus = Literal["deleted", "missing", "changed"]
CurrentAuthorityDeleteStatus = Literal["deleted", "missing", "changed"]
ProductProfileCompareWriteStatus = Literal[
    "written",
    "missing",
    "changed",
    "replayed",
    "idempotency_conflict",
    "reservation_in_progress",
    "reconciliation_required",
]
TenantRepositoryClassificationCompareWriteStatus = Literal[
    "written",
    "replayed",
    "idempotency_conflict",
    "reservation_in_progress",
    "reconciliation_required",
]
RepositoryInventoryCompareWriteStatus = Literal[
    "written",
    "replayed",
    "idempotency_conflict",
    "reservation_in_progress",
    "reconciliation_required",
]
ProductionBackupAuthorityCompareWriteStatus = Literal[
    "written",
    "replayed",
    "idempotency_conflict",
    "reservation_in_progress",
    "reconciliation_required",
]
RepositoryHumanRolePolicyCompareWriteStatus = Literal[
    "written",
    "exact_replay",
    "replayed",
    "idempotency_conflict",
    "reservation_in_progress",
    "reconciliation_required",
]
TrustedMaintenancePolicyCompareWriteStatus = Literal[
    "written",
    "exact_replay",
    "replayed",
    "idempotency_conflict",
    "reservation_in_progress",
    "reconciliation_required",
]
TenantTechnicalHumanWaiverCompareWriteStatus = Literal[
    "written",
    "exact_replay",
    "replayed",
    "idempotency_conflict",
    "reservation_in_progress",
    "reconciliation_required",
]
PublicIngressTransitionWriteStatus = Literal[
    "written",
    "authority_changed",
    "incident_changed",
]
ProviderTargetCreateStatus = Literal["created", "exists"]
MutationReservationDecision = Literal[
    "acquired",
    "replayed",
    "conflict",
    "target_busy",
    "in_progress",
    "reconcile_required",
]
MutationReservationUpdateStatus = Literal[
    "updated",
    "released",
    "missing",
    "not_running",
    "owner_mismatch",
    "reservation_mismatch",
    "lease_expired",
    "reconciliation_conflict",
]
MutationReservationCompletionStatus = Literal[
    "completed",
    "replayed",
    "conflict",
    "missing",
    "not_running",
    "owner_mismatch",
    "reservation_mismatch",
    "lease_expired",
    "reconcile_required",
]
DbOnlyMutationPreflightStatus = Literal[
    "missing",
    "released",
    "replayed",
    "conflict",
    "in_progress",
    "reconcile_required",
]
RouteBindingReconcileWriteStatus = Literal[
    "created",
    "refreshed",
    "unchanged",
    "missing",
    "changed",
    "replayed",
    "idempotency_conflict",
    "reservation_in_progress",
    "reconciliation_required",
]
MutationReservationAdoptionStatus = Literal[
    "adopted",
    "replayed",
    "missing",
    "conflict",
    "not_reconcile_required",
    "reservation_mismatch",
]
MutationReservationReleaseStatus = Literal[
    "released",
    "missing",
    "not_running",
    "owner_mismatch",
    "reservation_mismatch",
]
MutationReconciliationRetryStatus = Literal[
    "acquired",
    "replayed",
    "missing",
    "conflict",
    "not_reconcile_required",
    "reservation_mismatch",
]
MutationReconciliationSupersessionStatus = Literal[
    "acquired",
    "retry",
    "missing",
    "not_reconcile_required",
    "reservation_mismatch",
    "lease_active",
    "grace_active",
]
ExistingMutationReservationLookupStatus = Literal[
    "found",
    "missing",
    "conflict",
    "ambiguous",
    "hold_unknown",
]


class ProductProfileCompareWriteResult(NamedTuple):
    status: ProductProfileCompareWriteStatus
    idempotency_record: LaunchplaneIdempotencyRecord | None = None


class TenantRepositoryClassificationCompareWriteResult(NamedTuple):
    status: TenantRepositoryClassificationCompareWriteStatus
    idempotency_record: LaunchplaneIdempotencyRecord | None = None


class RepositoryInventoryCompareWriteResult(NamedTuple):
    status: RepositoryInventoryCompareWriteStatus
    idempotency_record: LaunchplaneIdempotencyRecord | None = None


class ProductionBackupAuthorityCompareWriteResult(NamedTuple):
    status: ProductionBackupAuthorityCompareWriteStatus
    result: ProductionBackupAuthorityWriteResult | None = None
    idempotency_record: LaunchplaneIdempotencyRecord | None = None


class RepositoryHumanRolePolicyCompareWriteResult(NamedTuple):
    status: RepositoryHumanRolePolicyCompareWriteStatus
    idempotency_record: LaunchplaneIdempotencyRecord | None = None


class TrustedMaintenancePolicyCompareWriteResult(NamedTuple):
    status: TrustedMaintenancePolicyCompareWriteStatus
    idempotency_record: LaunchplaneIdempotencyRecord | None = None


class TenantTechnicalHumanWaiverCompareWriteResult(NamedTuple):
    status: TenantTechnicalHumanWaiverCompareWriteStatus
    result: TenantTechnicalHumanWaiverApplyResult | None = None
    event_record: TenantTechnicalHumanWaiverEventRecord | None = None
    idempotency_record: LaunchplaneIdempotencyRecord | None = None


class PublicIngressTransitionWriteResult(NamedTuple):
    status: PublicIngressTransitionWriteStatus


class MutationReservationResult(NamedTuple):
    status: MutationReservationDecision
    record: LaunchplaneIdempotencyRecord


class ExistingMutationReservationLookupResult(NamedTuple):
    status: ExistingMutationReservationLookupStatus
    record: LaunchplaneIdempotencyRecord | None
    observed_at: str


class MutationReservationUpdateResult(NamedTuple):
    status: MutationReservationUpdateStatus
    record: LaunchplaneIdempotencyRecord | None = None


class MutationReservationCompletionResult(NamedTuple):
    status: MutationReservationCompletionStatus
    record: LaunchplaneIdempotencyRecord | None = None


class MutationReconciliationSupersessionResult(NamedTuple):
    status: MutationReconciliationSupersessionStatus
    record: LaunchplaneIdempotencyRecord | None = None


class DbOnlyMutationPreflightResult(NamedTuple):
    status: DbOnlyMutationPreflightStatus
    record: LaunchplaneIdempotencyRecord | None = None


class RouteBindingReconcileWriteResult(NamedTuple):
    status: RouteBindingReconcileWriteStatus
    current_record: EnvironmentRouteBindingRecord | None = None
    idempotency_record: LaunchplaneIdempotencyRecord | None = None


OutboxDeliveryClaimStatus = Literal["claimed", "empty"]
OutboxDeliveryCompletionStatus = Literal[
    "updated",
    "missing",
    "not_running",
    "owner_mismatch",
    "lease_expired",
]


class OutboxDeliveryClaimResult(NamedTuple):
    status: OutboxDeliveryClaimStatus
    record: OutboxDeliveryRecord | None = None


class OutboxDeliveryCompletionResult(NamedTuple):
    status: OutboxDeliveryCompletionStatus
    record: OutboxDeliveryRecord | None = None


class MutationReservationAdoptionResult(NamedTuple):
    status: MutationReservationAdoptionStatus
    record: LaunchplaneIdempotencyRecord | None = None


class MutationReservationReleaseResult(NamedTuple):
    status: MutationReservationReleaseStatus
    record: LaunchplaneIdempotencyRecord | None = None


class MutationReconciliationRetryResult(NamedTuple):
    status: MutationReconciliationRetryStatus
    record: LaunchplaneIdempotencyRecord | None = None


@dataclass(frozen=True)
class DbOnlyMutationRequest:
    scope: str
    route_path: str
    idempotency_key: str
    request_fingerprint: str
    lease_owner: str
    response_status_code: int
    response_trace_id: str
    response_payload: dict[str, Any]
    replay_response_payload: dict[str, Any] | None = None
    confirmation_consumption: SoloAdministrationConfirmationConsumptionBinding | None = None
    lease_seconds: int = 300


@dataclass(frozen=True)
class OutboxWithIdempotencyRequest:
    delivery: OutboxDeliveryRecord
    idempotency_record: LaunchplaneIdempotencyRecord | None = None


@dataclass(frozen=True)
class _TenantTechnicalHumanWaiverAuthoritySnapshot:
    classifications: tuple[TenantRepositoryClassificationRecord, ...]
    role_policies: tuple[RepositoryHumanRolePolicyRecord, ...]
    authz_policies: tuple[LaunchplaneAuthzPolicyRecord, ...]

    def list_tenant_repository_classification_records(
        self,
        *,
        repository_id: str = "",
        limit: int | None = None,
    ) -> tuple[TenantRepositoryClassificationRecord, ...]:
        records = tuple(
            record
            for record in self.classifications
            if not repository_id or record.repository_id == repository_id
        )
        return records if limit is None else records[: max(limit, 0)]

    def list_repository_human_role_policy_records(
        self,
        *,
        repository_id: str = "",
        repository_owner_id: str = "",
        repository: str = "",
        product: str = "",
        context: str = "",
        status: str = "",
        limit: int | None = None,
    ) -> tuple[RepositoryHumanRolePolicyRecord, ...]:
        normalized_repository = repository.strip().lower()
        records = tuple(
            record
            for record in self.role_policies
            if (not repository_id or record.repository_id == repository_id)
            and (not repository_owner_id or record.repository_owner_id == repository_owner_id)
            and (not normalized_repository or record.repository == normalized_repository)
            and (not product or record.product == product)
            and (not context or record.context == context)
            and (not status or record.status == status)
        )
        return records if limit is None else records[: max(limit, 0)]

    def list_authz_policy_records(
        self,
        *,
        status: str = "",
        limit: int | None = None,
    ) -> tuple[LaunchplaneAuthzPolicyRecord, ...]:
        records = tuple(
            record for record in self.authz_policies if not status or record.status == status
        )
        return records if limit is None else records[: max(limit, 0)]

    @staticmethod
    def list_tenant_technical_human_waiver_event_records(
        **_: object,
    ) -> tuple[TenantTechnicalHumanWaiverEventRecord, ...]:
        return ()


@dataclass(frozen=True)
class _TrustedMaintenanceAuthoritySnapshot:
    classifications: tuple[TenantRepositoryClassificationRecord, ...]
    policies: tuple[TrustedMaintenancePolicyRecord, ...]

    def list_tenant_repository_classification_records(
        self,
        *,
        repository_id: str = "",
        limit: int | None = None,
    ) -> tuple[TenantRepositoryClassificationRecord, ...]:
        records = tuple(
            record
            for record in self.classifications
            if not repository_id or record.repository_id == repository_id
        )
        return records if limit is None else records[: max(limit, 0)]

    def list_trusted_maintenance_policy_records(
        self,
        *,
        repository_id: str = "",
        repository_owner_id: str = "",
        repository: str = "",
        product: str = "",
        context: str = "",
        status: str = "",
        limit: int | None = None,
    ) -> tuple[TrustedMaintenancePolicyRecord, ...]:
        normalized_repository = repository.strip().lower()
        records = tuple(
            record
            for record in self.policies
            if (not repository_id or record.repository_id == repository_id)
            and (not repository_owner_id or record.repository_owner_id == repository_owner_id)
            and (not normalized_repository or record.repository == normalized_repository)
            and (not product or record.product == product)
            and (not context or record.context == context)
            and (not status or record.status == status)
        )
        return records if limit is None else records[: max(limit, 0)]

    @staticmethod
    def list_trusted_maintenance_evidence_records(
        **_: object,
    ) -> tuple[TrustedMaintenanceEvidenceRecord, ...]:
        return ()


def _utc_now_timestamp() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class _PayloadRow(Protocol):
    payload: PayloadDict


def _string_value(value: Any) -> str:
    return str(value)


def _payload_from_row(row: object) -> PayloadDict:
    return cast(_PayloadRow, row).payload


class Base(DeclarativeBase):
    pass


class LaunchplaneBackupGateRow(Base):
    __tablename__ = "launchplane_backup_gates"
    __table_args__ = (
        Index(
            "launchplane_backup_gates_context_instance_idx",
            "context",
            "instance",
            desc("created_at"),
        ),
    )

    record_id: Mapped[str] = mapped_column(String, primary_key=True)
    context: Mapped[str] = mapped_column(String, nullable=False)
    instance: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[PayloadDict] = mapped_column(PayloadJsonType, nullable=False)


class LaunchplaneProductionBackupTargetRow(Base):
    __tablename__ = "launchplane_production_backup_targets"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'superseded', 'retired')",
            name="launchplane_production_backup_target_status_ck",
        ),
        CheckConstraint(
            "target_revision >= 1",
            name="launchplane_production_backup_target_revision_ck",
        ),
        CheckConstraint(
            "(target_revision = 1 AND supersedes_record_id IS NULL) OR "
            "(target_revision > 1 AND supersedes_record_id IS NOT NULL)",
            name="launchplane_production_backup_target_supersedes_ck",
        ),
        Index(
            "launchplane_production_backup_target_revision_uidx",
            "target_id",
            "target_revision",
            unique=True,
        ),
        Index(
            "launchplane_production_backup_target_active_uidx",
            "target_id",
            unique=True,
            sqlite_where=text("status = 'active'"),
            postgresql_where=text("status = 'active'"),
        ),
        Index(
            "launchplane_production_backup_target_current_idx",
            "target_id",
            "status",
            desc("target_revision"),
        ),
    )

    record_id: Mapped[str] = mapped_column(String, primary_key=True)
    target_id: Mapped[str] = mapped_column(String, nullable=False)
    target_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    provider_type: Mapped[str] = mapped_column(String, nullable=False)
    destination_kind: Mapped[str] = mapped_column(String, nullable=False)
    effective_at: Mapped[str] = mapped_column(String, nullable=False)
    review_after: Mapped[str] = mapped_column(String, nullable=False)
    supersedes_record_id: Mapped[str | None] = mapped_column(String, nullable=True)
    target_digest: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[PayloadDict] = mapped_column(PayloadJsonType, nullable=False)


class LaunchplaneProductionBackupPolicyRow(Base):
    __tablename__ = "launchplane_production_backup_policies"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'superseded', 'retired')",
            name="launchplane_production_backup_policy_status_ck",
        ),
        CheckConstraint(
            "policy_revision >= 1",
            name="launchplane_production_backup_policy_revision_ck",
        ),
        CheckConstraint(
            "(policy_revision = 1 AND supersedes_record_id IS NULL) OR "
            "(policy_revision > 1 AND supersedes_record_id IS NOT NULL)",
            name="launchplane_production_backup_policy_supersedes_ck",
        ),
        Index(
            "launchplane_production_backup_policy_revision_uidx",
            "policy_id",
            "policy_revision",
            unique=True,
        ),
        Index(
            "launchplane_production_backup_policy_active_uidx",
            "product",
            "context",
            "instance",
            "promotion_action",
            unique=True,
            sqlite_where=text("status = 'active'"),
            postgresql_where=text("status = 'active'"),
        ),
        Index(
            "launchplane_production_backup_policy_current_idx",
            "product",
            "context",
            "instance",
            "promotion_action",
            "status",
            desc("policy_revision"),
        ),
    )

    record_id: Mapped[str] = mapped_column(String, primary_key=True)
    policy_id: Mapped[str] = mapped_column(String, nullable=False)
    product: Mapped[str] = mapped_column(String, nullable=False)
    context: Mapped[str] = mapped_column(String, nullable=False)
    instance: Mapped[str] = mapped_column(String, nullable=False)
    promotion_action: Mapped[str] = mapped_column(String, nullable=False)
    policy_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    source_target_id: Mapped[str] = mapped_column(String, nullable=False)
    destination_target_id: Mapped[str] = mapped_column(String, nullable=False)
    effective_at: Mapped[str] = mapped_column(String, nullable=False)
    review_after: Mapped[str] = mapped_column(String, nullable=False)
    supersedes_record_id: Mapped[str | None] = mapped_column(String, nullable=True)
    policy_digest: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[PayloadDict] = mapped_column(PayloadJsonType, nullable=False)


class LaunchplaneArtifactManifestRow(Base):
    __tablename__ = "launchplane_artifact_manifests"
    __table_args__ = (Index("launchplane_artifact_manifests_artifact_idx", desc("artifact_id")),)

    artifact_id: Mapped[str] = mapped_column(String, primary_key=True)
    source_commit: Mapped[str] = mapped_column(String, nullable=False)
    image_repository: Mapped[str] = mapped_column(String, nullable=False)
    image_digest: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[PayloadDict] = mapped_column(PayloadJsonType, nullable=False)


class LaunchplaneDeploymentRow(Base):
    __tablename__ = "launchplane_deployments"
    __table_args__ = (
        Index(
            "launchplane_deployments_context_instance_idx",
            "context",
            "instance",
            desc("deploy_finished_at"),
            desc("deploy_started_at"),
        ),
    )

    record_id: Mapped[str] = mapped_column(String, primary_key=True)
    context: Mapped[str] = mapped_column(String, nullable=False)
    instance: Mapped[str] = mapped_column(String, nullable=False)
    artifact_id: Mapped[str] = mapped_column(String, nullable=False)
    source_git_ref: Mapped[str] = mapped_column(String, nullable=False)
    deploy_started_at: Mapped[str] = mapped_column(String, nullable=False)
    deploy_finished_at: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[PayloadDict] = mapped_column(PayloadJsonType, nullable=False)


class LaunchplaneGenericWebRollbackPlanRow(Base):
    __tablename__ = "launchplane_generic_web_rollback_plans"
    __table_args__ = (
        Index(
            "launchplane_generic_web_rollback_plans_context_instance_idx",
            "context",
            "instance",
            desc("created_at"),
        ),
    )

    plan_id: Mapped[str] = mapped_column(String, primary_key=True)
    product: Mapped[str] = mapped_column(String, nullable=False)
    context: Mapped[str] = mapped_column(String, nullable=False)
    instance: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    rollback_deployment_record_id: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[PayloadDict] = mapped_column(PayloadJsonType, nullable=False)


class LaunchplanePromotionRow(Base):
    __tablename__ = "launchplane_promotions"
    __table_args__ = (
        Index(
            "launchplane_promotions_context_path_idx",
            "context",
            "from_instance",
            "to_instance",
            desc("deploy_finished_at"),
            desc("deploy_started_at"),
        ),
    )

    record_id: Mapped[str] = mapped_column(String, primary_key=True)
    context: Mapped[str] = mapped_column(String, nullable=False)
    from_instance: Mapped[str] = mapped_column(String, nullable=False)
    to_instance: Mapped[str] = mapped_column(String, nullable=False)
    artifact_id: Mapped[str] = mapped_column(String, nullable=False)
    deploy_started_at: Mapped[str] = mapped_column(String, nullable=False)
    deploy_finished_at: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[PayloadDict] = mapped_column(PayloadJsonType, nullable=False)


class LaunchplaneInventoryRow(Base):
    __tablename__ = "launchplane_inventory"
    __table_args__ = (Index("launchplane_inventory_updated_idx", desc("updated_at")),)

    context: Mapped[str] = mapped_column(String, primary_key=True)
    instance: Mapped[str] = mapped_column(String, primary_key=True)
    artifact_id: Mapped[str] = mapped_column(String, nullable=False)
    source_git_ref: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)
    deployment_record_id: Mapped[str] = mapped_column(String, nullable=False)
    promotion_record_id: Mapped[str] = mapped_column(String, nullable=False)
    promoted_from_instance: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[PayloadDict] = mapped_column(PayloadJsonType, nullable=False)


class LaunchplanePreviewRow(Base):
    __tablename__ = "launchplane_preview_records"
    __table_args__ = (
        Index(
            "launchplane_preview_records_lookup_idx",
            "context",
            "anchor_repo",
            "anchor_pr_number",
            desc("updated_at"),
        ),
    )

    preview_id: Mapped[str] = mapped_column(String, primary_key=True)
    context: Mapped[str] = mapped_column(String, nullable=False)
    anchor_repo: Mapped[str] = mapped_column(String, nullable=False)
    anchor_pr_number: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[PayloadDict] = mapped_column(PayloadJsonType, nullable=False)


class LaunchplanePreviewGenerationRow(Base):
    __tablename__ = "launchplane_preview_generations"
    __table_args__ = (
        Index(
            "launchplane_preview_generations_preview_idx",
            "preview_id",
            desc("sequence"),
            desc("requested_at"),
        ),
    )

    generation_id: Mapped[str] = mapped_column(String, primary_key=True)
    preview_id: Mapped[str] = mapped_column(String, nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(String, nullable=False)
    requested_at: Mapped[str] = mapped_column(String, nullable=False)
    finished_at: Mapped[str] = mapped_column(String, nullable=False)
    artifact_id: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[PayloadDict] = mapped_column(PayloadJsonType, nullable=False)


class LaunchplaneManagerPreviewApprovalEventRow(Base):
    __tablename__ = "launchplane_manager_preview_approval_events"
    __table_args__ = (
        Index(
            "launchplane_manager_preview_approval_events_subject_idx",
            "product",
            "context",
            "repository",
            "pr_number",
            desc("occurred_at"),
        ),
        Index(
            "launchplane_manager_preview_approval_events_preview_idx",
            "preview_id",
            "serving_generation_id",
            desc("occurred_at"),
        ),
        Index(
            "launchplane_manager_preview_approval_events_approval_idx",
            "approval_id",
            desc("occurred_at"),
        ),
    )

    event_id: Mapped[str] = mapped_column(String, primary_key=True)
    approval_id: Mapped[str] = mapped_column(String, nullable=False)
    product: Mapped[str] = mapped_column(String, nullable=False)
    context: Mapped[str] = mapped_column(String, nullable=False)
    repository: Mapped[str] = mapped_column(String, nullable=False)
    pr_number: Mapped[int] = mapped_column(Integer, nullable=False)
    head_sha: Mapped[str] = mapped_column(String, nullable=False)
    preview_id: Mapped[str] = mapped_column(String, nullable=False)
    serving_generation_id: Mapped[str] = mapped_column(String, nullable=False)
    artifact_id: Mapped[str] = mapped_column(String, nullable=False)
    artifact_image_digest: Mapped[str] = mapped_column(String, nullable=False)
    manifest_fingerprint: Mapped[str] = mapped_column(String, nullable=False)
    runtime_identity_sha256: Mapped[str] = mapped_column(String, nullable=False)
    action: Mapped[str] = mapped_column(String, nullable=False)
    manager_github_id: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    manager_login: Mapped[str] = mapped_column(String, nullable=False, server_default="")
    policy_record_id: Mapped[str] = mapped_column(String, nullable=False, server_default="")
    policy_sha256: Mapped[str] = mapped_column(String, nullable=False, server_default="")
    occurred_at: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[PayloadDict] = mapped_column(PayloadJsonType, nullable=False)


class LaunchplaneOwnerAcceptanceEventRow(Base):
    __tablename__ = "launchplane_owner_acceptance_events"
    __table_args__ = (
        Index(
            "launchplane_owner_acceptance_events_subject_idx",
            "repository_id",
            "pr_number",
            "product",
            "system",
            "owner_action",
            "environment",
            desc("subject_sequence"),
        ),
        Index(
            "launchplane_owner_acceptance_events_subject_sequence_uidx",
            "repository_id",
            "pr_number",
            "product",
            "system",
            "owner_action",
            "environment",
            "subject_sequence",
            unique=True,
        ),
        Index(
            "launchplane_owner_acceptance_events_binding_idx",
            "binding_sha256",
            desc("occurred_at"),
        ),
        Index(
            "launchplane_owner_acceptance_events_acceptance_idx",
            "acceptance_id",
            desc("occurred_at"),
        ),
    )

    event_id: Mapped[str] = mapped_column(String, primary_key=True)
    acceptance_id: Mapped[str] = mapped_column(String, nullable=False)
    subject_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    binding_sha256: Mapped[str] = mapped_column(String, nullable=False)
    repository_id: Mapped[str] = mapped_column(String, nullable=False)
    repository_owner_id: Mapped[str] = mapped_column(String, nullable=False)
    repository: Mapped[str] = mapped_column(String, nullable=False)
    pr_number: Mapped[int] = mapped_column(BigInteger, nullable=False)
    head_sha: Mapped[str] = mapped_column(String, nullable=False)
    tree_sha: Mapped[str] = mapped_column(String, nullable=False)
    product: Mapped[str] = mapped_column(String, nullable=False)
    system: Mapped[str] = mapped_column(String, nullable=False)
    owner_action: Mapped[str] = mapped_column(String, nullable=False)
    environment: Mapped[str] = mapped_column(String, nullable=False)
    action: Mapped[str] = mapped_column(String, nullable=False)
    owner_github_id: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    owner_login: Mapped[str] = mapped_column(String, nullable=False, server_default="")
    base_ref: Mapped[str] = mapped_column(String, nullable=False, server_default="")
    base_sha: Mapped[str] = mapped_column(String, nullable=False, server_default="")
    change_class: Mapped[str] = mapped_column(String, nullable=False, server_default="")
    review_max_age_seconds: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        server_default="0",
    )
    contribution_resolution: Mapped[str] = mapped_column(
        String,
        nullable=False,
        server_default="",
    )
    preview_isolation_class: Mapped[str] = mapped_column(
        String,
        nullable=False,
        server_default="",
    )
    self_review: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    occurred_at: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[PayloadDict] = mapped_column(PayloadJsonType, nullable=False)


class LaunchplaneOwnerControlChannelSessionRow(Base):
    __tablename__ = "launchplane_owner_control_channel_sessions"
    __table_args__ = (
        CheckConstraint(
            "status IN ('enrolled', 'revoked')",
            name="launchplane_owner_control_session_status_ck",
        ),
        CheckConstraint(
            "(status = 'enrolled' AND revoked_at IS NULL) OR "
            "(status = 'revoked' AND revoked_at IS NOT NULL)",
            name="launchplane_owner_control_session_revocation_ck",
        ),
        CheckConstraint(
            "authority_state = 'inert'",
            name="launchplane_owner_control_session_authority_ck",
        ),
        UniqueConstraint(
            "owner_github_id",
            "binding_sha256",
            name="launchplane_owner_control_session_owner_binding_uq",
        ),
        Index(
            "launchplane_owner_control_session_status_idx",
            "status",
            "session_expires_at",
        ),
    )

    channel_session_id: Mapped[str] = mapped_column(String, primary_key=True)
    owner_github_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    session_issued_at: Mapped[str] = mapped_column(String, nullable=False)
    session_expires_at: Mapped[str] = mapped_column(String, nullable=False)
    binding_sha256: Mapped[str] = mapped_column(String, nullable=False)
    enrolled_at: Mapped[str] = mapped_column(String, nullable=False)
    revoked_at: Mapped[str | None] = mapped_column(String, nullable=True)
    authority_state: Mapped[str] = mapped_column(
        String,
        nullable=False,
        server_default="inert",
    )
    payload: Mapped[PayloadDict] = mapped_column(PayloadJsonType, nullable=False)


class LaunchplaneOwnerControlEnrollmentProvenanceRow(Base):
    __tablename__ = "launchplane_owner_control_enrollment_provenance"
    __table_args__ = (
        CheckConstraint(
            "enrollment_context = 'postgres_record_store'",
            name="launchplane_owner_control_provenance_context_ck",
        ),
        CheckConstraint(
            "server_observed_corroboration = 'none'",
            name="launchplane_owner_control_provenance_corroboration_ck",
        ),
        CheckConstraint(
            "provenance_tier = 'self_asserted'",
            name="launchplane_owner_control_provenance_tier_ck",
        ),
        CheckConstraint(
            "authority_state = 'inert'",
            name="launchplane_owner_control_provenance_authority_ck",
        ),
        CheckConstraint(
            "authorizes_execution = false",
            name="launchplane_owner_control_provenance_authorization_ck",
        ),
        UniqueConstraint(
            "owner_github_id",
            "binding_sha256",
            "host_principal_claim_sha256",
            name="launchplane_owner_control_provenance_binding_claim_uq",
        ),
    )

    channel_session_id: Mapped[str] = mapped_column(
        String,
        ForeignKey(
            "launchplane_owner_control_channel_sessions.channel_session_id",
            ondelete="RESTRICT",
        ),
        primary_key=True,
    )
    owner_github_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    binding_sha256: Mapped[str] = mapped_column(String, nullable=False)
    host_principal_claim_sha256: Mapped[str] = mapped_column(String, nullable=False)
    enrolled_at: Mapped[str] = mapped_column(String, nullable=False)
    enrollment_context: Mapped[str] = mapped_column(
        String,
        nullable=False,
        server_default="postgres_record_store",
    )
    server_observed_corroboration: Mapped[str] = mapped_column(
        String,
        nullable=False,
        server_default="none",
    )
    provenance_tier: Mapped[str] = mapped_column(
        String,
        nullable=False,
        server_default="self_asserted",
    )
    authority_state: Mapped[str] = mapped_column(
        String,
        nullable=False,
        server_default="inert",
    )
    authorizes_execution: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=false(),
    )
    payload: Mapped[PayloadDict] = mapped_column(PayloadJsonType, nullable=False)


class LaunchplaneAdministratorEnrollmentRow(Base):
    __tablename__ = "launchplane_administrator_enrollments"
    __table_args__ = (
        CheckConstraint(
            "state IN ('issued', 'control_proven', 'withdrawn', 'expired', 'enrolled')",
            name="launchplane_administrator_enrollment_state_ck",
        ),
        CheckConstraint(
            "proposer_github_id > 0 AND (candidate_github_id IS NULL OR candidate_github_id > 0)",
            name="launchplane_administrator_enrollment_github_id_ck",
        ),
        CheckConstraint(
            "expires_at > created_at",
            name="launchplane_administrator_enrollment_expiry_ck",
        ),
        CheckConstraint(
            "strftime('%s', created_at) IS NOT NULL "
            "AND strftime('%s', expires_at) IS NOT NULL "
            "AND CAST(strftime('%s', expires_at) AS INTEGER) "
            "- CAST(strftime('%s', created_at) AS INTEGER) = 1800",
            name="lp_admin_enrollment_ttl_sqlite_ck",
        ).ddl_if(dialect="sqlite"),
        CheckConstraint(
            "CAST(expires_at AS timestamptz) - CAST(created_at AS timestamptz) "
            "= INTERVAL '30 minutes'",
            name="lp_admin_enrollment_ttl_pg_ck",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "candidate_github_id IS NULL OR candidate_github_id <> proposer_github_id",
            name="launchplane_administrator_enrollment_distinct_human_ck",
        ),
        CheckConstraint(
            "authority_state = 'inert' AND authorizes_policy = false",
            name="launchplane_administrator_enrollment_no_authority_ck",
        ),
        CheckConstraint(
            "policy_bridge_state IN ('not_applied', 'applied')",
            name="launchplane_administrator_enrollment_bridge_state_ck",
        ),
        CheckConstraint(
            "(candidate_github_id IS NULL AND control_proven_at IS NULL) OR "
            "(candidate_github_id IS NOT NULL AND control_proven_at IS NOT NULL)",
            name="launchplane_administrator_enrollment_control_proof_ck",
        ),
        CheckConstraint(
            "control_proven_at IS NULL OR "
            "(control_proven_at >= created_at AND control_proven_at < expires_at)",
            name="launchplane_administrator_enrollment_control_time_ck",
        ),
        CheckConstraint(
            "withdrawn_at IS NULL OR "
            "(withdrawn_at >= COALESCE(control_proven_at, created_at) "
            "AND withdrawn_at < expires_at)",
            name="launchplane_administrator_enrollment_withdrawal_time_ck",
        ),
        CheckConstraint(
            "expired_at IS NULL OR expired_at >= expires_at",
            name="launchplane_administrator_enrollment_expired_time_ck",
        ),
        CheckConstraint(
            "enrolled_at IS NULL OR "
            "(control_proven_at IS NOT NULL AND enrolled_at >= control_proven_at "
            "AND enrolled_at < expires_at)",
            name="launchplane_administrator_enrollment_enrolled_time_ck",
        ),
        CheckConstraint(
            "(enrolled_policy_record_id IS NULL AND enrolled_policy_revision IS NULL "
            "AND enrolled_policy_sha256 IS NULL AND reviewed_plan_sha256 IS NULL "
            "AND bridge_idempotency_key_sha256 IS NULL) OR "
            "(enrolled_policy_record_id IS NOT NULL AND enrolled_policy_record_id <> '' "
            "AND enrolled_policy_revision > 0 AND enrolled_policy_sha256 IS NOT NULL "
            "AND reviewed_plan_sha256 IS NOT NULL "
            "AND bridge_idempotency_key_sha256 IS NOT NULL)",
            name="launchplane_administrator_enrollment_policy_evidence_ck",
        ),
        CheckConstraint(
            "state <> 'enrolled' OR enrolled_policy_record_id = "
            "'launchplane-authz-policy-r' || printf('%020d', enrolled_policy_revision) "
            "|| '-' || substr(enrolled_policy_sha256, 1, 12)",
            name="lp_admin_enrollment_policy_record_sqlite_ck",
        ).ddl_if(dialect="sqlite"),
        CheckConstraint(
            "state <> 'enrolled' OR enrolled_policy_record_id = "
            "'launchplane-authz-policy-r' "
            "|| lpad(CAST(enrolled_policy_revision AS text), 20, '0') "
            "|| '-' || substr(enrolled_policy_sha256, 1, 12)",
            name="lp_admin_enrollment_policy_record_pg_ck",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "(state = 'issued' AND candidate_github_id IS NULL AND control_proven_at IS NULL "
            "AND withdrawn_at IS NULL AND expired_at IS NULL AND enrolled_at IS NULL "
            "AND enrolled_policy_record_id IS NULL AND policy_bridge_state = 'not_applied') OR "
            "(state = 'control_proven' AND candidate_github_id IS NOT NULL "
            "AND control_proven_at IS NOT NULL AND withdrawn_at IS NULL "
            "AND expired_at IS NULL AND enrolled_at IS NULL "
            "AND enrolled_policy_record_id IS NULL AND policy_bridge_state = 'not_applied') OR "
            "(state = 'withdrawn' AND withdrawn_at IS NOT NULL AND expired_at IS NULL "
            "AND enrolled_at IS NULL AND enrolled_policy_record_id IS NULL "
            "AND policy_bridge_state = 'not_applied') OR "
            "(state = 'expired' AND expired_at IS NOT NULL AND withdrawn_at IS NULL "
            "AND enrolled_at IS NULL AND enrolled_policy_record_id IS NULL "
            "AND policy_bridge_state = 'not_applied') OR "
            "(state = 'enrolled' AND candidate_github_id IS NOT NULL "
            "AND control_proven_at IS NOT NULL AND enrolled_at IS NOT NULL "
            "AND withdrawn_at IS NULL AND expired_at IS NULL "
            "AND enrolled_policy_record_id IS NOT NULL AND policy_bridge_state = 'applied')",
            name="launchplane_administrator_enrollment_terminal_ck",
        ),
        UniqueConstraint(
            "challenge_sha256", name="launchplane_administrator_enrollment_challenge_uq"
        ),
        Index("launchplane_administrator_enrollment_state_expiry_idx", "state", "expires_at"),
    )

    enrollment_id: Mapped[str] = mapped_column(String, primary_key=True)
    state: Mapped[str] = mapped_column(String, nullable=False)
    proposer_github_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    candidate_github_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    challenge_sha256: Mapped[str] = mapped_column(String, nullable=False)
    reason: Mapped[str] = mapped_column(String, nullable=False)
    provenance_sha256: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    expires_at: Mapped[str] = mapped_column(String, nullable=False)
    control_proven_at: Mapped[str | None] = mapped_column(String, nullable=True)
    withdrawn_at: Mapped[str | None] = mapped_column(String, nullable=True)
    expired_at: Mapped[str | None] = mapped_column(String, nullable=True)
    enrolled_at: Mapped[str | None] = mapped_column(String, nullable=True)
    enrolled_policy_record_id: Mapped[str | None] = mapped_column(String, nullable=True)
    enrolled_policy_revision: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    enrolled_policy_sha256: Mapped[str | None] = mapped_column(String, nullable=True)
    reviewed_plan_sha256: Mapped[str | None] = mapped_column(String, nullable=True)
    bridge_idempotency_key_sha256: Mapped[str | None] = mapped_column(String, nullable=True)
    authority_state: Mapped[str] = mapped_column(String, nullable=False, server_default="inert")
    authorizes_policy: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    policy_bridge_state: Mapped[str] = mapped_column(
        String, nullable=False, server_default="not_applied"
    )
    payload: Mapped[PayloadDict] = mapped_column(PayloadJsonType, nullable=False)


class LaunchplaneSoloAdministrationConfirmationRow(Base):
    __tablename__ = "launchplane_solo_administration_confirmations"
    __table_args__ = (
        CheckConstraint(
            "state IN ('issued', 'consumed', 'revoked', 'expired')",
            name="launchplane_solo_admin_confirmation_state_ck",
        ),
        CheckConstraint(
            "active_policy_revision > 0 AND github_id > 0",
            name="launchplane_solo_admin_confirmation_positive_ids_ck",
        ),
        CheckConstraint(
            "candidate_administrator_quorum = 1 AND "
            "candidate_distinct_human_administrator_count = 1",
            name="launchplane_solo_admin_confirmation_solo_quorum_ck",
        ),
        CheckConstraint(
            "authority_state = 'inert' AND authorizes_policy = false",
            name="launchplane_solo_admin_confirmation_no_authority_ck",
        ),
        CheckConstraint(
            "expires_at > created_at",
            name="launchplane_solo_admin_confirmation_expiry_ck",
        ),
        CheckConstraint(
            "strftime('%s', expires_at) IS NOT NULL "
            "AND strftime('%s', created_at) IS NOT NULL "
            "AND CAST(strftime('%s', expires_at) AS INTEGER) "
            "- CAST(strftime('%s', created_at) AS INTEGER) = 300",
            name="lp_solo_admin_confirmation_ttl_sqlite_ck",
        ).ddl_if(dialect="sqlite"),
        CheckConstraint(
            "CAST(expires_at AS timestamptz) - CAST(created_at AS timestamptz) "
            "= INTERVAL '5 minutes'",
            name="lp_solo_admin_confirmation_ttl_pg_ck",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "(state = 'issued' AND terminal_at IS NULL) OR "
            "(state IN ('consumed', 'revoked') AND terminal_at >= created_at "
            "AND terminal_at < expires_at) OR "
            "(state = 'expired' AND terminal_at >= expires_at)",
            name="launchplane_solo_admin_confirmation_terminal_ck",
        ),
        Index(
            "launchplane_solo_administration_confirmation_state_expiry_idx",
            "state",
            "expires_at",
        ),
        Index(
            "launchplane_solo_administration_confirmation_session_idx",
            "human_session_id_sha256",
            "created_at",
        ),
        Index(
            "lp_solo_admin_confirmation_consumed_recovery_idx",
            "candidate_policy_sha256",
            "github_id",
            "idempotency_scope_sha256",
            "state",
        ),
        Index(
            "launchplane_solo_administration_confirmation_issued_binding_uq",
            "reviewed_plan_sha256",
            "human_session_id_sha256",
            "idempotency_scope_sha256",
            "idempotency_key_sha256",
            unique=True,
            postgresql_where=text("state = 'issued'"),
            sqlite_where=text("state = 'issued'"),
        ),
    )

    confirmation_id: Mapped[str] = mapped_column(String, primary_key=True)
    state: Mapped[str] = mapped_column(String, nullable=False)
    active_policy_record_id: Mapped[str] = mapped_column(String, nullable=False)
    active_policy_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    active_policy_sha256: Mapped[str] = mapped_column(String, nullable=False)
    candidate_policy_sha256: Mapped[str] = mapped_column(String, nullable=False)
    candidate_administrator_quorum: Mapped[int] = mapped_column(BigInteger, nullable=False)
    candidate_distinct_human_administrator_count: Mapped[int] = mapped_column(
        BigInteger, nullable=False
    )
    reviewed_plan_sha256: Mapped[str] = mapped_column(String, nullable=False)
    human_session_id_sha256: Mapped[str] = mapped_column(String, nullable=False)
    github_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    idempotency_scope_sha256: Mapped[str] = mapped_column(String, nullable=False)
    idempotency_key_sha256: Mapped[str] = mapped_column(String, nullable=False)
    acknowledgement_sha256: Mapped[str] = mapped_column(String, nullable=False)
    secret_sha256: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    expires_at: Mapped[str] = mapped_column(String, nullable=False)
    terminal_at: Mapped[str | None] = mapped_column(String, nullable=True)
    authority_state: Mapped[str] = mapped_column(String, nullable=False, server_default="inert")
    authorizes_policy: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    payload: Mapped[PayloadDict] = mapped_column(PayloadJsonType, nullable=False)


class LaunchplaneSoloAdministrationConfirmationLifecycleEventRow(Base):
    __tablename__ = "launchplane_solo_administration_confirmation_events"
    __table_args__ = (
        CheckConstraint(
            "event_type IN ('issued', 'consumed', 'revoked', 'expired')",
            name="launchplane_solo_admin_confirmation_event_type_ck",
        ),
        CheckConstraint(
            "(event_type = 'issued' AND from_state = '' AND to_state = 'issued') OR "
            "(event_type IN ('consumed', 'revoked', 'expired') AND from_state = 'issued' "
            "AND to_state = event_type)",
            name="launchplane_solo_admin_confirmation_event_transition_ck",
        ),
        CheckConstraint(
            "authority_state = 'inert' AND authorizes_policy = false",
            name="launchplane_solo_admin_confirmation_event_authority_ck",
        ),
        UniqueConstraint(
            "confirmation_id",
            "event_type",
            name="lp_solo_admin_confirmation_event_transition_uq",
        ),
        Index(
            "lp_solo_admin_confirmation_event_confirmation_idx",
            "confirmation_id",
            "occurred_at",
        ),
    )

    event_id: Mapped[str] = mapped_column(String, primary_key=True)
    confirmation_id: Mapped[str] = mapped_column(String, nullable=False)
    event_type: Mapped[str] = mapped_column(String, nullable=False)
    from_state: Mapped[str] = mapped_column(String, nullable=False)
    to_state: Mapped[str] = mapped_column(String, nullable=False)
    occurred_at: Mapped[str] = mapped_column(String, nullable=False)
    authority_state: Mapped[str] = mapped_column(String, nullable=False, server_default="inert")
    authorizes_policy: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    payload: Mapped[PayloadDict] = mapped_column(PayloadJsonType, nullable=False)


class LaunchplaneOwnerControlIssuedChallengeRow(Base):
    __tablename__ = "launchplane_owner_control_issued_challenges"
    __table_args__ = (
        CheckConstraint(
            "expires_at > issued_at",
            name="launchplane_owner_control_challenge_expiry_ck",
        ),
        CheckConstraint(
            "state IN ('issued', 'consumed', 'expired', 'rejected')",
            name="launchplane_owner_control_challenge_state_ck",
        ),
        CheckConstraint(
            "attempt_count BETWEEN 0 AND 8",
            name="launchplane_owner_control_challenge_attempt_count_ck",
        ),
        CheckConstraint(
            "(state = 'issued' AND consumed_at IS NULL AND terminal_event_id IS NULL) OR "
            "(state = 'consumed' AND consumed_at IS NOT NULL AND terminal_event_id IS NOT NULL) OR "
            "(state IN ('expired', 'rejected') AND consumed_at IS NULL AND terminal_event_id IS NOT NULL)",
            name="launchplane_owner_control_challenge_terminal_ck",
        ),
        CheckConstraint(
            "authority_state = 'inert'",
            name="launchplane_owner_control_challenge_authority_ck",
        ),
        UniqueConstraint(
            "challenge_nonce",
            name="launchplane_owner_control_challenge_nonce_uq",
        ),
        UniqueConstraint(
            "approval_request_sha256",
            name="launchplane_owner_control_challenge_request_digest_uq",
        ),
        Index(
            "launchplane_owner_control_challenge_session_idx",
            "channel_session_id",
            "expires_at",
        ),
        Index(
            "launchplane_owner_control_challenge_state_idx",
            "state",
            "expires_at",
        ),
        Index(
            "launchplane_owner_control_challenge_active_operation_uidx",
            "operation_id",
            unique=True,
            sqlite_where=text("state = 'issued'"),
            postgresql_where=text("state = 'issued'"),
        ),
    )

    challenge_id: Mapped[str] = mapped_column(String, primary_key=True)
    challenge_nonce: Mapped[str] = mapped_column(String, nullable=False)
    channel_session_id: Mapped[str] = mapped_column(String, nullable=False)
    operation_id: Mapped[str] = mapped_column(String, nullable=False)
    descriptor_id: Mapped[str] = mapped_column(String, nullable=False)
    owner_github_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    issued_at: Mapped[str] = mapped_column(String, nullable=False)
    expires_at: Mapped[str] = mapped_column(String, nullable=False)
    approval_request_sha256: Mapped[str] = mapped_column(String, nullable=False)
    binding_sha256: Mapped[str] = mapped_column(String, nullable=False)
    state: Mapped[str] = mapped_column(String, nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False)
    consumed_at: Mapped[str | None] = mapped_column(String, nullable=True)
    terminal_event_id: Mapped[str | None] = mapped_column(String, nullable=True)
    authority_state: Mapped[str] = mapped_column(
        String,
        nullable=False,
        server_default="inert",
    )
    payload: Mapped[PayloadDict] = mapped_column(PayloadJsonType, nullable=False)


class LaunchplaneOwnerControlChallengeLifecycleEventRow(Base):
    __tablename__ = "launchplane_owner_control_challenge_lifecycle_events"
    __table_args__ = (
        CheckConstraint(
            "from_state = 'issued'",
            name="launchplane_owner_control_lifecycle_event_from_state_ck",
        ),
        CheckConstraint(
            "to_state = 'expired'",
            name="launchplane_owner_control_lifecycle_event_to_state_ck",
        ),
        CheckConstraint(
            "transition_reason = 'expired'",
            name="launchplane_owner_control_lifecycle_event_reason_ck",
        ),
        CheckConstraint(
            "occurred_at >= challenge_expires_at",
            name="launchplane_owner_control_lifecycle_event_time_ck",
        ),
        CheckConstraint(
            "authorizes_execution = false",
            name="launchplane_owner_control_lifecycle_event_authorization_ck",
        ),
        CheckConstraint(
            "authority_state = 'inert'",
            name="launchplane_owner_control_lifecycle_event_authority_ck",
        ),
        UniqueConstraint(
            "challenge_id",
            "transition_reason",
            name="launchplane_owner_control_lifecycle_event_transition_uq",
        ),
        Index(
            "launchplane_owner_control_lifecycle_event_challenge_idx",
            "challenge_nonce",
            desc("occurred_at"),
        ),
    )

    event_id: Mapped[str] = mapped_column(String, primary_key=True)
    challenge_id: Mapped[str] = mapped_column(String, nullable=False)
    challenge_nonce: Mapped[str] = mapped_column(String, nullable=False)
    channel_session_id: Mapped[str] = mapped_column(String, nullable=False)
    operation_id: Mapped[str] = mapped_column(String, nullable=False)
    approval_request_sha256: Mapped[str] = mapped_column(String, nullable=False)
    binding_sha256: Mapped[str] = mapped_column(String, nullable=False)
    from_state: Mapped[str] = mapped_column(String, nullable=False)
    to_state: Mapped[str] = mapped_column(String, nullable=False)
    transition_reason: Mapped[str] = mapped_column(String, nullable=False)
    challenge_expires_at: Mapped[str] = mapped_column(String, nullable=False)
    occurred_at: Mapped[str] = mapped_column(String, nullable=False)
    authorizes_execution: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=false(),
    )
    authority_state: Mapped[str] = mapped_column(
        String,
        nullable=False,
        server_default="inert",
    )
    payload: Mapped[PayloadDict] = mapped_column(PayloadJsonType, nullable=False)


class LaunchplaneOwnerControlShadowVerificationEventRow(Base):
    __tablename__ = "launchplane_owner_control_shadow_verification_events"
    __table_args__ = (
        CheckConstraint(
            "verification_status IN ('verified', 'rejected')",
            name="launchplane_owner_control_shadow_event_status_ck",
        ),
        CheckConstraint(
            "verifier_mode = 'shadow'",
            name="launchplane_owner_control_shadow_event_mode_ck",
        ),
        CheckConstraint(
            "authorizes_execution = false",
            name="launchplane_owner_control_shadow_event_authorization_ck",
        ),
        CheckConstraint(
            "authority_state = 'inert'",
            name="launchplane_owner_control_shadow_event_authority_ck",
        ),
        CheckConstraint(
            "sequence BETWEEN 1 AND 8",
            name="launchplane_owner_control_shadow_event_sequence_ck",
        ),
        UniqueConstraint(
            "challenge_id",
            "sequence",
            name="launchplane_owner_control_shadow_event_sequence_uq",
        ),
        Index(
            "launchplane_owner_control_shadow_event_challenge_idx",
            "challenge_nonce",
            desc("occurred_at"),
        ),
        Index(
            "launchplane_owner_control_shadow_event_session_idx",
            "channel_session_id",
            desc("occurred_at"),
        ),
    )

    event_id: Mapped[str] = mapped_column(String, primary_key=True)
    challenge_id: Mapped[str] = mapped_column(String, nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    channel_session_id: Mapped[str] = mapped_column(String, nullable=False)
    challenge_nonce: Mapped[str] = mapped_column(String, nullable=False)
    envelope_sha256: Mapped[str] = mapped_column(String, nullable=False)
    approval_request_sha256: Mapped[str] = mapped_column(String, nullable=False)
    binding_sha256: Mapped[str] = mapped_column(String, nullable=False)
    verification_status: Mapped[str] = mapped_column(String, nullable=False)
    rejection_reason: Mapped[str | None] = mapped_column(String, nullable=True)
    resulting_challenge_state: Mapped[str] = mapped_column(String, nullable=False)
    occurred_at: Mapped[str] = mapped_column(String, nullable=False)
    verifier_mode: Mapped[str] = mapped_column(String, nullable=False, server_default="shadow")
    authorizes_execution: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=false(),
    )
    authority_state: Mapped[str] = mapped_column(
        String,
        nullable=False,
        server_default="inert",
    )
    payload: Mapped[PayloadDict] = mapped_column(PayloadJsonType, nullable=False)


class LaunchplanePrivilegedOperationRow(Base):
    __tablename__ = "launchplane_privileged_operations"
    __table_args__ = (
        Index(
            "launchplane_privileged_operations_status_idx",
            "status",
            desc("created_at"),
        ),
        Index(
            "launchplane_privileged_operations_descriptor_idx",
            "descriptor_id",
            desc("created_at"),
        ),
    )

    operation_id: Mapped[str] = mapped_column(String, primary_key=True)
    descriptor_id: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    requester_github_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)
    expires_at: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[PayloadDict] = mapped_column(PayloadJsonType, nullable=False)


class LaunchplanePrivilegedOperationEventRow(Base):
    __tablename__ = "launchplane_privileged_operation_events"
    __table_args__ = (
        Index(
            "launchplane_privileged_operation_events_operation_sequence_uidx",
            "operation_id",
            "sequence",
            unique=True,
        ),
        Index(
            "launchplane_privileged_operation_events_occurred_idx",
            desc("occurred_at"),
        ),
    )

    event_id: Mapped[str] = mapped_column(String, primary_key=True)
    operation_id: Mapped[str] = mapped_column(String, nullable=False)
    sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    action: Mapped[str] = mapped_column(String, nullable=False)
    occurred_at: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[PayloadDict] = mapped_column(PayloadJsonType, nullable=False)


class LaunchplanePrivilegedOperationWorkerHeartbeatRow(Base):
    __tablename__ = "launchplane_privileged_operation_worker_heartbeats"
    __table_args__ = (
        Index(
            "launchplane_privop_worker_heartbeats_freshness_idx",
            "worker_kind",
            "last_poll_succeeded_at",
        ),
    )

    worker_identity_sha256: Mapped[str] = mapped_column(String, primary_key=True)
    worker_kind: Mapped[str] = mapped_column(String, nullable=False)
    image_reference: Mapped[str] = mapped_column(String, nullable=False)
    last_poll_succeeded_at: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[PayloadDict] = mapped_column(PayloadJsonType, nullable=False)


class LaunchplaneOwnerAcceptanceSubjectSequenceRow(Base):
    __tablename__ = "launchplane_owner_acceptance_subject_sequences"

    repository_id: Mapped[str] = mapped_column(String, primary_key=True)
    pr_number: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    product: Mapped[str] = mapped_column(String, primary_key=True)
    system: Mapped[str] = mapped_column(String, primary_key=True)
    owner_action: Mapped[str] = mapped_column(String, primary_key=True)
    environment: Mapped[str] = mapped_column(String, primary_key=True)
    last_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)


class LaunchplaneTenantRepositoryClassificationRow(Base):
    __tablename__ = "launchplane_tenant_repository_classifications"
    __table_args__ = (
        Index(
            "launchplane_tenant_repo_class_revision_uidx",
            "repository_id",
            "classification_revision",
            unique=True,
        ),
        Index(
            "launchplane_tenant_repo_class_current_idx",
            "repository_id",
            desc("classification_revision"),
        ),
    )

    record_id: Mapped[str] = mapped_column(String, primary_key=True)
    repository_id: Mapped[str] = mapped_column(String, nullable=False)
    repository_owner_id: Mapped[str] = mapped_column(String, nullable=False)
    repository: Mapped[str] = mapped_column(String, nullable=False)
    product: Mapped[str] = mapped_column(String, nullable=False)
    context: Mapped[str] = mapped_column(String, nullable=False)
    classification_kind: Mapped[str] = mapped_column(String, nullable=False)
    classification_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    classified_at: Mapped[str] = mapped_column(String, nullable=False)
    classification_digest: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[PayloadDict] = mapped_column(PayloadJsonType, nullable=False)


class LaunchplaneRepositoryInventoryRow(Base):
    __tablename__ = "launchplane_repository_inventory_records"
    __table_args__ = (
        CheckConstraint(
            "inventory_state IN ('tracked', 'retired')",
            name="launchplane_repository_inventory_state_ck",
        ),
        CheckConstraint(
            "inventory_revision >= 1",
            name="launchplane_repository_inventory_revision_ck",
        ),
        Index(
            "launchplane_repository_inventory_revision_uidx",
            "repository_id",
            "inventory_revision",
            unique=True,
        ),
        Index(
            "launchplane_repository_inventory_current_idx",
            "repository_id",
            desc("inventory_revision"),
        ),
    )

    record_id: Mapped[str] = mapped_column(String, primary_key=True)
    repository_id: Mapped[str] = mapped_column(String, nullable=False)
    repository_owner_id: Mapped[str] = mapped_column(String, nullable=False)
    repository: Mapped[str] = mapped_column(String, nullable=False)
    inventory_state: Mapped[str] = mapped_column(String, nullable=False)
    inventory_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    recorded_at: Mapped[str] = mapped_column(String, nullable=False)
    inventory_digest: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[PayloadDict] = mapped_column(PayloadJsonType, nullable=False)


class LaunchplaneRepositoryHumanRolePolicyRow(Base):
    __tablename__ = "launchplane_repository_human_role_policies"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'superseded')",
            name="launchplane_repo_human_role_status_ck",
        ),
        CheckConstraint(
            "role_policy_revision >= 1",
            name="launchplane_repo_human_role_revision_ck",
        ),
        CheckConstraint(
            "(role_policy_revision = 1 AND supersedes_record_id IS NULL) OR "
            "(role_policy_revision > 1 AND supersedes_record_id IS NOT NULL)",
            name="launchplane_repo_human_role_supersedes_ck",
        ),
        Index(
            "launchplane_repo_human_role_revision_uidx",
            "repository_id",
            "product",
            "context",
            "role_policy_revision",
            unique=True,
        ),
        Index(
            "launchplane_repo_human_role_active_uidx",
            "repository_id",
            "product",
            "context",
            unique=True,
            sqlite_where=text("status = 'active'"),
            postgresql_where=text("status = 'active'"),
        ),
        Index(
            "launchplane_repo_human_role_current_idx",
            "repository_id",
            "product",
            "context",
            "status",
            desc("role_policy_revision"),
        ),
    )

    record_id: Mapped[str] = mapped_column(String, primary_key=True)
    repository_id: Mapped[str] = mapped_column(String, nullable=False)
    repository_owner_id: Mapped[str] = mapped_column(String, nullable=False)
    repository: Mapped[str] = mapped_column(String, nullable=False)
    product: Mapped[str] = mapped_column(String, nullable=False)
    context: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    role_policy_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    effective_at: Mapped[str] = mapped_column(String, nullable=False)
    source: Mapped[str] = mapped_column(String, nullable=False)
    supersedes_record_id: Mapped[str | None] = mapped_column(String, nullable=True)
    role_policy_digest: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[PayloadDict] = mapped_column(PayloadJsonType, nullable=False)


class LaunchplaneTenantTechnicalHumanWaiverEventRow(Base):
    __tablename__ = "launchplane_tenant_technical_human_waiver_events"
    __table_args__ = (
        CheckConstraint(
            "action IN ('created', 'revoked')",
            name="launchplane_tenant_human_waiver_action_ck",
        ),
        CheckConstraint(
            "pull_request_number >= 1",
            name="launchplane_tenant_human_waiver_pr_ck",
        ),
        CheckConstraint(
            "classification_revision >= 1",
            name="launchplane_tenant_human_waiver_class_revision_ck",
        ),
        CheckConstraint(
            "role_policy_revision >= 1",
            name="launchplane_tenant_human_waiver_role_revision_ck",
        ),
        CheckConstraint(
            "authz_policy_revision >= 1",
            name="launchplane_tenant_human_waiver_authz_revision_ck",
        ),
        CheckConstraint(
            "author_github_id >= 1",
            name="launchplane_tenant_human_waiver_author_ck",
        ),
        Index(
            "launchplane_tenant_human_waiver_exact_head_idx",
            "repository_id",
            "pull_request_number",
            "head_sha",
            desc("occurred_at"),
            desc("event_id"),
        ),
        Index(
            "launchplane_tenant_human_waiver_binding_idx",
            "binding_sha256",
            desc("occurred_at"),
            desc("event_id"),
        ),
        Index(
            "launchplane_tenant_human_waiver_waiver_idx",
            "waiver_id",
            desc("occurred_at"),
            desc("event_id"),
        ),
        Index(
            "launchplane_tenant_human_waiver_policy_idx",
            "role_policy_record_id",
            "authz_policy_record_id",
            desc("occurred_at"),
        ),
    )

    event_id: Mapped[str] = mapped_column(String, primary_key=True)
    repository_id: Mapped[str] = mapped_column(String, nullable=False)
    repository_owner_id: Mapped[str] = mapped_column(String, nullable=False)
    repository: Mapped[str] = mapped_column(String, nullable=False)
    product: Mapped[str] = mapped_column(String, nullable=False)
    context: Mapped[str] = mapped_column(String, nullable=False)
    waiver_id: Mapped[str] = mapped_column(String, nullable=False)
    binding_sha256: Mapped[str] = mapped_column(String, nullable=False)
    pull_request_number: Mapped[int] = mapped_column(BigInteger, nullable=False)
    head_sha: Mapped[str] = mapped_column(String, nullable=False)
    classification_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    classification_digest: Mapped[str] = mapped_column(String, nullable=False)
    role_policy_record_id: Mapped[str] = mapped_column(String, nullable=False)
    role_policy_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    role_policy_digest: Mapped[str] = mapped_column(String, nullable=False)
    authz_policy_record_id: Mapped[str] = mapped_column(String, nullable=False)
    authz_policy_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    authz_policy_digest: Mapped[str] = mapped_column(String, nullable=False)
    action: Mapped[str] = mapped_column(String, nullable=False)
    author_github_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    author_login: Mapped[str] = mapped_column(String, nullable=False)
    managed_set_id: Mapped[str] = mapped_column(String, nullable=False)
    managed_rule_id: Mapped[str] = mapped_column(String, nullable=False)
    authorized_at: Mapped[str] = mapped_column(String, nullable=False)
    occurred_at: Mapped[str] = mapped_column(String, nullable=False)
    expires_at: Mapped[str] = mapped_column(String, nullable=False)
    source_event_kind: Mapped[str] = mapped_column(String, nullable=False)
    source_event_id: Mapped[str] = mapped_column(String, nullable=False)
    event_digest: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[PayloadDict] = mapped_column(PayloadJsonType, nullable=False)


class LaunchplaneTrustedMaintenancePolicyRow(Base):
    __tablename__ = "launchplane_trusted_maintenance_policies"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'superseded')",
            name="launchplane_trusted_maintenance_policy_status_ck",
        ),
        CheckConstraint(
            "policy_revision >= 1",
            name="launchplane_trusted_maintenance_policy_revision_ck",
        ),
        CheckConstraint(
            "(policy_revision = 1 AND supersedes_record_id IS NULL) OR "
            "(policy_revision > 1 AND supersedes_record_id IS NOT NULL)",
            name="launchplane_trusted_maintenance_policy_supersedes_ck",
        ),
        Index(
            "launchplane_trusted_maintenance_policy_revision_uidx",
            "repository_id",
            "product",
            "context",
            "policy_revision",
            unique=True,
        ),
        Index(
            "launchplane_trusted_maintenance_policy_active_uidx",
            "repository_id",
            "product",
            "context",
            unique=True,
            sqlite_where=text("status = 'active'"),
            postgresql_where=text("status = 'active'"),
        ),
        Index(
            "launchplane_trusted_maintenance_policy_current_idx",
            "repository_id",
            "product",
            "context",
            "status",
            desc("policy_revision"),
        ),
    )

    record_id: Mapped[str] = mapped_column(String, primary_key=True)
    repository_id: Mapped[str] = mapped_column(String, nullable=False)
    repository_owner_id: Mapped[str] = mapped_column(String, nullable=False)
    repository: Mapped[str] = mapped_column(String, nullable=False)
    product: Mapped[str] = mapped_column(String, nullable=False)
    context: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    policy_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    effective_at: Mapped[str] = mapped_column(String, nullable=False)
    source: Mapped[str] = mapped_column(String, nullable=False)
    supersedes_record_id: Mapped[str | None] = mapped_column(String, nullable=True)
    policy_digest: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[PayloadDict] = mapped_column(PayloadJsonType, nullable=False)


class LaunchplaneProductOwnerPolicyRow(Base):
    __tablename__ = "launchplane_product_owner_policies"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'superseded')",
            name="launchplane_product_owner_policy_status_ck",
        ),
        CheckConstraint(
            "policy_revision >= 1",
            name="launchplane_product_owner_policy_revision_ck",
        ),
        CheckConstraint(
            "quorum = 1",
            name="launchplane_product_owner_policy_quorum_ck",
        ),
        CheckConstraint(
            "(policy_revision = 1 AND supersedes_record_id IS NULL) OR "
            "(policy_revision > 1 AND supersedes_record_id IS NOT NULL)",
            name="launchplane_product_owner_policy_supersedes_ck",
        ),
        Index(
            "launchplane_product_owner_policy_revision_uidx",
            "product",
            "system",
            "policy_revision",
            unique=True,
        ),
        Index(
            "launchplane_product_owner_policy_active_uidx",
            "product",
            "system",
            unique=True,
            sqlite_where=text("status = 'active'"),
            postgresql_where=text("status = 'active'"),
        ),
        Index(
            "launchplane_product_owner_policy_current_idx",
            "product",
            "system",
            "status",
            desc("policy_revision"),
        ),
    )

    record_id: Mapped[str] = mapped_column(String, primary_key=True)
    product: Mapped[str] = mapped_column(String, nullable=False)
    system: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    policy_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    quorum: Mapped[int] = mapped_column(BigInteger, nullable=False)
    effective_at: Mapped[str] = mapped_column(String, nullable=False)
    source: Mapped[str] = mapped_column(String, nullable=False)
    supersedes_record_id: Mapped[str | None] = mapped_column(String, nullable=True)
    policy_digest: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[PayloadDict] = mapped_column(PayloadJsonType, nullable=False)


class LaunchplaneProductOwnerRequirementRow(Base):
    __tablename__ = "launchplane_product_owner_requirements"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'superseded')",
            name="launchplane_product_owner_requirement_status_ck",
        ),
        CheckConstraint(
            "requirement_revision >= 1",
            name="launchplane_product_owner_requirement_revision_ck",
        ),
        CheckConstraint(
            "(requirement_revision = 1 AND supersedes_record_id IS NULL) OR "
            "(requirement_revision > 1 AND supersedes_record_id IS NOT NULL)",
            name="launchplane_product_owner_requirement_supersedes_ck",
        ),
        Index(
            "launchplane_product_owner_requirement_revision_uidx",
            "product",
            "system",
            "requirement_revision",
            unique=True,
        ),
        Index(
            "launchplane_product_owner_requirement_active_uidx",
            "product",
            "system",
            unique=True,
            sqlite_where=text("status = 'active'"),
            postgresql_where=text("status = 'active'"),
        ),
        Index(
            "launchplane_product_owner_requirement_current_idx",
            "product",
            "system",
            "status",
            desc("requirement_revision"),
        ),
    )

    record_id: Mapped[str] = mapped_column(String, primary_key=True)
    product: Mapped[str] = mapped_column(String, nullable=False)
    system: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    requirement_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    effective_at: Mapped[str] = mapped_column(String, nullable=False)
    source: Mapped[str] = mapped_column(String, nullable=False)
    supersedes_record_id: Mapped[str | None] = mapped_column(String, nullable=True)
    requirement_digest: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[PayloadDict] = mapped_column(PayloadJsonType, nullable=False)


class LaunchplaneProductOwnerRoutingRow(Base):
    __tablename__ = "launchplane_product_owner_routing"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'superseded')",
            name="launchplane_product_owner_routing_status_ck",
        ),
        CheckConstraint(
            "routing_revision >= 1",
            name="launchplane_product_owner_routing_revision_ck",
        ),
        CheckConstraint(
            "authoritative = false",
            name="launchplane_product_owner_routing_non_authoritative_ck",
        ),
        CheckConstraint(
            "(routing_revision = 1 AND supersedes_record_id IS NULL) OR "
            "(routing_revision > 1 AND supersedes_record_id IS NOT NULL)",
            name="launchplane_product_owner_routing_supersedes_ck",
        ),
        Index(
            "launchplane_product_owner_routing_revision_uidx",
            "product",
            "system",
            "routing_revision",
            unique=True,
        ),
        Index(
            "launchplane_product_owner_routing_active_uidx",
            "product",
            "system",
            unique=True,
            sqlite_where=text("status = 'active'"),
            postgresql_where=text("status = 'active'"),
        ),
        Index(
            "launchplane_product_owner_routing_current_idx",
            "product",
            "system",
            "status",
            desc("routing_revision"),
        ),
    )

    record_id: Mapped[str] = mapped_column(String, primary_key=True)
    product: Mapped[str] = mapped_column(String, nullable=False)
    system: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    routing_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    authoritative: Mapped[bool] = mapped_column(Boolean, nullable=False)
    effective_at: Mapped[str] = mapped_column(String, nullable=False)
    source: Mapped[str] = mapped_column(String, nullable=False)
    supersedes_record_id: Mapped[str | None] = mapped_column(String, nullable=True)
    routing_digest: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[PayloadDict] = mapped_column(PayloadJsonType, nullable=False)


class LaunchplaneTrustedMaintenanceEvidenceRow(Base):
    __tablename__ = "launchplane_trusted_maintenance_evidence"
    __table_args__ = (
        CheckConstraint(
            "pull_request_number >= 1",
            name="launchplane_trusted_maintenance_evidence_pr_ck",
        ),
        CheckConstraint(
            "classification_revision >= 1",
            name="launchplane_trusted_maintenance_evidence_class_revision_ck",
        ),
        CheckConstraint(
            "policy_revision >= 1",
            name="launchplane_trusted_maintenance_evidence_policy_revision_ck",
        ),
        CheckConstraint(
            "pr_author_github_id >= 1",
            name="launchplane_trusted_maintenance_evidence_author_ck",
        ),
        CheckConstraint(
            "sender_github_id >= 1",
            name="launchplane_trusted_maintenance_evidence_sender_ck",
        ),
        CheckConstraint(
            "head_repository_id = repository_id AND "
            "head_repository_owner_id = repository_owner_id AND "
            "head_repository = repository",
            name="launchplane_trusted_maintenance_evidence_same_head_repo_ck",
        ),
        Index(
            "launchplane_trusted_maintenance_exact_head_idx",
            "repository_id",
            "pull_request_number",
            "head_sha",
            desc("occurred_at"),
            desc("evidence_id"),
        ),
        Index(
            "launchplane_trusted_maintenance_binding_idx",
            "binding_sha256",
            desc("occurred_at"),
            desc("evidence_id"),
        ),
        Index(
            "launchplane_trusted_maintenance_policy_idx",
            "policy_record_id",
            "classification_digest",
            desc("occurred_at"),
        ),
        Index(
            "launchplane_trusted_maintenance_actor_event_idx",
            "pr_author_github_id",
            "sender_github_id",
            "event_name",
            "event_action",
            desc("occurred_at"),
        ),
    )

    evidence_id: Mapped[str] = mapped_column(String, primary_key=True)
    repository_id: Mapped[str] = mapped_column(String, nullable=False)
    repository_owner_id: Mapped[str] = mapped_column(String, nullable=False)
    repository: Mapped[str] = mapped_column(String, nullable=False)
    product: Mapped[str] = mapped_column(String, nullable=False)
    context: Mapped[str] = mapped_column(String, nullable=False)
    binding_sha256: Mapped[str] = mapped_column(String, nullable=False)
    pull_request_number: Mapped[int] = mapped_column(BigInteger, nullable=False)
    head_sha: Mapped[str] = mapped_column(String, nullable=False)
    classification_record_id: Mapped[str] = mapped_column(String, nullable=False)
    classification_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    classification_digest: Mapped[str] = mapped_column(String, nullable=False)
    policy_record_id: Mapped[str] = mapped_column(String, nullable=False)
    policy_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    policy_digest: Mapped[str] = mapped_column(String, nullable=False)
    matched_actor_rule_id: Mapped[str] = mapped_column(String, nullable=False)
    pr_author_github_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    pr_author_type: Mapped[str] = mapped_column(String, nullable=False)
    pr_author_login: Mapped[str] = mapped_column(String, nullable=False)
    sender_github_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sender_type: Mapped[str] = mapped_column(String, nullable=False)
    sender_login: Mapped[str] = mapped_column(String, nullable=False)
    head_repository_id: Mapped[str] = mapped_column(String, nullable=False)
    head_repository_owner_id: Mapped[str] = mapped_column(String, nullable=False)
    head_repository: Mapped[str] = mapped_column(String, nullable=False)
    event_name: Mapped[str] = mapped_column(String, nullable=False)
    event_action: Mapped[str] = mapped_column(String, nullable=False)
    source: Mapped[str] = mapped_column(String, nullable=False)
    delivery_id: Mapped[str] = mapped_column(String, nullable=False)
    occurred_at: Mapped[str] = mapped_column(String, nullable=False)
    expires_at: Mapped[str] = mapped_column(String, nullable=False)
    evidence_digest: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[PayloadDict] = mapped_column(PayloadJsonType, nullable=False)


class LaunchplanePreviewEnablementRow(Base):
    __tablename__ = "launchplane_preview_enablement"
    __table_args__ = (
        Index(
            "launchplane_preview_enablement_context_idx",
            "context",
            desc("updated_at"),
        ),
        Index(
            "launchplane_preview_enablement_anchor_idx",
            "anchor_repo",
            "anchor_pr_number",
            desc("updated_at"),
        ),
    )

    record_id: Mapped[str] = mapped_column(String, primary_key=True)
    context: Mapped[str] = mapped_column(String, nullable=False)
    anchor_repo: Mapped[str] = mapped_column(String, nullable=False)
    anchor_pr_number: Mapped[int] = mapped_column(Integer, nullable=False)
    pr_state: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[PayloadDict] = mapped_column(PayloadJsonType, nullable=False)


class LaunchplanePreviewInventoryScanRow(Base):
    __tablename__ = "launchplane_preview_inventory_scans"
    __table_args__ = (
        Index(
            "launchplane_preview_inventory_scans_context_idx",
            "context",
            desc("scanned_at"),
        ),
    )

    scan_id: Mapped[str] = mapped_column(String, primary_key=True)
    context: Mapped[str] = mapped_column(String, nullable=False)
    scanned_at: Mapped[str] = mapped_column(String, nullable=False)
    source: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    preview_count: Mapped[int] = mapped_column(Integer, nullable=False)
    payload: Mapped[PayloadDict] = mapped_column(PayloadJsonType, nullable=False)


class LaunchplanePreviewDesiredStateRow(Base):
    __tablename__ = "launchplane_preview_desired_states"
    __table_args__ = (
        Index(
            "launchplane_preview_desired_states_context_idx",
            "context",
            desc("discovered_at"),
        ),
    )

    desired_state_id: Mapped[str] = mapped_column(String, primary_key=True)
    product: Mapped[str] = mapped_column(String, nullable=False)
    context: Mapped[str] = mapped_column(String, nullable=False)
    discovered_at: Mapped[str] = mapped_column(String, nullable=False)
    repository: Mapped[str] = mapped_column(String, nullable=False)
    label: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    desired_count: Mapped[int] = mapped_column(Integer, nullable=False)
    payload: Mapped[PayloadDict] = mapped_column(PayloadJsonType, nullable=False)


class LaunchplanePreviewLifecyclePlanRow(Base):
    __tablename__ = "launchplane_preview_lifecycle_plans"
    __table_args__ = (
        Index(
            "launchplane_preview_lifecycle_plans_context_idx",
            "context",
            desc("planned_at"),
        ),
    )

    plan_id: Mapped[str] = mapped_column(String, primary_key=True)
    product: Mapped[str] = mapped_column(String, nullable=False)
    context: Mapped[str] = mapped_column(String, nullable=False)
    planned_at: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    inventory_scan_id: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[PayloadDict] = mapped_column(PayloadJsonType, nullable=False)


class LaunchplanePreviewLifecycleCleanupRow(Base):
    __tablename__ = "launchplane_preview_lifecycle_cleanups"
    __table_args__ = (
        Index(
            "launchplane_preview_lifecycle_cleanups_context_idx",
            "context",
            desc("requested_at"),
        ),
        Index(
            "launchplane_preview_lifecycle_cleanups_plan_idx",
            "plan_id",
            desc("requested_at"),
        ),
    )

    cleanup_id: Mapped[str] = mapped_column(String, primary_key=True)
    product: Mapped[str] = mapped_column(String, nullable=False)
    context: Mapped[str] = mapped_column(String, nullable=False)
    plan_id: Mapped[str] = mapped_column(String, nullable=False)
    requested_at: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[PayloadDict] = mapped_column(PayloadJsonType, nullable=False)


class LaunchplanePreviewPrFeedbackRow(Base):
    __tablename__ = "launchplane_preview_pr_feedback"
    __table_args__ = (
        Index(
            "launchplane_preview_pr_feedback_context_idx",
            "context",
            desc("requested_at"),
        ),
        Index(
            "launchplane_preview_pr_feedback_anchor_idx",
            "anchor_repo",
            "anchor_pr_number",
            desc("requested_at"),
        ),
    )

    feedback_id: Mapped[str] = mapped_column(String, primary_key=True)
    product: Mapped[str] = mapped_column(String, nullable=False)
    context: Mapped[str] = mapped_column(String, nullable=False)
    anchor_repo: Mapped[str] = mapped_column(String, nullable=False)
    anchor_pr_number: Mapped[int] = mapped_column(Integer, nullable=False)
    requested_at: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    delivery_status: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[PayloadDict] = mapped_column(PayloadJsonType, nullable=False)


class LaunchplanePreviewPrFeedbackRemediationRow(Base):
    __tablename__ = "launchplane_preview_pr_feedback_remediations"
    __table_args__ = (
        Index(
            "launchplane_preview_pr_feedback_remediations_target_idx",
            "repository",
            "pull_request_number",
            desc("requested_at"),
        ),
        Index(
            "launchplane_preview_pr_feedback_remediations_idempotency_idx",
            "actor",
            "idempotency_key",
            desc("requested_at"),
        ),
    )

    remediation_id: Mapped[str] = mapped_column(String, primary_key=True)
    product: Mapped[str] = mapped_column(String, nullable=False)
    context: Mapped[str] = mapped_column(String, nullable=False)
    repository: Mapped[str] = mapped_column(String, nullable=False)
    pull_request_number: Mapped[int] = mapped_column(Integer, nullable=False)
    actor: Mapped[str] = mapped_column(String, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String, nullable=False)
    requested_at: Mapped[str] = mapped_column(String, nullable=False)
    mode: Mapped[str] = mapped_column(String, nullable=False)
    outcome: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[PayloadDict] = mapped_column(PayloadJsonType, nullable=False)


class LaunchplaneProductRetirementRow(Base):
    __tablename__ = "launchplane_product_retirements"
    __table_args__ = (
        Index(
            "launchplane_product_retirements_product_idx",
            "product",
            desc("recorded_at"),
        ),
        Index(
            "launchplane_product_retirements_plan_idx",
            "plan_record_id",
            desc("recorded_at"),
        ),
        Index(
            "launchplane_product_retirements_idempotency_idx",
            "idempotency_key",
            desc("recorded_at"),
        ),
        Index(
            "launchplane_product_retirements_plan_idempotency_unique",
            "product",
            "actor",
            "idempotency_key",
            unique=True,
            postgresql_where=text("mode = 'plan'"),
            sqlite_where=text("mode = 'plan'"),
        ),
    )

    record_id: Mapped[str] = mapped_column(String, primary_key=True)
    plan_record_id: Mapped[str] = mapped_column(String, nullable=False)
    product: Mapped[str] = mapped_column(String, nullable=False)
    context: Mapped[str] = mapped_column(String, nullable=False)
    instance: Mapped[str] = mapped_column(String, nullable=False)
    actor: Mapped[str] = mapped_column(String, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String, nullable=False)
    mode: Mapped[str] = mapped_column(String, nullable=False)
    outcome: Mapped[str] = mapped_column(String, nullable=False)
    recorded_at: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[PayloadDict] = mapped_column(PayloadJsonType, nullable=False)


class LaunchplaneDetachedApplicationRetirementRow(Base):
    __tablename__ = "launchplane_detached_application_retirements"
    __table_args__ = (
        Index(
            "launchplane_detached_app_retirements_candidate_idx",
            "candidate_target_sha256",
            desc("recorded_at"),
        ),
        Index(
            "launchplane_detached_app_retirements_plan_idx",
            "plan_record_id",
            desc("recorded_at"),
        ),
        Index(
            "launchplane_detached_app_retirements_idempotency_idx",
            "idempotency_key",
            desc("recorded_at"),
        ),
        Index(
            "launchplane_detached_app_retirements_plan_idempotency_unique",
            "candidate_target_sha256",
            "actor",
            "idempotency_key",
            unique=True,
            postgresql_where=text("mode = 'plan'"),
            sqlite_where=text("mode = 'plan'"),
        ),
        CheckConstraint(
            "authority_write_count = 0",
            name="launchplane_detached_app_retirements_no_authority_writes",
        ),
        CheckConstraint(
            "protected_target_count > 0",
            name="launchplane_detached_app_retirements_protected_nonempty",
        ),
    )

    record_id: Mapped[str] = mapped_column(String, primary_key=True)
    plan_record_id: Mapped[str] = mapped_column(String, nullable=False)
    candidate_target_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    actor: Mapped[str] = mapped_column(String, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String, nullable=False)
    mode: Mapped[str] = mapped_column(String, nullable=False)
    outcome: Mapped[str] = mapped_column(String, nullable=False)
    recorded_at: Mapped[str] = mapped_column(String, nullable=False)
    protected_target_count: Mapped[int] = mapped_column(Integer, nullable=False)
    authority_write_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    payload: Mapped[PayloadDict] = mapped_column(PayloadJsonType, nullable=False)


class LaunchplanePreviewPrFeedbackNotificationPolicyRow(Base):
    __tablename__ = "launchplane_preview_pr_feedback_notification_policies"
    __table_args__ = (
        Index(
            "launchplane_preview_pr_feedback_notify_policies_scope_idx",
            "product",
            "context",
            "repository",
            "status",
            desc("updated_at"),
        ),
    )

    policy_id: Mapped[str] = mapped_column(String, primary_key=True)
    product: Mapped[str] = mapped_column(String, nullable=False)
    context: Mapped[str] = mapped_column(String, nullable=False)
    repository: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[PayloadDict] = mapped_column(PayloadJsonType, nullable=False)


class LaunchplaneChangeImpactPolicyRow(Base):
    __tablename__ = "launchplane_change_impact_policies"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'superseded')",
            name="launchplane_change_impact_policy_status_ck",
        ),
        CheckConstraint(
            "policy_revision >= 1",
            name="launchplane_change_impact_policy_revision_ck",
        ),
        CheckConstraint(
            "default_unknown_review_tier = 'sensitive'",
            name="launchplane_change_impact_policy_unknown_tier_ck",
        ),
        CheckConstraint(
            "(policy_revision = 1 AND supersedes_record_id IS NULL) OR "
            "(policy_revision > 1 AND supersedes_record_id IS NOT NULL)",
            name="launchplane_change_impact_policy_supersedes_ck",
        ),
        Index(
            "launchplane_change_impact_policy_revision_uidx",
            "repository_id",
            "policy_revision",
            unique=True,
        ),
        Index(
            "launchplane_change_impact_policy_active_uidx",
            "repository_id",
            unique=True,
            sqlite_where=text("status = 'active'"),
            postgresql_where=text("status = 'active'"),
        ),
        Index(
            "launchplane_change_impact_policy_current_idx",
            "repository_id",
            "status",
            desc("policy_revision"),
        ),
    )

    record_id: Mapped[str] = mapped_column(String, primary_key=True)
    repository_id: Mapped[str] = mapped_column(String, nullable=False)
    repository_owner_id: Mapped[str] = mapped_column(String, nullable=False)
    repository: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    policy_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    default_unknown_review_tier: Mapped[str] = mapped_column(String, nullable=False)
    effective_at: Mapped[str] = mapped_column(String, nullable=False)
    source: Mapped[str] = mapped_column(String, nullable=False)
    supersedes_record_id: Mapped[str | None] = mapped_column(String, nullable=True)
    policy_digest: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[PayloadDict] = mapped_column(PayloadJsonType, nullable=False)
    audit_payload: Mapped[PayloadDict | None] = mapped_column(PayloadJsonType, nullable=True)


class LaunchplanePreviewPrFeedbackNotificationAttemptRow(Base):
    __tablename__ = "launchplane_preview_pr_feedback_notification_attempts"
    __table_args__ = (
        Index(
            "launchplane_preview_pr_feedback_notify_attempts_feedback_idx",
            "feedback_id",
            "event",
            desc("attempted_at"),
        ),
        Index(
            "launchplane_preview_pr_feedback_notify_attempts_destination_idx",
            "destination_kind",
            desc("attempted_at"),
        ),
    )

    attempt_id: Mapped[str] = mapped_column(String, primary_key=True)
    feedback_id: Mapped[str] = mapped_column(String, nullable=False)
    event: Mapped[str] = mapped_column(String, nullable=False)
    destination_kind: Mapped[str] = mapped_column(String, nullable=False)
    delivery_status: Mapped[str] = mapped_column(String, nullable=False)
    attempted_at: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[PayloadDict] = mapped_column(PayloadJsonType, nullable=False)


class LaunchplaneRunnerHostHygieneAuditRow(Base):
    __tablename__ = "launchplane_runner_host_hygiene_audits"
    __table_args__ = (
        Index(
            "launchplane_runner_host_hygiene_audits_host_idx",
            "host_name",
            desc("audit_record_key"),
        ),
        Index(
            "launchplane_runner_host_hygiene_audits_status_idx",
            "status",
            desc("audit_record_key"),
        ),
    )

    audit_record_key: Mapped[str] = mapped_column(String, primary_key=True)
    host_name: Mapped[str] = mapped_column(String, nullable=False)
    action: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    mutate: Mapped[int] = mapped_column(Integer, nullable=False)
    payload: Mapped[PayloadDict] = mapped_column(PayloadJsonType, nullable=False)


class LaunchplaneRunnerLaneRegistrationAuditRow(Base):
    __tablename__ = "launchplane_runner_lane_registration_audits"
    __table_args__ = (
        Index(
            "launchplane_runner_lane_registration_audits_repo_idx",
            "repository",
            desc("audit_record_key"),
        ),
        Index(
            "launchplane_runner_lane_registration_audits_host_idx",
            "host_name",
            desc("audit_record_key"),
        ),
        Index(
            "launchplane_runner_lane_registration_audits_status_idx",
            "status",
            desc("audit_record_key"),
        ),
    )

    audit_record_key: Mapped[str] = mapped_column(String, primary_key=True)
    repository: Mapped[str] = mapped_column(String, nullable=False)
    host_name: Mapped[str] = mapped_column(String, nullable=False)
    lane_name: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    mutate: Mapped[int] = mapped_column(Integer, nullable=False)
    payload: Mapped[PayloadDict] = mapped_column(PayloadJsonType, nullable=False)


class LaunchplaneReleaseTupleRow(Base):
    __tablename__ = "launchplane_release_tuples"
    __table_args__ = (Index("launchplane_release_tuples_minted_idx", desc("minted_at")),)

    context: Mapped[str] = mapped_column(String, primary_key=True)
    channel: Mapped[str] = mapped_column(String, primary_key=True)
    tuple_id: Mapped[str] = mapped_column(String, nullable=False)
    artifact_id: Mapped[str] = mapped_column(String, nullable=False)
    minted_at: Mapped[str] = mapped_column(String, nullable=False)
    provenance: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[PayloadDict] = mapped_column(PayloadJsonType, nullable=False)


class LaunchplaneAuthzPolicyRow(Base):
    __tablename__ = "launchplane_authz_policies"
    __table_args__ = (
        Index("launchplane_authz_policies_updated_idx", desc("updated_at")),
        Index(
            "launchplane_authz_policies_revision_uidx",
            "revision",
            unique=True,
        ),
        Index(
            "launchplane_authz_policies_active_uidx",
            "status",
            unique=True,
            sqlite_where=text("status = 'active'"),
            postgresql_where=text("status = 'active'"),
        ),
    )

    record_id: Mapped[str] = mapped_column(String, primary_key=True)
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    source: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)
    policy_sha256: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[PayloadDict] = mapped_column(PayloadJsonType, nullable=False)


class LaunchplaneAuthzDenialRow(Base):
    __tablename__ = "launchplane_authz_denials"
    __table_args__ = (
        Index("launchplane_authz_denials_recorded_idx", desc("recorded_at")),
        Index("launchplane_authz_denials_expires_idx", "expires_at"),
    )

    trace_id: Mapped[str] = mapped_column(String, primary_key=True)
    recorded_at: Mapped[str] = mapped_column(String, nullable=False)
    expires_at: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[PayloadDict] = mapped_column(PayloadJsonType, nullable=False)


class LaunchplaneRuntimeKeySafetyPolicyRow(Base):
    __tablename__ = "launchplane_runtime_key_safety_policies"
    __table_args__ = (
        Index("launchplane_runtime_key_safety_policies_updated_idx", desc("updated_at")),
    )

    record_id: Mapped[str] = mapped_column(String, primary_key=True)
    status: Mapped[str] = mapped_column(String, nullable=False)
    source: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[PayloadDict] = mapped_column(PayloadJsonType, nullable=False)


class LaunchplaneProductProfileRow(Base):
    __tablename__ = "launchplane_product_profiles"
    __table_args__ = (
        Index("launchplane_product_profiles_driver_idx", "driver_id", desc("updated_at")),
    )

    product: Mapped[str] = mapped_column(String, primary_key=True)
    display_name: Mapped[str] = mapped_column(String, nullable=False)
    repository: Mapped[str] = mapped_column(String, nullable=False)
    driver_id: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[PayloadDict] = mapped_column(PayloadJsonType, nullable=False)


class LaunchplanePublicIngressObservationRow(Base):
    __tablename__ = "launchplane_public_ingress_observations"
    __table_args__ = (
        Index(
            "launchplane_public_ingress_observations_lookup_idx",
            "product",
            "context",
            "instance",
            desc("observed_at"),
        ),
        Index(
            "launchplane_public_ingress_observations_status_idx",
            "status",
            desc("observed_at"),
        ),
        Index(
            "launchplane_public_ingress_observations_incident_idx",
            "incident_id",
            desc("observed_at"),
        ),
        Index(
            "launchplane_public_ingress_observations_check_idx",
            "product",
            "context",
            "instance",
            "check_token",
            "check_kind",
            desc("observed_at"),
        ),
    )

    record_id: Mapped[str] = mapped_column(String, primary_key=True)
    product: Mapped[str] = mapped_column(String, nullable=False)
    context: Mapped[str] = mapped_column(String, nullable=False)
    instance: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    observed_at: Mapped[str] = mapped_column(String, nullable=False)
    incident_id: Mapped[str] = mapped_column(String, nullable=False, default="")
    check_token: Mapped[str] = mapped_column(String, nullable=False, default="")
    check_kind: Mapped[str] = mapped_column(String, nullable=False, default="public_http")
    payload: Mapped[PayloadDict] = mapped_column(PayloadJsonType, nullable=False)


class LaunchplaneIngressRouteAuditRow(Base):
    __tablename__ = "launchplane_ingress_route_audits"
    __table_args__ = (
        Index(
            "launchplane_ingress_route_audits_lookup_idx",
            "product",
            "context",
            desc("recorded_at"),
        ),
        Index(
            "launchplane_ingress_route_audits_status_idx",
            "status",
            desc("recorded_at"),
        ),
    )

    record_id: Mapped[str] = mapped_column(String, primary_key=True)
    product: Mapped[str] = mapped_column(String, nullable=False)
    context: Mapped[str] = mapped_column(String, nullable=False)
    mode: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    provider_host_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    recorded_at: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[PayloadDict] = mapped_column(PayloadJsonType, nullable=False)


class LaunchplaneEdgeEndpointRow(Base):
    __tablename__ = "launchplane_edge_endpoints"
    __table_args__ = (
        Index(
            "launchplane_edge_endpoints_provider_status_idx",
            "provider",
            "status",
            "endpoint_key",
        ),
    )

    endpoint_key: Mapped[str] = mapped_column(String, primary_key=True)
    provider: Mapped[str] = mapped_column(String, nullable=False)
    server_name: Mapped[str] = mapped_column(String, nullable=False)
    upstream_host: Mapped[str] = mapped_column(String, nullable=False)
    upstream_scheme: Mapped[str] = mapped_column(String, nullable=False)
    upstream_port: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[PayloadDict] = mapped_column(PayloadJsonType, nullable=False)


class LaunchplanePrivateHealthEndpointRow(Base):
    __tablename__ = "launchplane_private_health_endpoints"
    __table_args__ = (
        Index(
            "launchplane_private_health_endpoints_lookup_idx",
            "product",
            "context",
            "instance",
            "status",
        ),
    )

    endpoint_key: Mapped[str] = mapped_column(String, primary_key=True)
    product: Mapped[str] = mapped_column(String, nullable=False)
    context: Mapped[str] = mapped_column(String, nullable=False)
    instance: Mapped[str] = mapped_column(String, nullable=False)
    url: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[PayloadDict] = mapped_column(PayloadJsonType, nullable=False)


class LaunchplaneRouteBindingRow(Base):
    __tablename__ = "launchplane_route_bindings"
    __table_args__ = (
        Index(
            "launchplane_route_bindings_lookup_idx",
            "product",
            "context",
            "status",
            "instance",
        ),
        Index("launchplane_route_bindings_updated_idx", desc("updated_at")),
    )

    product: Mapped[str] = mapped_column(String, primary_key=True)
    context: Mapped[str] = mapped_column(String, primary_key=True)
    instance: Mapped[str] = mapped_column(String, primary_key=True)
    provider_id: Mapped[str] = mapped_column(String, nullable=False)
    target_category: Mapped[str] = mapped_column(String, nullable=False)
    ingress_provider: Mapped[str] = mapped_column(String, nullable=False)
    ingress_endpoint_key: Mapped[str] = mapped_column(String, nullable=False)
    termination_kind: Mapped[str] = mapped_column(String, nullable=False)
    tls_owner: Mapped[str] = mapped_column(String, nullable=False)
    primary_domain: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    freshness_status: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[PayloadDict] = mapped_column(PayloadJsonType, nullable=False)


class LaunchplaneIngressCanaryRouteRow(Base):
    __tablename__ = "launchplane_ingress_canary_routes"
    __table_args__ = (
        Index(
            "launchplane_ingress_canary_routes_lookup_idx",
            "product",
            "context",
            "status",
            "canary_key",
        ),
    )

    canary_key: Mapped[str] = mapped_column(String, primary_key=True)
    product: Mapped[str] = mapped_column(String, nullable=False)
    context: Mapped[str] = mapped_column(String, nullable=False)
    domain_name: Mapped[str] = mapped_column(String, nullable=False)
    expected_host_id: Mapped[int] = mapped_column(Integer, nullable=False)
    edge_endpoint_key: Mapped[str] = mapped_column(String, nullable=False)
    certificate_id: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[PayloadDict] = mapped_column(PayloadJsonType, nullable=False)


class LaunchplanePublicIngressIncidentRow(Base):
    __tablename__ = "launchplane_public_ingress_incidents"
    __table_args__ = (
        Index(
            "launchplane_public_ingress_incidents_lookup_idx",
            "product",
            "context",
            "instance",
            "status",
            desc("opened_at"),
        ),
        Index(
            "launchplane_public_ingress_incidents_status_idx",
            "status",
            desc("opened_at"),
        ),
        Index(
            "launchplane_public_ingress_incidents_open_uidx",
            "product",
            "context",
            "instance",
            "check_token",
            "check_kind",
            unique=True,
            postgresql_where=text("status = 'open'"),
            sqlite_where=text("status = 'open'"),
        ),
    )

    incident_id: Mapped[str] = mapped_column(String, primary_key=True)
    product: Mapped[str] = mapped_column(String, nullable=False)
    context: Mapped[str] = mapped_column(String, nullable=False)
    instance: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    check_token: Mapped[str] = mapped_column(String, nullable=False)
    check_kind: Mapped[str] = mapped_column(String, nullable=False)
    state_version: Mapped[int] = mapped_column(Integer, nullable=False)
    opened_at: Mapped[str] = mapped_column(String, nullable=False)
    latest_observed_at: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[PayloadDict] = mapped_column(PayloadJsonType, nullable=False)


class LaunchplanePublicIngressIncidentEventRow(Base):
    __tablename__ = "launchplane_public_ingress_incident_events"
    __table_args__ = (
        Index(
            "launchplane_pi_incident_events_incident_idx",
            "incident_id",
            desc("occurred_at"),
        ),
        Index(
            "launchplane_pi_incident_events_kind_idx",
            "event",
            desc("occurred_at"),
        ),
    )

    event_id: Mapped[str] = mapped_column(String, primary_key=True)
    incident_id: Mapped[str] = mapped_column(String, nullable=False)
    event: Mapped[str] = mapped_column(String, nullable=False)
    occurred_at: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[PayloadDict] = mapped_column(PayloadJsonType, nullable=False)


class LaunchplanePublicIngressIncidentReminderRow(Base):
    __tablename__ = "launchplane_public_ingress_incident_reminders"
    __table_args__ = (
        Index(
            "launchplane_pi_incident_reminders_due_idx",
            "status",
            "next_reminder_at",
        ),
        Index(
            "launchplane_pi_incident_reminders_incident_idx",
            "incident_id",
            "policy_id",
        ),
    )

    reminder_state_id: Mapped[str] = mapped_column(String, primary_key=True)
    incident_id: Mapped[str] = mapped_column(String, nullable=False)
    policy_id: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    next_reminder_at: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[PayloadDict] = mapped_column(PayloadJsonType, nullable=False)


class LaunchplanePublicIngressNotificationPolicyRow(Base):
    __tablename__ = "launchplane_public_ingress_notification_policies"
    __table_args__ = (
        Index(
            "launchplane_pi_notify_policies_scope_idx",
            "product",
            "context",
            "instance",
            "status",
            desc("updated_at"),
        ),
    )

    policy_id: Mapped[str] = mapped_column(String, primary_key=True)
    product: Mapped[str] = mapped_column(String, nullable=False)
    context: Mapped[str] = mapped_column(String, nullable=False)
    instance: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[PayloadDict] = mapped_column(PayloadJsonType, nullable=False)


class LaunchplanePublicIngressNotificationAttemptRow(Base):
    __tablename__ = "launchplane_public_ingress_notification_attempts"
    __table_args__ = (
        Index(
            "launchplane_pi_notify_attempts_incident_idx",
            "incident_id",
            "event",
            desc("attempted_at"),
        ),
        Index(
            "launchplane_pi_notify_attempts_destination_idx",
            "destination_kind",
            desc("attempted_at"),
        ),
    )

    attempt_id: Mapped[str] = mapped_column(String, primary_key=True)
    incident_id: Mapped[str] = mapped_column(String, nullable=False)
    event: Mapped[str] = mapped_column(String, nullable=False)
    destination_kind: Mapped[str] = mapped_column(String, nullable=False)
    delivery_status: Mapped[str] = mapped_column(String, nullable=False)
    attempted_at: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[PayloadDict] = mapped_column(PayloadJsonType, nullable=False)


class LaunchplaneEveryCodeNotificationPolicyRow(Base):
    __tablename__ = "launchplane_every_code_notification_policies"
    __table_args__ = (
        Index(
            "launchplane_every_code_notify_policies_scope_idx",
            "repository",
            "status",
            desc("updated_at"),
        ),
    )

    policy_id: Mapped[str] = mapped_column(String, primary_key=True)
    repository: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[PayloadDict] = mapped_column(PayloadJsonType, nullable=False)


class LaunchplaneEveryCodeNotificationAttemptRow(Base):
    __tablename__ = "launchplane_every_code_notification_attempts"
    __table_args__ = (
        Index(
            "launchplane_every_code_notify_attempts_request_idx",
            "request_id",
            "event",
            desc("attempted_at"),
        ),
        Index(
            "launchplane_every_code_notify_attempts_destination_idx",
            "destination_kind",
            desc("attempted_at"),
        ),
    )

    attempt_id: Mapped[str] = mapped_column(String, primary_key=True)
    request_id: Mapped[str] = mapped_column(String, nullable=False)
    event: Mapped[str] = mapped_column(String, nullable=False)
    destination_kind: Mapped[str] = mapped_column(String, nullable=False)
    delivery_status: Mapped[str] = mapped_column(String, nullable=False)
    attempted_at: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[PayloadDict] = mapped_column(PayloadJsonType, nullable=False)


class LaunchplaneDokployTargetIdRow(Base):
    __tablename__ = "launchplane_dokploy_target_ids"
    __table_args__ = (Index("launchplane_dokploy_target_ids_updated_idx", desc("updated_at")),)

    context: Mapped[str] = mapped_column(String, primary_key=True)
    instance: Mapped[str] = mapped_column(String, primary_key=True)
    target_id: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[PayloadDict] = mapped_column(PayloadJsonType, nullable=False)


class LaunchplaneDokployTargetRow(Base):
    __tablename__ = "launchplane_dokploy_targets"
    __table_args__ = (Index("launchplane_dokploy_targets_updated_idx", desc("updated_at")),)

    context: Mapped[str] = mapped_column(String, primary_key=True)
    instance: Mapped[str] = mapped_column(String, primary_key=True)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[PayloadDict] = mapped_column(PayloadJsonType, nullable=False)


class LaunchplaneEngineeringReviewAuthorityRow(Base):
    __tablename__ = "launchplane_engineering_review_authorities"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'retired')",
            name="launchplane_eng_review_authority_status_ck",
        ),
        CheckConstraint(
            "policy_revision >= 1",
            name="launchplane_eng_review_authority_revision_ck",
        ),
        Index(
            "launchplane_eng_review_authority_revision_uidx",
            "repository",
            "policy_revision",
            unique=True,
        ),
        Index(
            "launchplane_eng_review_authority_active_uidx",
            "repository",
            unique=True,
            sqlite_where=text("status = 'active'"),
            postgresql_where=text("status = 'active'"),
        ),
    )

    authority_id: Mapped[str] = mapped_column(String, primary_key=True)
    repository: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    policy_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    authority_digest: Mapped[str] = mapped_column(String, nullable=False)
    recorded_at: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[PayloadDict] = mapped_column(PayloadJsonType, nullable=False)


class LaunchplaneEngineeringReviewRunRow(Base):
    __tablename__ = "launchplane_engineering_review_runs"
    __table_args__ = (
        Index(
            "launchplane_eng_review_runs_pr_idx",
            "repository",
            "pr_number",
            desc("created_at"),
        ),
        Index(
            "launchplane_eng_review_runs_work_request_idx",
            "work_request_id",
            desc("created_at"),
        ),
        Index(
            "launchplane_eng_review_runs_state_lease_idx",
            "state",
            "lease_expires_at",
        ),
        Index(
            "launchplane_eng_review_runs_worker_claim_idx",
            "worker_host",
            "worker_runtime_id",
            "state",
            "created_at",
        ),
        Index(
            "launchplane_eng_review_runs_assignment_uidx",
            "assignment_fingerprint",
            unique=True,
        ),
        Index(
            "launchplane_eng_review_runs_credential_uidx",
            "credential_hash",
            unique=True,
        ),
    )

    run_id: Mapped[str] = mapped_column(String, primary_key=True)
    assignment_fingerprint: Mapped[str] = mapped_column(String, nullable=False)
    review_slot: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(String, nullable=False)
    repository: Mapped[str] = mapped_column(String, nullable=False)
    pr_number: Mapped[int] = mapped_column(Integer, nullable=False)
    head_sha: Mapped[str] = mapped_column(String, nullable=False)
    authority_id: Mapped[str] = mapped_column(String, nullable=False)
    authority_digest: Mapped[str] = mapped_column(String, nullable=False)
    work_request_id: Mapped[str] = mapped_column(String, nullable=False)
    work_request_lifecycle_id: Mapped[str] = mapped_column(String, nullable=False)
    policy_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    worker_runtime_id: Mapped[str] = mapped_column(String, nullable=False)
    worker_host: Mapped[str] = mapped_column(String, nullable=False)
    credential_hash: Mapped[str] = mapped_column(String, nullable=False)
    lease_expires_at: Mapped[str] = mapped_column(String, nullable=False)
    fencing_token: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[PayloadDict] = mapped_column(PayloadJsonType, nullable=False)


class LaunchplaneEngineeringReviewDecisionRow(Base):
    __tablename__ = "launchplane_engineering_review_decisions"
    __table_args__ = (
        Index(
            "launchplane_eng_review_decisions_target_idx",
            "repository",
            "pull_request_number",
            "head_sha",
            desc("evaluated_at"),
        ),
        Index(
            "launchplane_eng_review_decisions_work_request_idx",
            "work_request_id",
            desc("evaluated_at"),
        ),
        Index(
            "launchplane_eng_review_decisions_binding_uidx",
            "decision_binding_sha256",
            unique=True,
        ),
    )

    decision_id: Mapped[str] = mapped_column(String, primary_key=True)
    decision_binding_sha256: Mapped[str] = mapped_column(String, nullable=False)
    repository: Mapped[str] = mapped_column(String, nullable=False)
    pull_request_number: Mapped[int] = mapped_column(Integer, nullable=False)
    head_sha: Mapped[str] = mapped_column(String, nullable=False)
    tree_sha: Mapped[str] = mapped_column(String, nullable=False)
    work_request_id: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    evaluated_at: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[PayloadDict] = mapped_column(PayloadJsonType, nullable=False)


class LaunchplaneProviderTargetRow(Base):
    __tablename__ = "launchplane_provider_targets"
    __table_args__ = (
        Index("launchplane_provider_targets_provider_idx", "provider_id", desc("updated_at")),
        Index("launchplane_provider_targets_updated_idx", desc("updated_at")),
    )

    context: Mapped[str] = mapped_column(String, primary_key=True)
    instance: Mapped[str] = mapped_column(String, primary_key=True)
    provider_id: Mapped[str] = mapped_column(String, nullable=False)
    target_category: Mapped[str] = mapped_column(String, nullable=False)
    target_id: Mapped[str] = mapped_column(String, nullable=False)
    display_name: Mapped[str] = mapped_column(String, nullable=False)
    provider_target_type: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[PayloadDict] = mapped_column(PayloadJsonType, nullable=False)


class LaunchplaneRuntimeEnvironmentRow(Base):
    __tablename__ = "launchplane_runtime_environments"
    __table_args__ = (Index("launchplane_runtime_environments_updated_idx", desc("updated_at")),)

    scope: Mapped[str] = mapped_column(String, primary_key=True)
    context: Mapped[str] = mapped_column(String, primary_key=True)
    instance: Mapped[str] = mapped_column(String, primary_key=True)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[PayloadDict] = mapped_column(PayloadJsonType, nullable=False)


class LaunchplaneRuntimeEnvironmentDeleteEventRow(Base):
    __tablename__ = "launchplane_runtime_environment_delete_events"
    __table_args__ = (
        Index(
            "launchplane_runtime_environment_delete_events_route_idx",
            "scope",
            "context",
            "instance",
            desc("recorded_at"),
        ),
    )

    event_id: Mapped[str] = mapped_column(String, primary_key=True)
    scope: Mapped[str] = mapped_column(String, nullable=False)
    context: Mapped[str] = mapped_column(String, nullable=False)
    instance: Mapped[str] = mapped_column(String, nullable=False)
    recorded_at: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[PayloadDict] = mapped_column(PayloadJsonType, nullable=False)


class LaunchplaneOdooInstanceOverrideRow(Base):
    __tablename__ = "launchplane_odoo_instance_overrides"
    __table_args__ = (Index("launchplane_odoo_instance_overrides_updated_idx", desc("updated_at")),)

    context: Mapped[str] = mapped_column(String, primary_key=True)
    instance: Mapped[str] = mapped_column(String, primary_key=True)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[PayloadDict] = mapped_column(PayloadJsonType, nullable=False)


class LaunchplaneEveryCodeWorkRequestRow(Base):
    __tablename__ = "launchplane_every_code_work_requests"
    __table_args__ = (
        Index(
            "launchplane_every_code_work_requests_state_updated_idx",
            "state",
            desc("updated_at"),
        ),
        Index(
            "launchplane_every_code_work_requests_repo_issue_idx",
            "repository",
            "issue_number",
        ),
        Index(
            "launchplane_every_code_work_requests_lease_idx",
            "state",
            "lease_expires_at",
        ),
    )

    request_id: Mapped[str] = mapped_column(String, primary_key=True)
    source: Mapped[str] = mapped_column(String, nullable=False)
    state: Mapped[str] = mapped_column(String, nullable=False)
    repository: Mapped[str] = mapped_column(String, nullable=False)
    issue_number: Mapped[int] = mapped_column(Integer, nullable=False)
    trigger_label: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)
    claimed_by_host: Mapped[str] = mapped_column(String, nullable=False)
    lease_expires_at: Mapped[str] = mapped_column(String, nullable=False, server_default="")
    fencing_token: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    payload: Mapped[PayloadDict] = mapped_column(PayloadJsonType, nullable=False)


class LaunchplaneEveryCodePrFeedbackRow(Base):
    __tablename__ = "launchplane_every_code_pr_feedback"
    __table_args__ = (
        Index(
            "launchplane_every_code_pr_feedback_request_idx",
            "request_id",
            desc("received_at"),
        ),
        Index(
            "launchplane_every_code_pr_feedback_pr_idx",
            "repository",
            "pr_number",
            desc("received_at"),
        ),
        Index(
            "launchplane_every_code_pr_feedback_status_idx",
            "status",
            desc("received_at"),
        ),
    )

    feedback_id: Mapped[str] = mapped_column(String, primary_key=True)
    request_id: Mapped[str] = mapped_column(String, nullable=False)
    repository: Mapped[str] = mapped_column(String, nullable=False)
    pr_number: Mapped[int] = mapped_column(Integer, nullable=False)
    feedback_kind: Mapped[str] = mapped_column(String, nullable=False)
    github_delivery_id: Mapped[str] = mapped_column(String, nullable=False)
    actor: Mapped[str] = mapped_column(String, nullable=False)
    received_at: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[PayloadDict] = mapped_column(PayloadJsonType, nullable=False)


class LaunchplaneEveryCodePreviewGateRow(Base):
    __tablename__ = "launchplane_every_code_preview_gates"
    __table_args__ = (
        Index(
            "launchplane_every_code_preview_gates_request_idx",
            "request_id",
            desc("updated_at"),
        ),
        Index(
            "launchplane_every_code_preview_gates_pr_idx",
            "repository",
            "pr_number",
            desc("updated_at"),
        ),
        Index(
            "launchplane_every_code_preview_gates_status_idx",
            "status",
            desc("updated_at"),
        ),
    )

    gate_id: Mapped[str] = mapped_column(String, primary_key=True)
    request_id: Mapped[str] = mapped_column(String, nullable=False)
    repository: Mapped[str] = mapped_column(String, nullable=False)
    issue_number: Mapped[int] = mapped_column(Integer, nullable=False)
    pr_number: Mapped[int] = mapped_column(Integer, nullable=False)
    head_sha: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[PayloadDict] = mapped_column(PayloadJsonType, nullable=False)


class LaunchplaneAgentWriteIntentRow(Base):
    __tablename__ = "launchplane_agent_write_intents"
    __table_args__ = (
        Index(
            "launchplane_agent_write_intents_product_context_idx",
            "product",
            "context",
            desc("recorded_at"),
        ),
        Index(
            "launchplane_agent_write_intents_status_idx",
            "status",
            desc("recorded_at"),
        ),
    )

    record_id: Mapped[str] = mapped_column(String, primary_key=True)
    recorded_at: Mapped[str] = mapped_column(String, nullable=False)
    trace_id: Mapped[str] = mapped_column(String, nullable=False)
    intent: Mapped[str] = mapped_column(String, nullable=False)
    mode: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    authz_action: Mapped[str] = mapped_column(String, nullable=False)
    product: Mapped[str] = mapped_column(String, nullable=False)
    context: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[PayloadDict] = mapped_column(PayloadJsonType, nullable=False)


class LaunchplaneMergeTrainRunRow(Base):
    __tablename__ = "launchplane_merge_train_runs"
    __table_args__ = (
        Index(
            "launchplane_merge_train_runs_repository_base_idx",
            "repository",
            "base_branch",
            desc("recorded_at"),
        ),
        Index(
            "launchplane_merge_train_runs_status_idx",
            "status",
            desc("recorded_at"),
        ),
    )

    run_id: Mapped[str] = mapped_column(String, primary_key=True)
    recorded_at: Mapped[str] = mapped_column(String, nullable=False)
    trace_id: Mapped[str] = mapped_column(String, nullable=False)
    repository: Mapped[str] = mapped_column(String, nullable=False)
    base_branch: Mapped[str] = mapped_column(String, nullable=False)
    mode: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    intended_next_action: Mapped[str] = mapped_column(String, nullable=False)
    selected_pr_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    policy_key: Mapped[str] = mapped_column(String, nullable=False)
    policy_sha256: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[PayloadDict] = mapped_column(PayloadJsonType, nullable=False)


class LaunchplaneMergeTrainPolicyRow(Base):
    __tablename__ = "launchplane_merge_train_policies"
    __table_args__ = (
        Index(
            "launchplane_merge_train_policies_active_uidx",
            "status",
            unique=True,
            sqlite_where=text("status = 'active'"),
            postgresql_where=text("status = 'active'"),
        ),
        Index(
            "launchplane_merge_train_policies_status_updated_idx",
            "status",
            desc("updated_at"),
        ),
    )

    record_id: Mapped[str] = mapped_column(String, primary_key=True)
    status: Mapped[str] = mapped_column(String, nullable=False)
    source: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)
    policy_sha256: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[PayloadDict] = mapped_column(PayloadJsonType, nullable=False)


class LaunchplaneMergeTrainPrFeedbackRow(Base):
    __tablename__ = "launchplane_merge_train_pr_feedback"
    __table_args__ = (
        Index(
            "launchplane_merge_train_pr_feedback_pr_idx",
            "repository",
            "base_branch",
            "pull_request_number",
            desc("recorded_at"),
        ),
    )

    feedback_id: Mapped[str] = mapped_column(String, primary_key=True)
    repository: Mapped[str] = mapped_column(String, nullable=False)
    base_branch: Mapped[str] = mapped_column(String, nullable=False)
    pull_request_number: Mapped[int] = mapped_column(Integer, nullable=False)
    event: Mapped[str] = mapped_column(String, nullable=False)
    delivery_status: Mapped[str] = mapped_column(String, nullable=False)
    recorded_at: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[PayloadDict] = mapped_column(PayloadJsonType, nullable=False)


class LaunchplaneMergeTrainBatchCandidateRow(Base):
    __tablename__ = "launchplane_merge_train_batch_candidates"
    __table_args__ = (
        Index(
            "launchplane_merge_train_batch_candidates_repository_base_idx",
            "repository",
            "base_branch",
            desc("updated_at"),
        ),
        Index(
            "launchplane_merge_train_batch_candidates_status_idx",
            "status",
            desc("updated_at"),
        ),
    )

    record_id: Mapped[str] = mapped_column(String, primary_key=True)
    status: Mapped[str] = mapped_column(String, nullable=False)
    source: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)
    repository: Mapped[str] = mapped_column(String, nullable=False)
    base_branch: Mapped[str] = mapped_column(String, nullable=False)
    batch_id: Mapped[str] = mapped_column(String, nullable=False)
    candidate_status: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[PayloadDict] = mapped_column(PayloadJsonType, nullable=False)


class LaunchplaneMergeTrainControllerStateRow(Base):
    __tablename__ = "launchplane_merge_train_controller_states"
    __table_args__ = (
        Index(
            "launchplane_merge_train_controller_states_repository_base_idx",
            "repository",
            "base_branch",
            desc("updated_at"),
        ),
        Index(
            "launchplane_merge_train_controller_states_status_idx",
            "status",
            desc("updated_at"),
        ),
        Index(
            "launchplane_merge_train_controller_states_lease_idx",
            "status",
            "lease_expires_at",
            desc("updated_at"),
        ),
    )

    controller_key: Mapped[str] = mapped_column(String, primary_key=True)
    repository: Mapped[str] = mapped_column(String, nullable=False)
    base_branch: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    policy_key: Mapped[str] = mapped_column(String, nullable=False)
    policy_sha256: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)
    lease_owner: Mapped[str] = mapped_column(String, nullable=False)
    lease_expires_at: Mapped[str] = mapped_column(String, nullable=False)
    active_action: Mapped[str] = mapped_column(String, nullable=False)
    active_phase: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[PayloadDict] = mapped_column(PayloadJsonType, nullable=False)


class LaunchplaneMergeTrainBatchLandingPlanRow(Base):
    __tablename__ = "launchplane_merge_train_batch_landing_plans"
    __table_args__ = (
        Index(
            "launchplane_merge_train_batch_landing_plans_repository_base_idx",
            "repository",
            "base_branch",
            desc("updated_at"),
        ),
        Index(
            "launchplane_merge_train_batch_landing_plans_status_idx",
            "status",
            desc("updated_at"),
        ),
    )

    record_id: Mapped[str] = mapped_column(String, primary_key=True)
    status: Mapped[str] = mapped_column(String, nullable=False)
    source: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)
    repository: Mapped[str] = mapped_column(String, nullable=False)
    base_branch: Mapped[str] = mapped_column(String, nullable=False)
    batch_id: Mapped[str] = mapped_column(String, nullable=False)
    plan_id: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[PayloadDict] = mapped_column(PayloadJsonType, nullable=False)


class LaunchplaneMergeAdmissionRow(Base):
    __tablename__ = "launchplane_merge_admissions"
    __table_args__ = (
        CheckConstraint(
            "pull_request_number > 0 AND queue_position > 0 AND attempt_sequence > 0",
            name="launchplane_merge_admissions_positive_values_check",
        ),
        CheckConstraint(
            "decision = 'admitted'",
            name="launchplane_merge_admissions_decision_check",
        ),
        Index(
            "launchplane_merge_admissions_attempt_uidx",
            "attempt_id",
            unique=True,
        ),
        Index(
            "launchplane_merge_admissions_binding_uidx",
            "admission_binding_sha256",
            unique=True,
        ),
        Index(
            "launchplane_merge_admissions_target_idx",
            "repository",
            "base_branch",
            "pull_request_number",
            desc("created_at"),
        ),
        Index(
            "launchplane_merge_admissions_plan_idx",
            "landing_plan_id",
            "queue_position",
            desc("created_at"),
        ),
    )

    admission_id: Mapped[str] = mapped_column(String, primary_key=True)
    admission_binding_sha256: Mapped[str] = mapped_column(String, nullable=False)
    attempt_id: Mapped[str] = mapped_column(String, nullable=False)
    attempt_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    decision: Mapped[str] = mapped_column(String, nullable=False)
    repository: Mapped[str] = mapped_column(String, nullable=False)
    base_branch: Mapped[str] = mapped_column(String, nullable=False)
    pull_request_number: Mapped[int] = mapped_column(Integer, nullable=False)
    queue_position: Mapped[int] = mapped_column(Integer, nullable=False)
    landing_plan_record_id: Mapped[str] = mapped_column(String, nullable=False)
    landing_plan_id: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[PayloadDict] = mapped_column(PayloadJsonType, nullable=False)


class LaunchplaneMergeLandingOutcomeRow(Base):
    __tablename__ = "launchplane_merge_landing_outcomes"
    __table_args__ = (
        CheckConstraint(
            "pull_request_number > 0 AND observation_sequence > 0",
            name="launchplane_merge_landing_outcomes_positive_values_check",
        ),
        CheckConstraint(
            "status IN ('landed', 'rejected', 'reconcile_required')",
            name="launchplane_merge_landing_outcomes_status_check",
        ),
        Index(
            "launchplane_merge_landing_outcomes_observation_uidx",
            "admission_id",
            "observation_sequence",
            unique=True,
        ),
        Index(
            "launchplane_merge_landing_outcomes_binding_uidx",
            "outcome_binding_sha256",
            unique=True,
        ),
        Index(
            "launchplane_merge_landing_outcomes_target_idx",
            "repository",
            "base_branch",
            "pull_request_number",
            desc("observed_at"),
        ),
        Index(
            "launchplane_merge_landing_outcomes_status_idx",
            "status",
            desc("observed_at"),
        ),
    )

    outcome_id: Mapped[str] = mapped_column(String, primary_key=True)
    outcome_binding_sha256: Mapped[str] = mapped_column(String, nullable=False)
    admission_id: Mapped[str] = mapped_column(String, nullable=False)
    attempt_id: Mapped[str] = mapped_column(String, nullable=False)
    observation_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    repository: Mapped[str] = mapped_column(String, nullable=False)
    base_branch: Mapped[str] = mapped_column(String, nullable=False)
    pull_request_number: Mapped[int] = mapped_column(Integer, nullable=False)
    observed_at: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[PayloadDict] = mapped_column(PayloadJsonType, nullable=False)


class LaunchplaneMergeTrainStackCollapsePlanRow(Base):
    __tablename__ = "launchplane_merge_train_stack_collapse_plans"
    __table_args__ = (
        Index(
            "launchplane_merge_train_stack_collapse_repository_base_idx",
            "repository",
            "base_branch",
            desc("updated_at"),
        ),
        Index(
            "launchplane_merge_train_stack_collapse_plans_status_idx",
            "status",
            desc("updated_at"),
        ),
    )

    record_id: Mapped[str] = mapped_column(String, primary_key=True)
    status: Mapped[str] = mapped_column(String, nullable=False)
    source: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)
    repository: Mapped[str] = mapped_column(String, nullable=False)
    base_branch: Mapped[str] = mapped_column(String, nullable=False)
    collapse_id: Mapped[str] = mapped_column(String, nullable=False)
    root_pull_request_number: Mapped[int] = mapped_column(Integer, nullable=False)
    plan_status: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[PayloadDict] = mapped_column(PayloadJsonType, nullable=False)


class LaunchplaneIdempotencyRow(Base):
    __tablename__ = "launchplane_idempotency_records"
    __table_args__ = (
        Index(
            "launchplane_idempotency_scope_route_key_idx",
            "scope",
            "route_path",
            "idempotency_key",
            unique=True,
        ),
        Index(
            "launchplane_idempotency_state_lease_idx",
            "state",
            "lease_expires_at",
            "updated_at",
        ),
        Index(
            "launchplane_idempotency_active_reconciliation_idx",
            "provider_target_key",
            unique=True,
            postgresql_where=text(
                "provider_target_key <> '' AND state IN ('running', 'reconcile_required')"
            ),
            sqlite_where=text(
                "provider_target_key <> '' AND state IN ('running', 'reconcile_required')"
            ),
        ),
    )

    record_id: Mapped[str] = mapped_column(String, primary_key=True)
    scope: Mapped[str] = mapped_column(String, nullable=False)
    route_path: Mapped[str] = mapped_column(String, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String, nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String, nullable=False)
    state: Mapped[str] = mapped_column(String, nullable=False, server_default="completed")
    lease_owner: Mapped[str] = mapped_column(String, nullable=False, server_default="")
    lease_expires_at: Mapped[str] = mapped_column(String, nullable=False, server_default="")
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    reconciliation_key: Mapped[str] = mapped_column(String, nullable=False, server_default="")
    provider_target_key: Mapped[str] = mapped_column(String, nullable=False, server_default="")
    created_at: Mapped[str] = mapped_column(String, nullable=False, server_default="")
    updated_at: Mapped[str] = mapped_column(String, nullable=False, server_default="")
    response_status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    response_trace_id: Mapped[str] = mapped_column(String, nullable=False)
    recorded_at: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[PayloadDict] = mapped_column(PayloadJsonType, nullable=False)


class LaunchplaneOutboxDeliveryRow(Base):
    __tablename__ = "launchplane_outbox_deliveries"
    __table_args__ = (
        Index("launchplane_outbox_deliveries_dedupe_uidx", "dedupe_key", unique=True),
        Index(
            "launchplane_outbox_deliveries_claim_idx",
            "state",
            "next_attempt_at",
            "lease_expires_at",
            "created_at",
        ),
        Index(
            "launchplane_outbox_deliveries_aggregate_idx",
            "aggregate_type",
            "aggregate_id",
            desc("created_at"),
        ),
        Index(
            "launchplane_outbox_deliveries_provider_key_idx",
            "provider_operation_key",
        ),
    )

    delivery_id: Mapped[str] = mapped_column(String, primary_key=True)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    state: Mapped[str] = mapped_column(String, nullable=False)
    aggregate_type: Mapped[str] = mapped_column(String, nullable=False)
    aggregate_id: Mapped[str] = mapped_column(String, nullable=False)
    dedupe_key: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)
    next_attempt_at: Mapped[str] = mapped_column(String, nullable=False)
    lease_owner: Mapped[str] = mapped_column(String, nullable=False, server_default="")
    lease_expires_at: Mapped[str] = mapped_column(String, nullable=False, server_default="")
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="6")
    provider_operation_key: Mapped[str] = mapped_column(String, nullable=False, server_default="")
    provider_id: Mapped[str] = mapped_column(String, nullable=False, server_default="")
    external_id: Mapped[str] = mapped_column(String, nullable=False, server_default="")
    external_url: Mapped[str] = mapped_column(String, nullable=False, server_default="")
    action: Mapped[str] = mapped_column(String, nullable=False, server_default="")
    error_code: Mapped[str] = mapped_column(String, nullable=False, server_default="")
    payload: Mapped[PayloadDict] = mapped_column(PayloadJsonType, nullable=False)


class LaunchplaneOdooStableBootstrapOperationRow(Base):
    __tablename__ = "launchplane_odoo_stable_bootstrap_operations"
    __table_args__ = (
        Index(
            "launchplane_odoo_bootstrap_operation_lane_status_idx",
            "product",
            "context",
            "instance",
            "status",
            desc("updated_at"),
        ),
        Index(
            "launchplane_odoo_bootstrap_operation_idempotency_idx",
            "idempotency_key",
            desc("updated_at"),
        ),
        Index(
            "launchplane_odoo_bootstrap_active_lane_uidx",
            "product",
            "context",
            "instance",
            unique=True,
            postgresql_where=text("status IN ('pending', 'running')"),
            sqlite_where=text("status IN ('pending', 'running')"),
        ),
        Index(
            "launchplane_odoo_bootstrap_worker_claim_idx",
            "status",
            "lease_expires_at",
            "updated_at",
        ),
    )

    operation_id: Mapped[str] = mapped_column(String, primary_key=True)
    product: Mapped[str] = mapped_column(String, nullable=False)
    context: Mapped[str] = mapped_column(String, nullable=False)
    instance: Mapped[str] = mapped_column(String, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    phase: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)
    lease_owner: Mapped[str] = mapped_column(String, nullable=False, server_default="")
    lease_expires_at: Mapped[str] = mapped_column(String, nullable=False, server_default="")
    heartbeat_at: Mapped[str] = mapped_column(String, nullable=False, server_default="")
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    payload: Mapped[PayloadDict] = mapped_column(PayloadJsonType, nullable=False)


class LaunchplaneOdooStableTargetReplacementOperationRow(Base):
    __tablename__ = "launchplane_odoo_stable_target_replacement_operations"
    __table_args__ = (
        Index(
            "launchplane_odoo_replacement_operation_lane_status_idx",
            "product",
            "context",
            "instance",
            "status",
            desc("updated_at"),
        ),
        Index(
            "launchplane_odoo_replacement_operation_idempotency_idx",
            "idempotency_scope",
            "idempotency_key",
            desc("updated_at"),
        ),
        Index(
            "launchplane_odoo_replacement_active_lane_uidx",
            "product",
            "context",
            "instance",
            unique=True,
            postgresql_where=text("status IN ('pending', 'running')"),
            sqlite_where=text("status IN ('pending', 'running')"),
        ),
        Index(
            "launchplane_odoo_replacement_worker_claim_idx",
            "status",
            "lease_expires_at",
            "updated_at",
        ),
    )

    operation_id: Mapped[str] = mapped_column(String, primary_key=True)
    product: Mapped[str] = mapped_column(String, nullable=False)
    context: Mapped[str] = mapped_column(String, nullable=False)
    instance: Mapped[str] = mapped_column(String, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String, nullable=False)
    idempotency_scope: Mapped[str] = mapped_column(String, nullable=False, default="")
    status: Mapped[str] = mapped_column(String, nullable=False)
    phase: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)
    lease_owner: Mapped[str] = mapped_column(String, nullable=False, server_default="")
    lease_expires_at: Mapped[str] = mapped_column(String, nullable=False, server_default="")
    heartbeat_at: Mapped[str] = mapped_column(String, nullable=False, server_default="")
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    payload: Mapped[PayloadDict] = mapped_column(PayloadJsonType, nullable=False)


class LaunchplaneOdooProdBackupRestoreOperationRow(Base):
    __tablename__ = "launchplane_odoo_prod_backup_restore_operations"
    __table_args__ = (
        Index(
            "launchplane_odoo_restore_operation_lane_status_idx",
            "product",
            "context",
            "instance",
            "status",
            desc("updated_at"),
        ),
        Index(
            "launchplane_odoo_restore_operation_idempotency_idx",
            "idempotency_scope",
            "idempotency_key",
            desc("updated_at"),
        ),
        Index(
            "launchplane_odoo_restore_active_lane_uidx",
            "product",
            "context",
            "instance",
            unique=True,
            postgresql_where=text("status IN ('pending', 'running')"),
            sqlite_where=text("status IN ('pending', 'running')"),
        ),
        Index(
            "launchplane_odoo_restore_worker_claim_idx",
            "status",
            "lease_expires_at",
            "updated_at",
        ),
    )

    operation_id: Mapped[str] = mapped_column(String, primary_key=True)
    product: Mapped[str] = mapped_column(String, nullable=False)
    context: Mapped[str] = mapped_column(String, nullable=False)
    instance: Mapped[str] = mapped_column(String, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String, nullable=False)
    idempotency_scope: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    phase: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)
    lease_owner: Mapped[str] = mapped_column(String, nullable=False, server_default="")
    lease_expires_at: Mapped[str] = mapped_column(String, nullable=False, server_default="")
    heartbeat_at: Mapped[str] = mapped_column(String, nullable=False, server_default="")
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    payload: Mapped[PayloadDict] = mapped_column(PayloadJsonType, nullable=False)


class LaunchplaneOdooProdRetainedVolumeBackupImportOperationRow(Base):
    __tablename__ = "launchplane_odoo_prod_retained_volume_backup_import_operations"
    __table_args__ = (
        Index(
            "launchplane_odoo_retained_import_operation_lane_status_idx",
            "product",
            "context",
            "instance",
            "status",
            desc("updated_at"),
        ),
        Index(
            "launchplane_odoo_retained_import_operation_idempotency_idx",
            "operation_kind",
            "idempotency_scope",
            "idempotency_key",
            desc("updated_at"),
        ),
        Index(
            "launchplane_odoo_retained_import_active_lane_uidx",
            "product",
            "context",
            "instance",
            unique=True,
            postgresql_where=text("status IN ('pending', 'running')"),
            sqlite_where=text("status IN ('pending', 'running')"),
        ),
        Index(
            "launchplane_odoo_retained_import_worker_claim_idx",
            "status",
            "lease_expires_at",
            "updated_at",
        ),
    )

    operation_id: Mapped[str] = mapped_column(String, primary_key=True)
    operation_kind: Mapped[str] = mapped_column(String, nullable=False)
    product: Mapped[str] = mapped_column(String, nullable=False)
    context: Mapped[str] = mapped_column(String, nullable=False)
    instance: Mapped[str] = mapped_column(String, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String, nullable=False)
    idempotency_scope: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    phase: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)
    lease_owner: Mapped[str] = mapped_column(String, nullable=False, server_default="")
    lease_expires_at: Mapped[str] = mapped_column(String, nullable=False, server_default="")
    heartbeat_at: Mapped[str] = mapped_column(String, nullable=False, server_default="")
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    payload: Mapped[PayloadDict] = mapped_column(PayloadJsonType, nullable=False)


class LaunchplaneVeriReelProdBackupGateOperationRow(Base):
    __tablename__ = "launchplane_verireel_prod_backup_gate_operations"
    __table_args__ = (
        Index(
            "launchplane_verireel_backup_gate_operation_lane_status_idx",
            "product",
            "context",
            "instance",
            "status",
            desc("updated_at"),
        ),
        Index(
            "launchplane_verireel_backup_gate_operation_record_idx",
            "backup_record_id",
            desc("updated_at"),
        ),
        Index(
            "launchplane_verireel_backup_gate_active_record_uidx",
            "backup_record_id",
            unique=True,
            postgresql_where=text("status IN ('pending', 'running')"),
            sqlite_where=text("status IN ('pending', 'running')"),
        ),
        Index(
            "launchplane_verireel_backup_gate_worker_claim_idx",
            "status",
            "lease_expires_at",
            "updated_at",
        ),
    )

    operation_id: Mapped[str] = mapped_column(String, primary_key=True)
    product: Mapped[str] = mapped_column(String, nullable=False)
    context: Mapped[str] = mapped_column(String, nullable=False)
    instance: Mapped[str] = mapped_column(String, nullable=False)
    backup_record_id: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    phase: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)
    lease_owner: Mapped[str] = mapped_column(String, nullable=False, server_default="")
    lease_expires_at: Mapped[str] = mapped_column(String, nullable=False, server_default="")
    heartbeat_at: Mapped[str] = mapped_column(String, nullable=False, server_default="")
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    payload: Mapped[PayloadDict] = mapped_column(PayloadJsonType, nullable=False)


class LaunchplaneHumanSessionRow(Base):
    __tablename__ = "launchplane_human_sessions"
    __table_args__ = (
        Index("launchplane_human_sessions_login_idx", "login", desc("created_at")),
        Index("launchplane_human_sessions_expires_idx", desc("expires_at")),
    )

    session_id: Mapped[str] = mapped_column(String, primary_key=True)
    login: Mapped[str] = mapped_column(String, nullable=False)
    github_id: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    expires_at: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[PayloadDict] = mapped_column(PayloadJsonType, nullable=False)


class LaunchplaneSecretRow(Base):
    __tablename__ = "launchplane_secrets"
    __table_args__ = (
        Index(
            "launchplane_secrets_scope_name_idx",
            "scope",
            "integration",
            "name",
            "context",
            "instance",
            unique=True,
        ),
        Index(
            "launchplane_secrets_lookup_idx",
            "integration",
            "context",
            "instance",
            desc("updated_at"),
        ),
    )

    secret_id: Mapped[str] = mapped_column(String, primary_key=True)
    scope: Mapped[str] = mapped_column(String, nullable=False)
    integration: Mapped[str] = mapped_column(String, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    context: Mapped[str] = mapped_column(String, nullable=False)
    instance: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    current_version_id: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[PayloadDict] = mapped_column(PayloadJsonType, nullable=False)


class LaunchplaneSecretVersionRow(Base):
    __tablename__ = "launchplane_secret_versions"
    __table_args__ = (
        Index("launchplane_secret_versions_secret_idx", "secret_id", desc("created_at")),
    )

    version_id: Mapped[str] = mapped_column(String, primary_key=True)
    secret_id: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[PayloadDict] = mapped_column(PayloadJsonType, nullable=False)


class LaunchplaneSecretBindingRow(Base):
    __tablename__ = "launchplane_secret_bindings"
    __table_args__ = (
        Index(
            "launchplane_secret_bindings_lookup_idx",
            "integration",
            "context",
            "instance",
            "binding_key",
            desc("updated_at"),
        ),
    )

    binding_id: Mapped[str] = mapped_column(String, primary_key=True)
    secret_id: Mapped[str] = mapped_column(String, nullable=False)
    integration: Mapped[str] = mapped_column(String, nullable=False)
    binding_key: Mapped[str] = mapped_column(String, nullable=False)
    context: Mapped[str] = mapped_column(String, nullable=False)
    instance: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[PayloadDict] = mapped_column(PayloadJsonType, nullable=False)


class LaunchplaneSecretAuditEventRow(Base):
    __tablename__ = "launchplane_secret_audit_events"
    __table_args__ = (
        Index("launchplane_secret_audit_events_secret_idx", "secret_id", desc("recorded_at")),
    )

    event_id: Mapped[str] = mapped_column(String, primary_key=True)
    secret_id: Mapped[str] = mapped_column(String, nullable=False)
    event_type: Mapped[str] = mapped_column(String, nullable=False)
    recorded_at: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[PayloadDict] = mapped_column(PayloadJsonType, nullable=False)


def _artifact_id_from_model(model: BaseModel) -> str:
    artifact_identity = getattr(model, "artifact_identity", None)
    artifact_id = getattr(artifact_identity, "artifact_id", "")
    return artifact_id if isinstance(artifact_id, str) else ""


def _artifact_id_for_lane(
    *,
    inventory: EnvironmentInventory | None,
    latest_deployment: DeploymentRecord | None,
) -> str:
    if inventory is not None and inventory.artifact_identity is not None:
        inventory_artifact_id = inventory.artifact_identity.artifact_id.strip()
        if inventory_artifact_id:
            return inventory_artifact_id
    if latest_deployment is not None and latest_deployment.artifact_identity is not None:
        return latest_deployment.artifact_identity.artifact_id.strip()
    return ""


def _human_session_payload(session: LaunchplaneHumanSession) -> PayloadDict:
    return {
        "session_id": session.session_id,
        "created_at": session.created_at.isoformat(),
        "expires_at": session.expires_at.isoformat(),
        "csrf_generation": session.csrf_generation,
        "identity": {
            "login": session.identity.login,
            "github_id": session.identity.github_id,
            "name": session.identity.name,
            "email": session.identity.email,
            "organizations": sorted(session.identity.organizations),
            "teams": sorted(session.identity.teams),
            "role": session.identity.role,
        },
    }


def _human_session_from_payload(payload: PayloadDict) -> LaunchplaneHumanSession:
    identity_payload = payload.get("identity")
    if not isinstance(identity_payload, dict):
        raise ValueError("Launchplane human session payload is missing identity.")
    csrf_generation = payload.get("csrf_generation", 0)
    if (
        not isinstance(csrf_generation, int)
        or isinstance(csrf_generation, bool)
        or csrf_generation < 0
    ):
        raise ValueError("Launchplane human session payload has invalid CSRF generation.")
    return LaunchplaneHumanSession(
        session_id=str(payload.get("session_id") or ""),
        created_at=datetime.fromisoformat(str(payload.get("created_at") or "")),
        expires_at=datetime.fromisoformat(str(payload.get("expires_at") or "")),
        csrf_generation=csrf_generation,
        identity=GitHubHumanIdentity(
            login=str(identity_payload.get("login") or ""),
            github_id=int(identity_payload.get("github_id") or 0),
            name=str(identity_payload.get("name") or ""),
            email=str(identity_payload.get("email") or ""),
            organizations=frozenset(
                str(value) for value in identity_payload.get("organizations", [])
            ),
            teams=frozenset(str(value) for value in identity_payload.get("teams", [])),
            role="admin" if identity_payload.get("role") == "admin" else "read_only",
        ),
    )


def _build_engine(
    database_url: str,
    *,
    connection_factory: ConnectionFactory | None = None,
    postgres_connect_timeout_seconds: int | None = None,
    postgres_statement_timeout_milliseconds: int | None = None,
) -> Engine:
    engine_kwargs: dict[str, Any] = {}
    if connection_factory is not None:
        engine_kwargs["creator"] = connection_factory
    connect_args = _engine_connect_args(
        database_url,
        postgres_connect_timeout_seconds=postgres_connect_timeout_seconds,
        postgres_statement_timeout_milliseconds=postgres_statement_timeout_milliseconds,
    )
    if connect_args:
        engine_kwargs["connect_args"] = connect_args
    return create_engine(database_url, **engine_kwargs)


def _engine_connect_args(
    database_url: str,
    *,
    postgres_connect_timeout_seconds: int | None = None,
    postgres_statement_timeout_milliseconds: int | None = None,
) -> dict[str, object]:
    database_url_value = make_url(database_url)
    backend_name = database_url_value.get_backend_name()
    connect_args: dict[str, object] = {}
    if backend_name == "sqlite":
        connect_args["check_same_thread"] = False
    elif backend_name == "postgresql":
        if postgres_connect_timeout_seconds is not None:
            if postgres_connect_timeout_seconds < 1:
                raise ValueError("PostgreSQL connect timeout must be positive.")
            connect_args["connect_timeout"] = postgres_connect_timeout_seconds
        if postgres_statement_timeout_milliseconds is not None:
            if postgres_statement_timeout_milliseconds < 1:
                raise ValueError("PostgreSQL statement timeout must be positive.")
            existing_options = database_url_value.query.get("options", "")
            if isinstance(existing_options, tuple):
                existing_options = " ".join(existing_options)
            statement_timeout_option = (
                f"-c statement_timeout={postgres_statement_timeout_milliseconds}"
            )
            connect_args["options"] = " ".join(
                value
                for value in (str(existing_options).strip(), statement_timeout_option)
                if value
            )
    return connect_args


class PostgresRecordStore(HumanSessionStore):
    def __init__(
        self,
        *,
        database_url: str,
        connection_factory: ConnectionFactory | None = None,
        postgres_connect_timeout_seconds: int | None = None,
        postgres_statement_timeout_milliseconds: int | None = None,
    ) -> None:
        self.database_url = database_url
        self._engine = _build_engine(
            database_url,
            connection_factory=connection_factory,
            postgres_connect_timeout_seconds=postgres_connect_timeout_seconds,
            postgres_statement_timeout_milliseconds=postgres_statement_timeout_milliseconds,
        )
        self._session_factory = sessionmaker(self._engine, expire_on_commit=False)
        lock_engine_kwargs: dict[str, Any] = {"poolclass": NullPool}
        if connection_factory is not None:
            lock_engine_kwargs["creator"] = connection_factory
        elif self._engine.url.get_backend_name() == "postgresql":
            connect_args = _engine_connect_args(
                database_url,
                postgres_connect_timeout_seconds=postgres_connect_timeout_seconds,
                postgres_statement_timeout_milliseconds=(postgres_statement_timeout_milliseconds),
            )
            if connect_args:
                lock_engine_kwargs["connect_args"] = connect_args
        self._owner_acceptance_projection_lock_engine = (
            create_engine(database_url, **lock_engine_kwargs)
            if self._engine.url.get_backend_name() == "postgresql"
            else None
        )

    @property
    def backend_name(self) -> str:
        return "postgres"

    @property
    def database_dialect_name(self) -> str:
        return self._engine.url.get_backend_name()

    def ensure_schema(self) -> None:
        if self._engine.url.get_backend_name() != "sqlite":
            self.verify_schema()
            return
        Base.metadata.create_all(self._engine)

    def verify_schema(self) -> None:
        backend_name = self._engine.url.get_backend_name()
        inspector = inspect(self._engine)
        existing_tables = set(inspector.get_table_names())
        missing_tables = tuple(
            sorted(
                table_name
                for table_name in Base.metadata.tables
                if table_name not in existing_tables
            )
        )
        if missing_tables:
            missing = ", ".join(missing_tables)
            raise RuntimeError(
                "Launchplane shared storage schema is missing required table(s): "
                f"{missing}. Run Alembic migrations before starting the hosted service."
            )
        missing_columns: list[str] = []
        for table_name, table in sorted(Base.metadata.tables.items()):
            existing_columns = {column["name"] for column in inspector.get_columns(table_name)}
            for column_name in sorted(table.columns.keys()):
                if column_name not in existing_columns:
                    missing_columns.append(
                        f"{_string_value(table_name)}.{_string_value(column_name)}"
                    )
        if missing_columns:
            missing = ", ".join(missing_columns)
            raise RuntimeError(
                "Launchplane shared storage schema is missing required column(s): "
                f"{missing}. Run Alembic migrations before starting the hosted service."
            )
        if backend_name == "postgresql":
            verify_postgres_schema_invariants(self._engine)

    def verify_runtime_schema_compatibility(
        self,
        *,
        required_relations: Sequence[str],
    ) -> None:
        if self._engine.url.get_backend_name() != "postgresql":
            raise RuntimeError("Launchplane runtime schema verification requires PostgreSQL.")
        revision = self.schema_revision()
        if revision not in RUNTIME_COMPATIBLE_ALEMBIC_REVISIONS:
            raise RuntimeError("Launchplane database revision is not runtime-compatible.")
        normalized_relations = tuple(
            dict.fromkeys(relation.strip() for relation in required_relations if relation.strip())
        )
        if not normalized_relations:
            raise ValueError("Launchplane runtime schema verification requires relations.")
        values_sql = ", ".join(f"(:relation_{index})" for index in range(len(normalized_relations)))
        parameters = {
            f"relation_{index}": relation for index, relation in enumerate(normalized_relations)
        }
        statement = text(
            "SELECT required.relation_name "
            f"FROM (VALUES {values_sql}) AS required(relation_name) "
            "WHERE to_regclass(required.relation_name) IS NULL "
            "ORDER BY required.relation_name"
        )
        with self._engine.connect() as connection:
            missing_relations = tuple(connection.execute(statement, parameters).scalars().all())
        if missing_relations:
            raise RuntimeError(
                "Launchplane runtime schema is missing required relation(s): "
                + ", ".join(str(relation) for relation in missing_relations)
            )

    def schema_revision(self) -> str:
        if self._engine.url.get_backend_name() != "postgresql":
            return ""
        with self._engine.connect() as connection:
            rows = connection.execute(text("select version_num from alembic_version")).fetchall()
        revisions = tuple(str(row[0]).strip() for row in rows if str(row[0]).strip())
        if len(revisions) != 1:
            raise RuntimeError("Launchplane database must have exactly one Alembic revision.")
        return revisions[0]

    def close(self) -> None:
        if self._owner_acceptance_projection_lock_engine is not None:
            self._owner_acceptance_projection_lock_engine.dispose()
        self._engine.dispose()

    def __del__(self) -> None:
        with suppress(Exception):
            self.close()

    @staticmethod
    def _payload_dict(model: BaseModel) -> PayloadDict:
        return model.model_dump(mode="json", exclude_none=True)

    @staticmethod
    def _read_payload(*, model_type: type[RecordModel], payload: PayloadDict) -> RecordModel:
        return model_type.model_validate(payload)

    def _write_row(self, row: Base) -> None:
        with self._session_factory() as session:
            session.merge(row)
            session.commit()

    def _after_product_authority_bundle_step(self, step_name: str) -> None:
        return None

    def _after_authz_policy_write_step(self, step_name: str) -> None:
        return None

    @staticmethod
    def _after_tenant_technical_human_waiver_write_step(_step_name: str) -> None:
        return None

    def _merge_authority_row(self, session: Any, row: Base, *, step_name: str) -> None:
        session.merge(row)
        self._after_product_authority_bundle_step(step_name)

    def _product_profile_row(
        self, record: LaunchplaneProductProfileRecord
    ) -> LaunchplaneProductProfileRow:
        return LaunchplaneProductProfileRow(
            product=record.product,
            display_name=record.display_name,
            repository=record.repository,
            driver_id=record.driver_id,
            updated_at=record.updated_at,
            payload=self._payload_dict(record),
        )

    def _dokploy_target_id_row(
        self, record: DokployTargetIdRecord
    ) -> LaunchplaneDokployTargetIdRow:
        return LaunchplaneDokployTargetIdRow(
            context=record.context,
            instance=record.instance,
            target_id=record.target_id,
            updated_at=record.updated_at,
            payload=self._payload_dict(record),
        )

    def _dokploy_target_row(self, record: DokployTargetRecord) -> LaunchplaneDokployTargetRow:
        return LaunchplaneDokployTargetRow(
            context=record.context,
            instance=record.instance,
            updated_at=record.updated_at,
            payload=self._payload_dict(record),
        )

    def _provider_target_row(self, record: ProviderTargetRecord) -> LaunchplaneProviderTargetRow:
        return LaunchplaneProviderTargetRow(
            context=record.context,
            instance=record.instance,
            provider_id=record.provider_id,
            target_category=record.target_category,
            target_id=record.target_id,
            display_name=record.display_name,
            provider_target_type=record.provider_target_type,
            updated_at=record.updated_at,
            payload=self._payload_dict(record),
        )

    def _runtime_environment_row(
        self, record: RuntimeEnvironmentRecord
    ) -> LaunchplaneRuntimeEnvironmentRow:
        return LaunchplaneRuntimeEnvironmentRow(
            scope=record.scope,
            context=record.context,
            instance=record.instance,
            updated_at=record.updated_at,
            payload=self._payload_dict(record),
        )

    def _runtime_environment_delete_event_row(
        self, event: RuntimeEnvironmentDeleteEvent
    ) -> LaunchplaneRuntimeEnvironmentDeleteEventRow:
        return LaunchplaneRuntimeEnvironmentDeleteEventRow(
            event_id=event.event_id,
            scope=event.scope,
            context=event.context,
            instance=event.instance,
            recorded_at=event.recorded_at,
            payload=self._payload_dict(event),
        )

    def _secret_row(self, record: SecretRecord) -> LaunchplaneSecretRow:
        return LaunchplaneSecretRow(
            secret_id=record.secret_id,
            scope=record.scope,
            integration=record.integration,
            name=record.name,
            context=record.context,
            instance=record.instance,
            status=record.status,
            current_version_id=record.current_version_id,
            updated_at=record.updated_at,
            payload=self._payload_dict(record),
        )

    def _secret_version_row(self, version: SecretVersion) -> LaunchplaneSecretVersionRow:
        return LaunchplaneSecretVersionRow(
            version_id=version.version_id,
            secret_id=version.secret_id,
            created_at=version.created_at,
            payload=self._payload_dict(version),
        )

    def _secret_binding_row(self, binding: SecretBinding) -> LaunchplaneSecretBindingRow:
        return LaunchplaneSecretBindingRow(
            binding_id=binding.binding_id,
            secret_id=binding.secret_id,
            integration=binding.integration,
            binding_key=binding.binding_key,
            context=binding.context,
            instance=binding.instance,
            status=binding.status,
            updated_at=binding.updated_at,
            payload=self._payload_dict(binding),
        )

    def _secret_audit_event_row(self, event: SecretAuditEvent) -> LaunchplaneSecretAuditEventRow:
        return LaunchplaneSecretAuditEventRow(
            event_id=event.event_id,
            secret_id=event.secret_id,
            event_type=event.event_type,
            recorded_at=event.recorded_at,
            payload=self._payload_dict(event),
        )

    def _environment_inventory_row(self, record: EnvironmentInventory) -> LaunchplaneInventoryRow:
        return LaunchplaneInventoryRow(
            context=record.context,
            instance=record.instance,
            artifact_id=_artifact_id_from_model(record),
            source_git_ref=record.source_git_ref,
            updated_at=record.updated_at,
            deployment_record_id=record.deployment_record_id,
            promotion_record_id=record.promotion_record_id,
            promoted_from_instance=record.promoted_from_instance,
            payload=self._payload_dict(record),
        )

    def _release_tuple_row(self, record: ReleaseTupleRecord) -> LaunchplaneReleaseTupleRow:
        return LaunchplaneReleaseTupleRow(
            context=record.context,
            channel=record.channel,
            tuple_id=record.tuple_id,
            artifact_id=record.artifact_id,
            minted_at=record.minted_at,
            provenance=record.provenance,
            payload=self._payload_dict(record),
        )

    def write_product_authority_bundle(self, bundle: ProductAuthorityBundle) -> None:
        if not bundle.requires_write():
            return
        with self._session_factory() as session:
            self._begin_serialized_write(session)
            self._lock_product_authority_bundle_write(session)
            for delete_item in bundle.delete_runtime_environments:
                row = session.scalar(
                    self._runtime_environment_statement(
                        scope=delete_item.expected_record.scope,
                        context=delete_item.expected_record.context,
                        instance=delete_item.expected_record.instance,
                        for_update=True,
                    )
                )
                if row is None:
                    raise FileNotFoundError("Runtime environment record was already deleted.")
                current_record = self._read_payload(
                    model_type=RuntimeEnvironmentRecord,
                    payload=row.payload,
                )
                if self._payload_dict(current_record) != self._payload_dict(
                    delete_item.expected_record
                ):
                    raise ValueError("Runtime environment record changed during bundle write.")
                session.delete(row)
                self._after_product_authority_bundle_step("delete_runtime_environment")
                self._merge_authority_row(
                    session,
                    self._runtime_environment_delete_event_row(delete_item.event),
                    step_name="write_runtime_environment_delete_event",
                )

            for provider_target_delete in bundle.delete_provider_targets:
                self._delete_current_authority_row(
                    session=session,
                    orm_model=LaunchplaneProviderTargetRow,
                    model_type=ProviderTargetRecord,
                    filters=(
                        LaunchplaneProviderTargetRow.context == provider_target_delete.context,
                        LaunchplaneProviderTargetRow.instance == provider_target_delete.instance,
                    ),
                    expected_record=provider_target_delete,
                    label="Provider target record",
                    step_name="delete_provider_target",
                )
            for target_id_delete in bundle.delete_dokploy_target_ids:
                self._delete_current_authority_row(
                    session=session,
                    orm_model=LaunchplaneDokployTargetIdRow,
                    model_type=DokployTargetIdRecord,
                    filters=(
                        LaunchplaneDokployTargetIdRow.context == target_id_delete.context,
                        LaunchplaneDokployTargetIdRow.instance == target_id_delete.instance,
                    ),
                    expected_record=target_id_delete,
                    label="Dokploy target ID record",
                    step_name="delete_dokploy_target_id",
                )
            for target_delete in bundle.delete_dokploy_targets:
                self._delete_current_authority_row(
                    session=session,
                    orm_model=LaunchplaneDokployTargetRow,
                    model_type=DokployTargetRecord,
                    filters=(
                        LaunchplaneDokployTargetRow.context == target_delete.context,
                        LaunchplaneDokployTargetRow.instance == target_delete.instance,
                    ),
                    expected_record=target_delete,
                    label="Dokploy target record",
                    step_name="delete_dokploy_target",
                )

            for profile_record in bundle.product_profiles:
                self._merge_authority_row(
                    session,
                    self._product_profile_row(profile_record),
                    step_name="write_product_profile",
                )
            for target_record in bundle.dokploy_targets:
                self._merge_authority_row(
                    session,
                    self._dokploy_target_row(target_record),
                    step_name="write_dokploy_target",
                )
            for target_id_record in bundle.dokploy_target_ids:
                self._merge_authority_row(
                    session,
                    self._dokploy_target_id_row(target_id_record),
                    step_name="write_dokploy_target_id",
                )
            for provider_target_write in bundle.provider_target_writes:
                self._write_provider_target_with_expectation(
                    session=session,
                    write=provider_target_write,
                )
            for runtime_record in bundle.runtime_environments:
                self._merge_authority_row(
                    session,
                    self._runtime_environment_row(runtime_record),
                    step_name="write_runtime_environment",
                )
            for version in bundle.secret_versions:
                self._merge_authority_row(
                    session,
                    self._secret_version_row(version),
                    step_name="write_secret_version",
                )
            for secret_record in bundle.secret_records:
                self._merge_authority_row(
                    session, self._secret_row(secret_record), step_name="write_secret_record"
                )
            for binding in bundle.secret_bindings:
                self._merge_authority_row(
                    session,
                    self._secret_binding_row(binding),
                    step_name="write_secret_binding",
                )
            for event in bundle.secret_audit_events:
                self._merge_authority_row(
                    session,
                    self._secret_audit_event_row(event),
                    step_name="write_secret_audit_event",
                )
            for inventory_record in bundle.environment_inventory:
                self._merge_authority_row(
                    session,
                    self._environment_inventory_row(inventory_record),
                    step_name="write_environment_inventory",
                )
            for release_record in bundle.release_tuples:
                self._merge_authority_row(
                    session,
                    self._release_tuple_row(release_record),
                    step_name="write_release_tuple",
                )
            if bundle.idempotency_record is not None:
                self._merge_authority_row(
                    session,
                    self._idempotency_row(bundle.idempotency_record),
                    step_name="write_idempotency",
                )
            session.commit()

    def _write_provider_target_with_expectation(
        self,
        *,
        session: Any,
        write: ProviderTargetWrite,
    ) -> None:
        conflicting_identity_statement = select(LaunchplaneProviderTargetRow).where(
            LaunchplaneProviderTargetRow.provider_id == write.record.provider_id,
            LaunchplaneProviderTargetRow.target_category == write.record.target_category,
            LaunchplaneProviderTargetRow.target_id == write.record.target_id,
        )
        if not self.database_url.startswith("sqlite"):
            conflicting_identity_statement = conflicting_identity_statement.with_for_update()
        allowed_conflicting_routes = set(write.allowed_conflicting_routes)
        conflicting_routes = sorted(
            (candidate.context, candidate.instance)
            for candidate in session.scalars(conflicting_identity_statement)
            if (candidate.context, candidate.instance)
            != (write.record.context, write.record.instance)
            and (candidate.context, candidate.instance) not in allowed_conflicting_routes
        )
        if conflicting_routes:
            formatted_routes = ", ".join(
                f"{context}/{instance}" for context, instance in conflicting_routes
            )
            raise ValueError(
                "Provider target identity was bound to another route after authority "
                f"bundle planning: {formatted_routes}."
            )
        statement = (
            select(LaunchplaneProviderTargetRow)
            .where(
                LaunchplaneProviderTargetRow.context == write.record.context,
                LaunchplaneProviderTargetRow.instance == write.record.instance,
            )
            .limit(1)
        )
        if not self.database_url.startswith("sqlite"):
            statement = statement.with_for_update()
        row = session.scalar(statement)
        if write.expected_absent:
            if row is not None:
                raise ValueError(
                    "Provider target record was created after authority bundle planning."
                )
            session.add(self._provider_target_row(write.record))
            try:
                session.flush()
            except IntegrityError as error:
                raise ValueError(
                    "Provider target record was created after authority bundle planning."
                ) from error
            self._after_product_authority_bundle_step("write_provider_target")
            return
        expected_record = write.expected_record
        if expected_record is None:
            raise ValueError("Provider target write expectation is missing.")
        if row is None:
            raise ValueError("Provider target record changed after authority bundle planning.")
        current_record = self._read_payload(
            model_type=ProviderTargetRecord,
            payload=row.payload,
        )
        if self._payload_dict(current_record) != self._payload_dict(expected_record):
            raise ValueError("Provider target record changed after authority bundle planning.")
        self._merge_authority_row(
            session,
            self._provider_target_row(write.record),
            step_name="write_provider_target",
        )

    def _delete_current_authority_row(
        self,
        *,
        session: Any,
        orm_model: type[Base],
        model_type: type[RecordModel],
        filters: Sequence[object],
        expected_record: RecordModel,
        label: str,
        step_name: str,
    ) -> None:
        statement = select(orm_model).where(*cast(Any, filters)).limit(1)
        if not self.database_url.startswith("sqlite"):
            statement = statement.with_for_update()
        row = session.scalar(statement)
        if row is None:
            raise FileNotFoundError(f"{label} was already deleted.")
        current_record = self._read_payload(
            model_type=model_type,
            payload=_payload_from_row(row),
        )
        if self._payload_dict(current_record) != self._payload_dict(expected_record):
            raise ValueError(f"{label} changed during bundle write.")
        session.delete(row)
        self._after_product_authority_bundle_step(step_name)

    def _runtime_environment_statement(
        self,
        *,
        scope: str,
        context: str,
        instance: str,
        for_update: bool = False,
    ) -> Any:
        statement = (
            select(LaunchplaneRuntimeEnvironmentRow)
            .where(
                LaunchplaneRuntimeEnvironmentRow.scope == scope,
                LaunchplaneRuntimeEnvironmentRow.context == context,
                LaunchplaneRuntimeEnvironmentRow.instance == instance,
            )
            .limit(1)
        )
        if for_update and not self.database_url.startswith("sqlite"):
            statement = statement.with_for_update()
        return statement

    def _advisory_lock_merge_train_controller(self, session: Any, controller_key: str) -> None:
        if self.database_url.startswith("sqlite"):
            return
        session.execute(
            text("select pg_advisory_xact_lock(hashtextextended(:lock_name, 0))"),
            {"lock_name": controller_key},
        )

    def _sync_merge_train_controller_state_row(
        self,
        row: LaunchplaneMergeTrainControllerStateRow,
        record: MergeTrainControllerStateRecord,
    ) -> None:
        row.repository = record.repository
        row.base_branch = record.base_branch
        row.status = record.status
        row.policy_key = record.policy_key
        row.policy_sha256 = record.policy_sha256
        row.updated_at = record.updated_at
        row.lease_owner = record.lease_owner
        row.lease_expires_at = record.lease_expires_at
        row.active_action = record.active_action
        row.active_phase = record.active_phase
        row.payload = self._payload_dict(record)

    def _read_model(
        self,
        *,
        model_type: type[RecordModel],
        orm_model: type[Base],
        filters: Sequence[object],
    ) -> RecordModel:
        statement = select(orm_model).where(*cast(Any, filters)).limit(1)
        with self._session_factory() as session:
            row = session.scalar(statement)
            if row is None:
                raise FileNotFoundError(
                    f"No Launchplane record found in {orm_model.__tablename__} for {tuple(filters)!r}"
                )
            return self._read_payload(model_type=model_type, payload=getattr(row, "payload"))

    def _read_optional_model(
        self,
        *,
        model_type: type[RecordModel],
        orm_model: type[Base],
        filters: Sequence[object],
    ) -> RecordModel | None:
        try:
            return self._read_model(
                model_type=model_type,
                orm_model=orm_model,
                filters=filters,
            )
        except FileNotFoundError:
            return None

    def _list_models(
        self,
        *,
        model_type: type[RecordModel],
        orm_model: type[Base],
        filters: Sequence[object] = (),
        order_by: Sequence[object],
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[RecordModel, ...]:
        statement = select(orm_model)
        if filters:
            statement = statement.where(*cast(Any, filters))
        statement = statement.order_by(*cast(Any, order_by))
        if offset > 0:
            statement = statement.offset(offset)
        if limit is not None:
            statement = statement.limit(limit)
        with self._session_factory() as session:
            rows = session.scalars(statement).all()
            return tuple(
                self._read_payload(
                    model_type=model_type,
                    payload=_payload_from_row(row),
                )
                for row in rows
            )

    def write_artifact_manifest(self, manifest: ArtifactIdentityManifest) -> None:
        self._write_row(
            LaunchplaneArtifactManifestRow(
                artifact_id=manifest.artifact_id,
                source_commit=manifest.source_commit,
                image_repository=manifest.image.repository,
                image_digest=manifest.image.digest,
                payload=self._payload_dict(manifest),
            )
        )

    def read_artifact_manifest(self, artifact_id: str) -> ArtifactIdentityManifest:
        return self._read_model(
            model_type=ArtifactIdentityManifest,
            orm_model=LaunchplaneArtifactManifestRow,
            filters=(LaunchplaneArtifactManifestRow.artifact_id == artifact_id,),
        )

    def list_artifact_manifests(self) -> tuple[ArtifactIdentityManifest, ...]:
        return self._list_models(
            model_type=ArtifactIdentityManifest,
            orm_model=LaunchplaneArtifactManifestRow,
            order_by=(LaunchplaneArtifactManifestRow.artifact_id.desc(),),
        )

    def write_backup_gate_record(self, record: BackupGateRecord) -> None:
        self._write_row(
            LaunchplaneBackupGateRow(
                record_id=record.record_id,
                context=record.context,
                instance=record.instance,
                created_at=record.created_at,
                status=record.status,
                payload=self._payload_dict(record),
            )
        )

    def read_backup_gate_record(self, record_id: str) -> BackupGateRecord:
        return self._read_model(
            model_type=BackupGateRecord,
            orm_model=LaunchplaneBackupGateRow,
            filters=(LaunchplaneBackupGateRow.record_id == record_id,),
        )

    def list_backup_gate_records(
        self,
        *,
        context_name: str = "",
        instance_name: str = "",
        limit: int | None = None,
    ) -> tuple[BackupGateRecord, ...]:
        filters: list[object] = []
        if context_name:
            filters.append(LaunchplaneBackupGateRow.context == context_name)
        if instance_name:
            filters.append(LaunchplaneBackupGateRow.instance == instance_name)
        return self._list_models(
            model_type=BackupGateRecord,
            orm_model=LaunchplaneBackupGateRow,
            filters=filters,
            order_by=(
                LaunchplaneBackupGateRow.created_at.desc(),
                LaunchplaneBackupGateRow.record_id.desc(),
            ),
            limit=limit,
        )

    def _production_backup_target_row(
        self, record: ProductionBackupTargetRecord
    ) -> LaunchplaneProductionBackupTargetRow:
        return LaunchplaneProductionBackupTargetRow(
            record_id=record.record_id,
            target_id=record.target_id,
            target_revision=record.target_revision,
            status=record.status,
            provider_type=record.provider_type,
            destination_kind=record.destination_kind,
            effective_at=record.effective_at,
            review_after=record.review_after,
            supersedes_record_id=record.supersedes_record_id,
            target_digest=record.target_digest,
            payload=self._payload_dict(record),
        )

    def write_production_backup_target_record(
        self, record: ProductionBackupTargetRecord
    ) -> Literal["written", "replayed"]:
        with self._session_factory() as session:
            self._begin_serialized_write(session)
            self._lock_product_authority_bundle_write(session)
            rows = session.scalars(
                select(LaunchplaneProductionBackupTargetRow)
                .where(LaunchplaneProductionBackupTargetRow.target_id == record.target_id)
                .order_by(LaunchplaneProductionBackupTargetRow.target_revision.asc())
                .with_for_update()
            ).all()
            records = tuple(
                self._read_payload(
                    model_type=ProductionBackupTargetRecord,
                    payload=row.payload,
                )
                for row in rows
            )
            plan = plan_production_backup_target_append(records=records, record=record)
            if plan.status == "replayed":
                return "replayed"
            if plan.superseded_current_record is not None:
                session.merge(self._production_backup_target_row(plan.superseded_current_record))
                session.flush()
            session.add(self._production_backup_target_row(record))
            session.commit()
        return "written"

    def read_production_backup_target_record(self, record_id: str) -> ProductionBackupTargetRecord:
        return self._read_model(
            model_type=ProductionBackupTargetRecord,
            orm_model=LaunchplaneProductionBackupTargetRow,
            filters=(LaunchplaneProductionBackupTargetRow.record_id == record_id,),
        )

    def list_production_backup_target_records(
        self,
        *,
        target_id: str = "",
        status: str = "",
        limit: int | None = None,
    ) -> tuple[ProductionBackupTargetRecord, ...]:
        filters: list[object] = []
        if target_id:
            filters.append(LaunchplaneProductionBackupTargetRow.target_id == target_id)
        if status:
            filters.append(LaunchplaneProductionBackupTargetRow.status == status)
        return self._list_models(
            model_type=ProductionBackupTargetRecord,
            orm_model=LaunchplaneProductionBackupTargetRow,
            filters=filters,
            order_by=(
                LaunchplaneProductionBackupTargetRow.target_revision.desc(),
                LaunchplaneProductionBackupTargetRow.target_id.asc(),
                LaunchplaneProductionBackupTargetRow.record_id.asc(),
            ),
            limit=limit,
        )

    def _production_backup_policy_row(
        self, record: ProductionBackupPolicyRecord
    ) -> LaunchplaneProductionBackupPolicyRow:
        return LaunchplaneProductionBackupPolicyRow(
            record_id=record.record_id,
            policy_id=record.policy_id,
            product=record.product,
            context=record.context,
            instance=record.instance,
            promotion_action=record.promotion_action,
            policy_revision=record.policy_revision,
            status=record.status,
            source_target_id=record.fast_snapshot.source_target_id,
            destination_target_id=record.independent_backup.destination_target_id,
            effective_at=record.effective_at,
            review_after=record.review_after,
            supersedes_record_id=record.supersedes_record_id,
            policy_digest=record.policy_digest,
            payload=self._payload_dict(record),
        )

    def write_production_backup_policy_record(
        self, record: ProductionBackupPolicyRecord
    ) -> Literal["written", "replayed"]:
        with self._session_factory() as session:
            self._begin_serialized_write(session)
            self._lock_product_authority_bundle_write(session)
            policy_rows = session.scalars(
                select(LaunchplaneProductionBackupPolicyRow)
                .where(LaunchplaneProductionBackupPolicyRow.policy_id == record.policy_id)
                .order_by(LaunchplaneProductionBackupPolicyRow.policy_revision.asc())
                .with_for_update()
            ).all()
            records = tuple(
                self._read_payload(
                    model_type=ProductionBackupPolicyRecord,
                    payload=row.payload,
                )
                for row in policy_rows
            )
            if record.status == "active":
                target_rows = session.scalars(
                    select(LaunchplaneProductionBackupTargetRow).with_for_update()
                ).all()
                validate_production_backup_policy_binding(
                    policy=record,
                    target_records=tuple(
                        self._read_payload(
                            model_type=ProductionBackupTargetRecord,
                            payload=row.payload,
                        )
                        for row in target_rows
                    ),
                )
            plan = plan_production_backup_policy_append(records=records, record=record)
            if plan.status == "replayed":
                return "replayed"
            if plan.superseded_current_record is not None:
                session.merge(self._production_backup_policy_row(plan.superseded_current_record))
                session.flush()
            session.add(self._production_backup_policy_row(record))
            session.commit()
        return "written"

    def read_production_backup_policy_record(self, record_id: str) -> ProductionBackupPolicyRecord:
        return self._read_model(
            model_type=ProductionBackupPolicyRecord,
            orm_model=LaunchplaneProductionBackupPolicyRow,
            filters=(LaunchplaneProductionBackupPolicyRow.record_id == record_id,),
        )

    def list_production_backup_policy_records(
        self,
        *,
        product: str = "",
        context_name: str = "",
        instance_name: str = "",
        promotion_action: str = "",
        status: str = "",
        limit: int | None = None,
    ) -> tuple[ProductionBackupPolicyRecord, ...]:
        filters: list[object] = []
        if product:
            filters.append(LaunchplaneProductionBackupPolicyRow.product == product)
        if context_name:
            filters.append(LaunchplaneProductionBackupPolicyRow.context == context_name)
        if instance_name:
            filters.append(LaunchplaneProductionBackupPolicyRow.instance == instance_name)
        if promotion_action:
            filters.append(
                LaunchplaneProductionBackupPolicyRow.promotion_action == promotion_action
            )
        if status:
            filters.append(LaunchplaneProductionBackupPolicyRow.status == status)
        return self._list_models(
            model_type=ProductionBackupPolicyRecord,
            orm_model=LaunchplaneProductionBackupPolicyRow,
            filters=filters,
            order_by=(
                LaunchplaneProductionBackupPolicyRow.policy_revision.desc(),
                LaunchplaneProductionBackupPolicyRow.product.asc(),
                LaunchplaneProductionBackupPolicyRow.context.asc(),
                LaunchplaneProductionBackupPolicyRow.instance.asc(),
                LaunchplaneProductionBackupPolicyRow.promotion_action.asc(),
                LaunchplaneProductionBackupPolicyRow.record_id.asc(),
            ),
            limit=limit,
        )

    def apply_production_backup_authority(
        self, envelope: ProductionBackupAuthorityWriteEnvelope
    ) -> ProductionBackupAuthorityWriteResult:
        with self._session_factory() as session:
            self._begin_serialized_write(session)
            self._lock_product_authority_bundle_write(session)
            plan = self._plan_production_backup_authority_in_session(
                session=session,
                envelope=envelope,
            )
            if envelope.mode == "dry_run" or plan.result.status == "replayed":
                return plan.result
            self._write_production_backup_authority_plan(
                session=session,
                envelope=envelope,
                target_plans=plan.target_plans,
                policy_plan=plan.policy_plan,
            )
            session.commit()
            return plan.result.model_copy(update={"status": "applied"})

    def compare_and_apply_production_backup_authority(
        self,
        *,
        envelope: ProductionBackupAuthorityWriteEnvelope,
        mutation: DbOnlyMutationRequest,
        response_payload_builder: Callable[[ProductionBackupAuthorityWriteResult], dict[str, Any]],
    ) -> ProductionBackupAuthorityCompareWriteResult:
        if envelope.mode != "apply":
            raise ValueError("Production backup authority compare-write requires apply mode.")
        with self._session_factory() as session:
            self._begin_serialized_write(session)
            reservation_status, reservation_row, reservation = (
                self._reserve_db_only_mutation_in_session(
                    session=session,
                    mutation=mutation,
                )
            )
            if reservation_status != "acquired":
                return ProductionBackupAuthorityCompareWriteResult(
                    status=cast(ProductionBackupAuthorityCompareWriteStatus, reservation_status),
                    idempotency_record=reservation,
                )
            assert reservation_row is not None
            self._lock_product_authority_bundle_write(session)
            try:
                plan = self._plan_production_backup_authority_in_session(
                    session=session,
                    envelope=envelope,
                )
            except Exception:
                session.delete(reservation_row)
                session.commit()
                raise
            self._write_production_backup_authority_plan(
                session=session,
                envelope=envelope,
                target_plans=plan.target_plans,
                policy_plan=plan.policy_plan,
            )
            applied_result = plan.result.model_copy(update={"status": "applied"})
            completed_at = self._database_mutation_timestamp(session)
            completion = complete_launchplane_mutation_reservation(
                reservation,
                response_status_code=mutation.response_status_code,
                response_trace_id=mutation.response_trace_id,
                completed_at=completed_at,
                response_payload=response_payload_builder(applied_result),
            )
            self._sync_idempotency_row(reservation_row, completion)
            session.commit()
            return ProductionBackupAuthorityCompareWriteResult(
                status="written",
                result=applied_result,
                idempotency_record=completion,
            )

    def _plan_production_backup_authority_in_session(
        self,
        *,
        session: Any,
        envelope: ProductionBackupAuthorityWriteEnvelope,
    ) -> ProductionBackupAuthorityWritePlan:
        target_statement = select(LaunchplaneProductionBackupTargetRow)
        policy_statement = select(LaunchplaneProductionBackupPolicyRow)
        if not self.database_url.startswith("sqlite"):
            target_statement = target_statement.with_for_update()
            policy_statement = policy_statement.with_for_update()
        target_records = tuple(
            self._read_payload(
                model_type=ProductionBackupTargetRecord,
                payload=row.payload,
            )
            for row in session.scalars(target_statement).all()
        )
        policy_records = tuple(
            self._read_payload(
                model_type=ProductionBackupPolicyRecord,
                payload=row.payload,
            )
            for row in session.scalars(policy_statement).all()
        )
        return plan_production_backup_authority_write_from_records(
            target_records=target_records,
            policy_records=policy_records,
            envelope=envelope,
        )

    def _write_production_backup_authority_plan(
        self,
        *,
        session: Any,
        envelope: ProductionBackupAuthorityWriteEnvelope,
        target_plans: tuple[ProductionBackupTargetAppendPlan, ...],
        policy_plan: ProductionBackupPolicyAppendPlan,
    ) -> None:
        for target, target_plan in zip(envelope.targets, target_plans, strict=True):
            if target_plan.status == "replayed":
                continue
            if target_plan.superseded_current_record is not None:
                session.merge(
                    self._production_backup_target_row(target_plan.superseded_current_record)
                )
                session.flush()
            session.add(self._production_backup_target_row(target))
        if policy_plan.status != "replayed":
            if policy_plan.superseded_current_record is not None:
                session.merge(
                    self._production_backup_policy_row(policy_plan.superseded_current_record)
                )
                session.flush()
            session.add(self._production_backup_policy_row(envelope.policy))
        session.flush()

    def _idempotency_row(self, record: LaunchplaneIdempotencyRecord) -> LaunchplaneIdempotencyRow:
        return LaunchplaneIdempotencyRow(
            record_id=record.record_id,
            scope=record.scope,
            route_path=record.route_path,
            idempotency_key=record.idempotency_key,
            request_fingerprint=record.request_fingerprint,
            state=record.state,
            lease_owner=record.lease_owner,
            lease_expires_at=record.lease_expires_at,
            attempt=record.attempt,
            reconciliation_key=record.reconciliation_key,
            provider_target_key=record.provider_target_key,
            created_at=record.created_at,
            updated_at=record.updated_at,
            response_status_code=record.response_status_code,
            response_trace_id=record.response_trace_id,
            recorded_at=record.recorded_at,
            payload=self._payload_dict(record),
        )

    def _sync_idempotency_row(
        self,
        row: LaunchplaneIdempotencyRow,
        record: LaunchplaneIdempotencyRecord,
    ) -> None:
        row.request_fingerprint = record.request_fingerprint
        row.state = record.state
        row.lease_owner = record.lease_owner
        row.lease_expires_at = record.lease_expires_at
        row.attempt = record.attempt
        row.reconciliation_key = record.reconciliation_key
        row.provider_target_key = record.provider_target_key
        row.created_at = record.created_at
        row.updated_at = record.updated_at
        row.response_status_code = record.response_status_code
        row.response_trace_id = record.response_trace_id
        row.recorded_at = record.recorded_at
        row.payload = self._payload_dict(record)

    def _outbox_delivery_row(self, record: OutboxDeliveryRecord) -> LaunchplaneOutboxDeliveryRow:
        return LaunchplaneOutboxDeliveryRow(
            delivery_id=record.delivery_id,
            kind=record.kind,
            state=record.state,
            aggregate_type=record.aggregate_type,
            aggregate_id=record.aggregate_id,
            dedupe_key=record.dedupe_key,
            created_at=record.created_at,
            updated_at=record.updated_at,
            next_attempt_at=record.next_attempt_at,
            lease_owner=record.lease_owner,
            lease_expires_at=record.lease_expires_at,
            attempt=record.attempt,
            max_attempts=record.max_attempts,
            provider_operation_key=record.provider_operation_key,
            provider_id=record.provider_id,
            external_id=record.external_id,
            external_url=record.external_url,
            action=record.action,
            error_code=record.error_code,
            payload=self._payload_dict(record),
        )

    def _sync_outbox_delivery_row(
        self,
        row: LaunchplaneOutboxDeliveryRow,
        record: OutboxDeliveryRecord,
    ) -> None:
        row.kind = record.kind
        row.state = record.state
        row.aggregate_type = record.aggregate_type
        row.aggregate_id = record.aggregate_id
        row.dedupe_key = record.dedupe_key
        row.created_at = record.created_at
        row.updated_at = record.updated_at
        row.next_attempt_at = record.next_attempt_at
        row.lease_owner = record.lease_owner
        row.lease_expires_at = record.lease_expires_at
        row.attempt = record.attempt
        row.max_attempts = record.max_attempts
        row.provider_operation_key = record.provider_operation_key
        row.provider_id = record.provider_id
        row.external_id = record.external_id
        row.external_url = record.external_url
        row.action = record.action
        row.error_code = record.error_code
        row.payload = self._payload_dict(record)

    def _idempotency_statement(
        self,
        *,
        scope: str,
        route_path: str,
        idempotency_key: str,
        for_update: bool = False,
    ) -> Any:
        statement = (
            select(LaunchplaneIdempotencyRow)
            .where(
                LaunchplaneIdempotencyRow.scope == scope,
                LaunchplaneIdempotencyRow.route_path == route_path,
                LaunchplaneIdempotencyRow.idempotency_key == idempotency_key,
            )
            .limit(1)
        )
        if for_update and not self.database_url.startswith("sqlite"):
            statement = statement.with_for_update()
        return statement

    def _active_provider_target_statement(
        self,
        *,
        provider_target_key: str,
        for_update: bool = False,
    ) -> Any:
        statement = (
            select(LaunchplaneIdempotencyRow)
            .where(
                LaunchplaneIdempotencyRow.provider_target_key == provider_target_key,
                LaunchplaneIdempotencyRow.state.in_(("running", "reconcile_required")),
            )
            .limit(1)
        )
        if for_update and not self.database_url.startswith("sqlite"):
            statement = statement.with_for_update()
        return statement

    def _begin_serialized_write(self, session: Any) -> None:
        if self.database_url.startswith("sqlite"):
            session.execute(text("BEGIN IMMEDIATE"))

    def _lock_product_authority_bundle_write(self, session: Any) -> None:
        if self.database_url.startswith("sqlite"):
            return
        session.execute(
            text("select pg_advisory_xact_lock(hashtextextended(:lock_name, 0))"),
            {"lock_name": "launchplane:product-authority-bundle"},
        )

    def _lock_route_binding_write(self, session: Any, *, binding_key: str) -> None:
        if self.database_url.startswith("sqlite"):
            return
        session.execute(
            text("select pg_advisory_xact_lock(hashtextextended(:lock_name, 0))"),
            {"lock_name": f"launchplane:route-binding:{binding_key}"},
        )

    def _lock_merge_train_policy_write(self, session: Any) -> None:
        if self.database_url.startswith("sqlite"):
            return
        session.execute(
            text("select pg_advisory_xact_lock(hashtextextended(:lock_name, 0))"),
            {"lock_name": "launchplane:active-merge-train-policy"},
        )

    def _lock_public_ingress_incident_write(
        self,
        session: Any,
        *,
        product: str,
        context_name: str,
        instance_name: str,
        check_token: str,
        check_kind: str,
    ) -> None:
        if self.database_url.startswith("sqlite"):
            return
        session.execute(
            text("select pg_advisory_xact_lock(hashtextextended(:lock_name, 0))"),
            {
                "lock_name": ":".join(
                    (
                        "launchplane",
                        "public-ingress-incident",
                        product,
                        context_name,
                        instance_name,
                        check_token,
                        check_kind,
                    )
                )
            },
        )

    @contextmanager
    def _product_authority_bundle_read_guard(self) -> Iterator[None]:
        with self._session_factory() as session:
            if self.database_url.startswith("sqlite"):
                session.execute(text("BEGIN IMMEDIATE"))
            else:
                session.execute(
                    text("select pg_advisory_xact_lock_shared(hashtextextended(:lock_name, 0))"),
                    {"lock_name": "launchplane:product-authority-bundle"},
                )
            try:
                yield
            finally:
                session.rollback()

    def _database_mutation_timestamp(self, session: Any) -> str:
        if self.database_url.startswith("sqlite"):
            value = session.scalar(select(func.current_timestamp()))
        else:
            value = session.scalar(select(func.clock_timestamp()))
        if isinstance(value, datetime):
            parsed = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        else:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
        return format_launchplane_mutation_timestamp(parsed)

    def _owner_control_shadow_timestamp(self, session: Any) -> str:
        return self._database_mutation_timestamp(session).replace("Z", "+00:00")

    @staticmethod
    def _mutation_lease_expiry(*, observed_at: str, lease_seconds: int) -> str:
        if lease_seconds < 1 or lease_seconds > 86_400:
            raise ValueError("Mutation reservation lease_seconds must be between 1 and 86400.")
        return format_launchplane_mutation_timestamp(
            parse_launchplane_mutation_timestamp(
                observed_at,
                field_name="observed_at",
            )
            + timedelta(seconds=lease_seconds)
        )

    @staticmethod
    def _updated_idempotency_record(
        record: LaunchplaneIdempotencyRecord,
        **updates: object,
    ) -> LaunchplaneIdempotencyRecord:
        payload = record.model_dump(mode="json")
        payload.update(updates)
        return LaunchplaneIdempotencyRecord.model_validate(payload)

    @staticmethod
    def _updated_outbox_delivery_record(
        record: OutboxDeliveryRecord,
        **updates: object,
    ) -> OutboxDeliveryRecord:
        payload = record.model_dump(mode="json")
        payload.update(updates)
        return OutboxDeliveryRecord.model_validate(payload)

    @staticmethod
    def _mutation_transition_identity(
        record: LaunchplaneIdempotencyRecord,
    ) -> tuple[object, ...]:
        return (
            record.record_id,
            record.scope,
            record.route_path,
            record.idempotency_key,
            record.request_fingerprint,
            record.lease_owner,
            record.lease_expires_at,
            record.attempt,
            record.reconciliation_key,
            record.provider_target_key,
            record.provider_effect_phase,
            record.provider_effect_started_at,
            record.created_at,
        )

    def _mutation_reservation_matches(
        self,
        current_record: LaunchplaneIdempotencyRecord,
        reservation: LaunchplaneIdempotencyRecord,
    ) -> bool:
        return self._mutation_transition_identity(
            current_record
        ) == self._mutation_transition_identity(reservation)

    def write_idempotency_record(self, record: LaunchplaneIdempotencyRecord) -> None:
        if record.state != "completed":
            raise ValueError(
                "Running or reconcile-required reservations must use mutation reservation methods."
            )
        self._write_row(self._idempotency_row(record))

    def write_outbox_delivery_record(self, record: OutboxDeliveryRecord) -> None:
        self._write_row(self._outbox_delivery_row(record))

    def enqueue_outbox_delivery_with_idempotency(
        self, request: OutboxWithIdempotencyRequest
    ) -> OutboxDeliveryRecord:
        if (
            request.idempotency_record is not None
            and request.idempotency_record.state != "completed"
        ):
            raise ValueError("Outbox idempotency evidence must be completed replay evidence.")
        with self._session_factory() as session:
            self._begin_serialized_write(session)
            self._lock_outbox_dedupe_keys(
                session,
                dedupe_keys=(request.delivery.dedupe_key,),
            )
            existing_row = session.scalar(
                select(LaunchplaneOutboxDeliveryRow)
                .where(LaunchplaneOutboxDeliveryRow.dedupe_key == request.delivery.dedupe_key)
                .limit(1)
            )
            if existing_row is None:
                delivery = request.delivery
                session.add(self._outbox_delivery_row(delivery))
            else:
                delivery = self._read_payload(
                    model_type=OutboxDeliveryRecord,
                    payload=existing_row.payload,
                )
            if request.idempotency_record is not None:
                session.merge(self._idempotency_row(request.idempotency_record))
            session.commit()
            return delivery

    def _lock_outbox_dedupe_keys(
        self,
        session: Any,
        *,
        dedupe_keys: tuple[str, ...],
    ) -> None:
        if self.database_url.startswith("sqlite"):
            return
        for dedupe_key in sorted(set(dedupe_keys)):
            session.execute(
                text("select pg_advisory_xact_lock(hashtextextended(:lock_name, 0))"),
                {"lock_name": f"launchplane:outbox:{dedupe_key}"},
            )

    def read_outbox_delivery_record(self, delivery_id: str) -> OutboxDeliveryRecord:
        return self._read_model(
            model_type=OutboxDeliveryRecord,
            orm_model=LaunchplaneOutboxDeliveryRow,
            filters=(LaunchplaneOutboxDeliveryRow.delivery_id == delivery_id,),
        )

    def list_outbox_delivery_records(
        self,
        *,
        states: tuple[str, ...] = (),
        kind: str = "",
        aggregate_type: str = "",
        aggregate_id: str = "",
        limit: int | None = None,
    ) -> tuple[OutboxDeliveryRecord, ...]:
        filters: list[object] = []
        if states:
            filters.append(LaunchplaneOutboxDeliveryRow.state.in_(states))
        if kind:
            filters.append(LaunchplaneOutboxDeliveryRow.kind == kind)
        if aggregate_type:
            filters.append(LaunchplaneOutboxDeliveryRow.aggregate_type == aggregate_type)
        if aggregate_id:
            filters.append(LaunchplaneOutboxDeliveryRow.aggregate_id == aggregate_id)
        return self._list_models(
            model_type=OutboxDeliveryRecord,
            orm_model=LaunchplaneOutboxDeliveryRow,
            filters=filters,
            order_by=(
                LaunchplaneOutboxDeliveryRow.created_at.asc(),
                LaunchplaneOutboxDeliveryRow.delivery_id.asc(),
            ),
            limit=limit,
        )

    def claim_next_outbox_delivery_record(
        self,
        *,
        lease_owner: str,
        lease_seconds: int = 300,
        now: str = "",
    ) -> OutboxDeliveryClaimResult:
        normalized_lease_owner = lease_owner.strip()
        if not normalized_lease_owner:
            raise ValueError("Outbox delivery claim requires lease_owner.")
        observed_at = now.strip()
        with self._session_factory() as session:
            self._begin_serialized_write(session)
            if not observed_at:
                observed_at = self._database_mutation_timestamp(session)
            lease_expires_at = self._mutation_lease_expiry(
                observed_at=observed_at,
                lease_seconds=lease_seconds,
            )
            statement = (
                select(LaunchplaneOutboxDeliveryRow)
                .where(
                    LaunchplaneOutboxDeliveryRow.state.in_(
                        ("pending", "running", "reconcile_required")
                    ),
                    LaunchplaneOutboxDeliveryRow.next_attempt_at <= observed_at,
                    or_(
                        LaunchplaneOutboxDeliveryRow.state == "pending",
                        LaunchplaneOutboxDeliveryRow.state == "reconcile_required",
                        LaunchplaneOutboxDeliveryRow.lease_expires_at == "",
                        LaunchplaneOutboxDeliveryRow.lease_expires_at < observed_at,
                    ),
                )
                .order_by(
                    LaunchplaneOutboxDeliveryRow.next_attempt_at.asc(),
                    LaunchplaneOutboxDeliveryRow.created_at.asc(),
                    LaunchplaneOutboxDeliveryRow.delivery_id.asc(),
                )
                .limit(1)
            )
            if not self.database_url.startswith("sqlite"):
                statement = statement.with_for_update(skip_locked=True)
            row = session.scalar(statement)
            if row is None:
                return OutboxDeliveryClaimResult(status="empty")
            record = self._read_payload(model_type=OutboxDeliveryRecord, payload=row.payload)
            if record.state == "running" and record.provider_operation_key:
                record = self._updated_outbox_delivery_record(
                    record,
                    state="reconcile_required",
                    lease_owner="",
                    lease_expires_at="",
                    updated_at=observed_at,
                    next_attempt_at=observed_at,
                    error_code="lease_expired_after_provider_marker",
                )
            if record.attempt >= record.max_attempts:
                failed_record = self._updated_outbox_delivery_record(
                    record,
                    state="failed",
                    lease_owner="",
                    lease_expires_at="",
                    updated_at=observed_at,
                    next_attempt_at=observed_at,
                    error_code="max_attempts_exhausted",
                )
                self._sync_outbox_delivery_row(row, failed_record)
                session.commit()
                return OutboxDeliveryClaimResult(status="empty")
            claimed_record = self._updated_outbox_delivery_record(
                record,
                state="running",
                updated_at=observed_at,
                lease_owner=normalized_lease_owner,
                lease_expires_at=lease_expires_at,
                attempt=record.attempt + 1,
                error_code="",
            )
            self._sync_outbox_delivery_row(row, claimed_record)
            session.commit()
            return OutboxDeliveryClaimResult(status="claimed", record=claimed_record)

    def mark_outbox_delivery_provider_started(
        self,
        *,
        record: OutboxDeliveryRecord,
        lease_owner: str,
        provider_operation_key: str,
        provider_id: str = "",
        updated_at: str = "",
    ) -> OutboxDeliveryCompletionResult:
        normalized_provider_operation_key = provider_operation_key.strip()
        if not normalized_provider_operation_key:
            raise ValueError("Outbox provider start requires provider_operation_key.")
        return self._update_running_outbox_delivery(
            record=record,
            lease_owner=lease_owner,
            updates={
                "provider_operation_key": normalized_provider_operation_key,
                "provider_id": provider_id.strip(),
                "payload": record.payload,
                "updated_at": updated_at.strip(),
            },
        )

    def complete_outbox_delivery_record(
        self,
        *,
        record: OutboxDeliveryRecord,
        lease_owner: str,
    ) -> OutboxDeliveryCompletionResult:
        return self._update_running_outbox_delivery(
            record=record,
            lease_owner=lease_owner,
            updates=record.model_dump(mode="json"),
            require_terminal=True,
        )

    def _update_running_outbox_delivery(
        self,
        *,
        record: OutboxDeliveryRecord,
        lease_owner: str,
        updates: dict[str, object],
        require_terminal: bool = False,
    ) -> OutboxDeliveryCompletionResult:
        normalized_lease_owner = lease_owner.strip()
        if not normalized_lease_owner:
            raise ValueError("Outbox delivery update requires lease_owner.")
        with self._session_factory() as session:
            self._begin_serialized_write(session)
            observed_at = self._database_mutation_timestamp(session)
            statement = (
                select(LaunchplaneOutboxDeliveryRow)
                .where(LaunchplaneOutboxDeliveryRow.delivery_id == record.delivery_id)
                .limit(1)
            )
            if not self.database_url.startswith("sqlite"):
                statement = statement.with_for_update()
            row = session.scalar(statement)
            if row is None:
                return OutboxDeliveryCompletionResult(status="missing")
            current_record = self._read_payload(
                model_type=OutboxDeliveryRecord,
                payload=row.payload,
            )
            if current_record.state != "running":
                return OutboxDeliveryCompletionResult(status="not_running", record=current_record)
            if current_record.lease_owner != normalized_lease_owner:
                return OutboxDeliveryCompletionResult(
                    status="owner_mismatch", record=current_record
                )
            if current_record.lease_expires_at <= observed_at:
                return OutboxDeliveryCompletionResult(status="lease_expired", record=current_record)
            next_updates = dict(updates)
            if not _string_value(next_updates.get("updated_at") or "").strip():
                next_updates["updated_at"] = observed_at
            if require_terminal:
                next_updates["lease_owner"] = ""
                next_updates["lease_expires_at"] = ""
                if next_updates.get("state") in {"pending", "reconcile_required"}:
                    retry_seconds = min(
                        300,
                        5 * (2 ** min(max(current_record.attempt - 1, 0), 6)),
                    )
                    next_updates["next_attempt_at"] = self._mutation_lease_expiry(
                        observed_at=observed_at,
                        lease_seconds=retry_seconds,
                    )
            else:
                next_updates["state"] = "running"
                next_updates["lease_owner"] = normalized_lease_owner
                next_updates["lease_expires_at"] = current_record.lease_expires_at
            updated_record = self._updated_outbox_delivery_record(current_record, **next_updates)
            if require_terminal and updated_record.kind == "public_ingress_notification":
                attempt_payload = updated_record.payload.get("attempt_result")
                if isinstance(attempt_payload, dict):
                    attempt = PublicIngressNotificationAttemptRecord.model_validate(attempt_payload)
                    session.merge(
                        LaunchplanePublicIngressNotificationAttemptRow(
                            attempt_id=attempt.attempt_id,
                            incident_id=attempt.incident_id,
                            event=attempt.event,
                            destination_kind=attempt.destination_kind,
                            delivery_status=attempt.delivery_status,
                            attempted_at=attempt.attempted_at,
                            payload=self._payload_dict(attempt),
                        )
                    )
            self._sync_outbox_delivery_row(row, updated_record)
            session.commit()
            return OutboxDeliveryCompletionResult(status="updated", record=updated_record)

    def read_idempotency_record(
        self,
        *,
        scope: str,
        route_path: str,
        idempotency_key: str,
    ) -> LaunchplaneIdempotencyRecord | None:
        statement = self._idempotency_statement(
            scope=scope,
            route_path=route_path,
            idempotency_key=idempotency_key,
        )
        with self._session_factory() as session:
            row = session.scalar(statement)
            if row is None:
                return None
            return self._read_payload(model_type=LaunchplaneIdempotencyRecord, payload=row.payload)

    def lookup_existing_mutation_reservation(
        self,
        *,
        route_path: str,
        idempotency_key: str,
        request_fingerprint: str,
    ) -> ExistingMutationReservationLookupResult:
        normalized_route_path = route_path.strip()
        normalized_idempotency_key = idempotency_key.strip()
        normalized_request_fingerprint = request_fingerprint.strip()
        if not normalized_route_path:
            raise ValueError("Existing mutation lookup requires route_path.")
        if not normalized_idempotency_key:
            raise ValueError("Existing mutation lookup requires idempotency_key.")
        if not normalized_request_fingerprint:
            raise ValueError("Existing mutation lookup requires request_fingerprint.")
        statement = (
            select(LaunchplaneIdempotencyRow)
            .where(
                LaunchplaneIdempotencyRow.route_path == normalized_route_path,
                LaunchplaneIdempotencyRow.idempotency_key == normalized_idempotency_key,
            )
            .order_by(LaunchplaneIdempotencyRow.scope)
            .limit(2)
        )
        with self._session_factory() as session:
            observed_at = self._database_mutation_timestamp(session)
            rows = tuple(session.scalars(statement))
            if not rows:
                return ExistingMutationReservationLookupResult(
                    status="missing",
                    record=None,
                    observed_at=observed_at,
                )
            records: list[LaunchplaneIdempotencyRecord] = []
            for row in rows:
                raw_payload = row.payload
                if not isinstance(raw_payload, dict) or (
                    raw_payload.get("scope") != row.scope
                    or raw_payload.get("route_path") != row.route_path
                    or raw_payload.get("idempotency_key") != row.idempotency_key
                    or raw_payload.get("request_fingerprint") != row.request_fingerprint
                ):
                    return ExistingMutationReservationLookupResult(
                        status="conflict",
                        record=None,
                        observed_at=observed_at,
                    )
                try:
                    record = self._read_payload(
                        model_type=LaunchplaneIdempotencyRecord,
                        payload=raw_payload,
                    )
                except ValueError:
                    if raw_payload.get("state") == "running":
                        return ExistingMutationReservationLookupResult(
                            status="hold_unknown",
                            record=None,
                            observed_at=observed_at,
                        )
                    return ExistingMutationReservationLookupResult(
                        status="conflict",
                        record=None,
                        observed_at=observed_at,
                    )
                canonical_payload = record.model_dump(mode="json")
                integrity_fields = (
                    "record_id",
                    "scope",
                    "route_path",
                    "idempotency_key",
                    "request_fingerprint",
                    "state",
                    "lease_owner",
                    "lease_expires_at",
                    "attempt",
                    "reconciliation_key",
                    "provider_target_key",
                    "provider_effect_phase",
                    "provider_effect_started_at",
                    "created_at",
                    "updated_at",
                    "response_status_code",
                    "response_trace_id",
                    "recorded_at",
                    "response_payload",
                )
                row_projection = {
                    "record_id": row.record_id,
                    "scope": row.scope,
                    "route_path": row.route_path,
                    "idempotency_key": row.idempotency_key,
                    "request_fingerprint": row.request_fingerprint,
                    "state": row.state,
                    "lease_owner": row.lease_owner,
                    "lease_expires_at": row.lease_expires_at,
                    "attempt": row.attempt,
                    "reconciliation_key": row.reconciliation_key,
                    "provider_target_key": row.provider_target_key,
                    "created_at": row.created_at,
                    "updated_at": row.updated_at,
                    "response_status_code": row.response_status_code,
                    "response_trace_id": row.response_trace_id,
                    "recorded_at": row.recorded_at,
                }
                if any(
                    raw_payload.get(field_name) != canonical_payload[field_name]
                    for field_name in integrity_fields
                ) or any(
                    canonical_payload[field_name] != field_value
                    for field_name, field_value in row_projection.items()
                ):
                    return ExistingMutationReservationLookupResult(
                        status="conflict",
                        record=None,
                        observed_at=observed_at,
                    )
                records.append(record)
            matches = tuple(
                record
                for record in records
                if record.request_fingerprint == normalized_request_fingerprint
            )
            if len(matches) != len(records):
                return ExistingMutationReservationLookupResult(
                    status="conflict",
                    record=None,
                    observed_at=observed_at,
                )
            if len(matches) != 1:
                return ExistingMutationReservationLookupResult(
                    status="ambiguous",
                    record=None,
                    observed_at=observed_at,
                )
            return ExistingMutationReservationLookupResult(
                status="found",
                record=matches[0],
                observed_at=observed_at,
            )

    def prepare_db_only_mutation(
        self,
        *,
        scope: str,
        route_path: str,
        idempotency_key: str,
        request_fingerprint: str,
    ) -> DbOnlyMutationPreflightResult:
        normalized_scope = scope.strip()
        normalized_route_path = route_path.strip()
        normalized_idempotency_key = idempotency_key.strip()
        normalized_request_fingerprint = request_fingerprint.strip()
        if not all(
            (
                normalized_scope,
                normalized_route_path,
                normalized_idempotency_key,
                normalized_request_fingerprint,
            )
        ):
            raise ValueError("DB-only mutation preflight requires complete idempotency identity.")
        with self._session_factory() as session:
            self._begin_serialized_write(session)
            row = session.scalar(
                self._idempotency_statement(
                    scope=normalized_scope,
                    route_path=normalized_route_path,
                    idempotency_key=normalized_idempotency_key,
                    for_update=True,
                )
            )
            if row is None:
                return DbOnlyMutationPreflightResult(status="missing")
            current_record = self._read_payload(
                model_type=LaunchplaneIdempotencyRecord,
                payload=row.payload,
            )
            if current_record.request_fingerprint != normalized_request_fingerprint:
                return DbOnlyMutationPreflightResult(
                    status="conflict",
                    record=current_record,
                )
            if current_record.state == "completed":
                return DbOnlyMutationPreflightResult(
                    status="replayed",
                    record=current_record,
                )
            if current_record.state == "reconcile_required":
                return DbOnlyMutationPreflightResult(
                    status="reconcile_required",
                    record=current_record,
                )
            observed_at = self._database_mutation_timestamp(session)
            if parse_launchplane_mutation_timestamp(
                current_record.lease_expires_at,
                field_name="lease_expires_at",
            ) > parse_launchplane_mutation_timestamp(
                observed_at,
                field_name="observed_at",
            ):
                return DbOnlyMutationPreflightResult(
                    status="in_progress",
                    record=current_record,
                )
            if current_record.reconciliation_key:
                reconcile_record = self._updated_idempotency_record(
                    current_record,
                    state="reconcile_required",
                    updated_at=observed_at,
                )
                self._sync_idempotency_row(row, reconcile_record)
                session.commit()
                return DbOnlyMutationPreflightResult(
                    status="reconcile_required",
                    record=reconcile_record,
                )
            session.delete(row)
            session.commit()
            return DbOnlyMutationPreflightResult(
                status="released",
                record=current_record,
            )

    def reserve_mutation(
        self,
        *,
        scope: str,
        route_path: str,
        idempotency_key: str,
        request_fingerprint: str,
        lease_owner: str,
        lease_seconds: int = 300,
        reconciliation_key: str = "",
        provider_target_key: str = "",
    ) -> MutationReservationResult:
        return self._reserve_mutation(
            scope=scope,
            route_path=route_path,
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint,
            lease_owner=lease_owner,
            lease_seconds=lease_seconds,
            reconciliation_key=reconciliation_key,
            provider_target_key=provider_target_key,
            retry_missing_collision=True,
        )

    def _reserve_mutation(
        self,
        *,
        scope: str,
        route_path: str,
        idempotency_key: str,
        request_fingerprint: str,
        lease_owner: str,
        lease_seconds: int,
        reconciliation_key: str,
        provider_target_key: str,
        retry_missing_collision: bool,
    ) -> MutationReservationResult:
        normalized_scope = scope.strip()
        normalized_route_path = route_path.strip()
        normalized_idempotency_key = idempotency_key.strip()
        normalized_request_fingerprint = request_fingerprint.strip()
        normalized_reconciliation_key = reconciliation_key.strip()
        normalized_provider_target_key = (
            provider_target_key.strip() or normalized_reconciliation_key
        )
        if normalized_provider_target_key and not normalized_reconciliation_key:
            raise ValueError("Provider target keys require a reconciliation key.")
        insert_error: IntegrityError
        try:
            with self._session_factory() as session:
                self._begin_serialized_write(session)
                observed_at = self._database_mutation_timestamp(session)
                reservation = build_launchplane_mutation_reservation(
                    scope=scope,
                    route_path=route_path,
                    idempotency_key=idempotency_key,
                    request_fingerprint=request_fingerprint,
                    lease_owner=lease_owner,
                    lease_expires_at=self._mutation_lease_expiry(
                        observed_at=observed_at,
                        lease_seconds=lease_seconds,
                    ),
                    reserved_at=observed_at,
                    reconciliation_key=reconciliation_key,
                    provider_target_key=normalized_provider_target_key,
                )
                session.add(self._idempotency_row(reservation))
                session.commit()
            return MutationReservationResult(status="acquired", record=reservation)
        except IntegrityError as error:
            insert_error = error

        with self._session_factory() as session:
            self._begin_serialized_write(session)
            row = session.scalar(
                self._idempotency_statement(
                    scope=normalized_scope,
                    route_path=normalized_route_path,
                    idempotency_key=normalized_idempotency_key,
                    for_update=True,
                )
            )
            target_collision = False
            if row is None and normalized_provider_target_key:
                row = session.scalar(
                    self._active_provider_target_statement(
                        provider_target_key=normalized_provider_target_key,
                        for_update=True,
                    )
                )
                target_collision = row is not None
            if row is None:
                if retry_missing_collision:
                    session.rollback()
                    return self._reserve_mutation(
                        scope=scope,
                        route_path=route_path,
                        idempotency_key=idempotency_key,
                        request_fingerprint=request_fingerprint,
                        lease_owner=lease_owner,
                        lease_seconds=lease_seconds,
                        reconciliation_key=reconciliation_key,
                        provider_target_key=provider_target_key,
                        retry_missing_collision=False,
                    )
                raise insert_error
            current_record = self._read_payload(
                model_type=LaunchplaneIdempotencyRecord,
                payload=row.payload,
            )
            if target_collision:
                if current_record.state == "running":
                    observed_at = self._database_mutation_timestamp(session)
                    if parse_launchplane_mutation_timestamp(
                        current_record.lease_expires_at,
                        field_name="lease_expires_at",
                    ) <= parse_launchplane_mutation_timestamp(
                        observed_at,
                        field_name="observed_at",
                    ):
                        reconcile_record = self._updated_idempotency_record(
                            current_record,
                            state="reconcile_required",
                            updated_at=observed_at,
                            response_status_code=None,
                            response_trace_id="",
                            recorded_at="",
                            response_payload={},
                        )
                        self._sync_idempotency_row(row, reconcile_record)
                        session.commit()
                        return MutationReservationResult(
                            status="target_busy",
                            record=reconcile_record,
                        )
                return MutationReservationResult(status="target_busy", record=current_record)
            if current_record.request_fingerprint != normalized_request_fingerprint:
                return MutationReservationResult(status="conflict", record=current_record)
            if current_record.state == "completed":
                return MutationReservationResult(status="replayed", record=current_record)
            if current_record.state == "reconcile_required":
                return MutationReservationResult(
                    status="reconcile_required",
                    record=current_record,
                )
            observed_at = self._database_mutation_timestamp(session)
            if parse_launchplane_mutation_timestamp(
                current_record.lease_expires_at,
                field_name="lease_expires_at",
            ) > parse_launchplane_mutation_timestamp(
                observed_at,
                field_name="observed_at",
            ):
                return MutationReservationResult(status="in_progress", record=current_record)
            if current_record.reconciliation_key:
                reconcile_record = self._updated_idempotency_record(
                    current_record,
                    state="reconcile_required",
                    updated_at=observed_at,
                    response_status_code=None,
                    response_trace_id="",
                    recorded_at="",
                    response_payload={},
                )
                self._sync_idempotency_row(row, reconcile_record)
                session.commit()
                return MutationReservationResult(
                    status="reconcile_required",
                    record=reconcile_record,
                )
            reclaimed_record = self._updated_idempotency_record(
                current_record,
                state="running",
                lease_owner=lease_owner.strip(),
                lease_expires_at=self._mutation_lease_expiry(
                    observed_at=observed_at,
                    lease_seconds=lease_seconds,
                ),
                attempt=current_record.attempt + 1,
                reconciliation_key=reconciliation_key.strip(),
                provider_target_key=normalized_provider_target_key,
                updated_at=observed_at,
                response_status_code=None,
                response_trace_id="",
                recorded_at="",
                response_payload={},
            )
            self._sync_idempotency_row(row, reclaimed_record)
            session.commit()
            return MutationReservationResult(status="acquired", record=reclaimed_record)

    def release_mutation_reservation(
        self,
        *,
        reservation: LaunchplaneIdempotencyRecord,
    ) -> MutationReservationUpdateResult:
        if reservation.state != "running":
            raise ValueError("Mutation reservation release requires a running reservation.")
        with self._session_factory() as session:
            self._begin_serialized_write(session)
            row = session.scalar(
                self._idempotency_statement(
                    scope=reservation.scope,
                    route_path=reservation.route_path,
                    idempotency_key=reservation.idempotency_key,
                    for_update=True,
                )
            )
            if row is None:
                return MutationReservationUpdateResult(status="missing")
            current_record = self._read_payload(
                model_type=LaunchplaneIdempotencyRecord,
                payload=row.payload,
            )
            if current_record.state != "running":
                return MutationReservationUpdateResult(
                    status="not_running",
                    record=current_record,
                )
            if current_record.lease_owner != reservation.lease_owner:
                return MutationReservationUpdateResult(
                    status="owner_mismatch",
                    record=current_record,
                )
            if not self._mutation_reservation_matches(current_record, reservation):
                return MutationReservationUpdateResult(
                    status="reservation_mismatch",
                    record=current_record,
                )
            if current_record.reconciliation_key:
                return MutationReservationUpdateResult(
                    status="reconciliation_conflict",
                    record=current_record,
                )
            session.delete(row)
            session.commit()
            return MutationReservationUpdateResult(
                status="released",
                record=current_record,
            )

    def renew_mutation_reservation(
        self,
        *,
        reservation: LaunchplaneIdempotencyRecord,
        lease_seconds: int = 300,
    ) -> MutationReservationUpdateResult:
        if reservation.state != "running":
            raise ValueError("Mutation reservation renewal requires a running reservation.")
        with self._session_factory() as session:
            self._begin_serialized_write(session)
            row = session.scalar(
                self._idempotency_statement(
                    scope=reservation.scope,
                    route_path=reservation.route_path,
                    idempotency_key=reservation.idempotency_key,
                    for_update=True,
                )
            )
            if row is None:
                return MutationReservationUpdateResult(status="missing")
            current_record = self._read_payload(
                model_type=LaunchplaneIdempotencyRecord,
                payload=row.payload,
            )
            if current_record.state != "running":
                return MutationReservationUpdateResult(
                    status="not_running",
                    record=current_record,
                )
            if current_record.lease_owner != reservation.lease_owner:
                return MutationReservationUpdateResult(
                    status="owner_mismatch",
                    record=current_record,
                )
            if not self._mutation_reservation_matches(current_record, reservation):
                return MutationReservationUpdateResult(
                    status="reservation_mismatch",
                    record=current_record,
                )
            renewed_at = self._database_mutation_timestamp(session)
            if parse_launchplane_mutation_timestamp(
                current_record.lease_expires_at,
                field_name="lease_expires_at",
            ) <= parse_launchplane_mutation_timestamp(
                renewed_at,
                field_name="renewed_at",
            ):
                return MutationReservationUpdateResult(
                    status="lease_expired",
                    record=current_record,
                )
            renewed_record = self._updated_idempotency_record(
                current_record,
                lease_expires_at=self._mutation_lease_expiry(
                    observed_at=renewed_at,
                    lease_seconds=lease_seconds,
                ),
                updated_at=renewed_at,
            )
            self._sync_idempotency_row(row, renewed_record)
            session.commit()
            return MutationReservationUpdateResult(status="updated", record=renewed_record)

    def checkpoint_mutation_provider_effect(
        self,
        *,
        reservation: LaunchplaneIdempotencyRecord,
        effect_phase: str,
        lease_seconds: int = 300,
    ) -> MutationReservationUpdateResult:
        normalized_effect_phase = effect_phase.strip()
        if reservation.state != "running" or not normalized_effect_phase:
            raise ValueError("Provider effect checkpoints require a running reservation and phase.")
        if not reservation.reconciliation_key:
            raise ValueError("Provider effect checkpoints require a reconciliation key.")
        with self._session_factory() as session:
            self._begin_serialized_write(session)
            row = session.scalar(
                self._idempotency_statement(
                    scope=reservation.scope,
                    route_path=reservation.route_path,
                    idempotency_key=reservation.idempotency_key,
                    for_update=True,
                )
            )
            if row is None:
                return MutationReservationUpdateResult(status="missing")
            current_record = self._read_payload(
                model_type=LaunchplaneIdempotencyRecord,
                payload=row.payload,
            )
            if current_record.state != "running":
                return MutationReservationUpdateResult(
                    status="not_running",
                    record=current_record,
                )
            if current_record.lease_owner != reservation.lease_owner:
                return MutationReservationUpdateResult(
                    status="owner_mismatch",
                    record=current_record,
                )
            if not self._mutation_reservation_matches(current_record, reservation):
                return MutationReservationUpdateResult(
                    status="reservation_mismatch",
                    record=current_record,
                )
            checkpointed_at = self._database_mutation_timestamp(session)
            if parse_launchplane_mutation_timestamp(
                current_record.lease_expires_at,
                field_name="lease_expires_at",
            ) <= parse_launchplane_mutation_timestamp(
                checkpointed_at,
                field_name="checkpointed_at",
            ):
                return MutationReservationUpdateResult(
                    status="lease_expired",
                    record=current_record,
                )
            checkpointed_record = self._updated_idempotency_record(
                current_record,
                lease_expires_at=self._mutation_lease_expiry(
                    observed_at=checkpointed_at,
                    lease_seconds=lease_seconds,
                ),
                provider_effect_phase=normalized_effect_phase,
                provider_effect_started_at=checkpointed_at,
                updated_at=checkpointed_at,
            )
            self._sync_idempotency_row(row, checkpointed_record)
            session.commit()
            return MutationReservationUpdateResult(
                status="updated",
                record=checkpointed_record,
            )

    def bind_mutation_reconciliation_key(
        self,
        *,
        reservation: LaunchplaneIdempotencyRecord,
        reconciliation_key: str,
    ) -> MutationReservationUpdateResult:
        normalized_reconciliation_key = reconciliation_key.strip()
        if reservation.state != "running" or not normalized_reconciliation_key:
            raise ValueError(
                "Mutation reconciliation binding requires a running reservation and key."
            )
        with self._session_factory() as session:
            self._begin_serialized_write(session)
            row = session.scalar(
                self._idempotency_statement(
                    scope=reservation.scope,
                    route_path=reservation.route_path,
                    idempotency_key=reservation.idempotency_key,
                    for_update=True,
                )
            )
            if row is None:
                return MutationReservationUpdateResult(status="missing")
            current_record = self._read_payload(
                model_type=LaunchplaneIdempotencyRecord,
                payload=row.payload,
            )
            if current_record.state != "running":
                return MutationReservationUpdateResult(
                    status="not_running",
                    record=current_record,
                )
            if current_record.lease_owner != reservation.lease_owner:
                return MutationReservationUpdateResult(
                    status="owner_mismatch",
                    record=current_record,
                )
            if not self._mutation_reservation_matches(current_record, reservation):
                return MutationReservationUpdateResult(
                    status="reservation_mismatch",
                    record=current_record,
                )
            bound_at = self._database_mutation_timestamp(session)
            if parse_launchplane_mutation_timestamp(
                current_record.lease_expires_at,
                field_name="lease_expires_at",
            ) <= parse_launchplane_mutation_timestamp(
                bound_at,
                field_name="bound_at",
            ):
                return MutationReservationUpdateResult(
                    status="lease_expired",
                    record=current_record,
                )
            if (
                current_record.reconciliation_key
                and current_record.reconciliation_key != normalized_reconciliation_key
            ):
                return MutationReservationUpdateResult(
                    status="reconciliation_conflict",
                    record=current_record,
                )
            bound_record = self._updated_idempotency_record(
                current_record,
                reconciliation_key=normalized_reconciliation_key,
                updated_at=bound_at,
            )
            self._sync_idempotency_row(row, bound_record)
            session.commit()
            return MutationReservationUpdateResult(status="updated", record=bound_record)

    def mark_mutation_reconcile_required(
        self,
        *,
        reservation: LaunchplaneIdempotencyRecord,
        reconciliation_key: str,
    ) -> MutationReservationUpdateResult:
        normalized_reconciliation_key = reconciliation_key.strip()
        if (
            reservation.state not in {"running", "reconcile_required"}
            or not normalized_reconciliation_key
        ):
            raise ValueError(
                "Reconcile-required mutation transition requires a reservation and key."
            )
        with self._session_factory() as session:
            self._begin_serialized_write(session)
            row = session.scalar(
                self._idempotency_statement(
                    scope=reservation.scope,
                    route_path=reservation.route_path,
                    idempotency_key=reservation.idempotency_key,
                    for_update=True,
                )
            )
            if row is None:
                return MutationReservationUpdateResult(status="missing")
            current_record = self._read_payload(
                model_type=LaunchplaneIdempotencyRecord,
                payload=row.payload,
            )
            if current_record.lease_owner != reservation.lease_owner:
                return MutationReservationUpdateResult(
                    status="owner_mismatch",
                    record=current_record,
                )
            if not self._mutation_reservation_matches(current_record, reservation):
                return MutationReservationUpdateResult(
                    status="reservation_mismatch",
                    record=current_record,
                )
            if current_record.state == "reconcile_required":
                if current_record.reconciliation_key != normalized_reconciliation_key:
                    return MutationReservationUpdateResult(
                        status="reconciliation_conflict",
                        record=current_record,
                    )
                return MutationReservationUpdateResult(status="updated", record=current_record)
            if current_record.state != "running":
                return MutationReservationUpdateResult(
                    status="not_running",
                    record=current_record,
                )
            if (
                current_record.reconciliation_key
                and current_record.reconciliation_key != normalized_reconciliation_key
            ):
                return MutationReservationUpdateResult(
                    status="reconciliation_conflict",
                    record=current_record,
                )
            reconcile_record = self._updated_idempotency_record(
                current_record,
                state="reconcile_required",
                reconciliation_key=normalized_reconciliation_key,
                updated_at=self._database_mutation_timestamp(session),
                response_status_code=None,
                response_trace_id="",
                recorded_at="",
                response_payload={},
            )
            self._sync_idempotency_row(row, reconcile_record)
            session.commit()
            return MutationReservationUpdateResult(status="updated", record=reconcile_record)

    def complete_mutation_reservation(
        self,
        *,
        completion: LaunchplaneIdempotencyRecord,
    ) -> MutationReservationCompletionResult:
        if completion.state != "completed":
            raise ValueError("Mutation completion requires a completed reservation record.")
        with self._session_factory() as session:
            self._begin_serialized_write(session)
            row = session.scalar(
                self._idempotency_statement(
                    scope=completion.scope,
                    route_path=completion.route_path,
                    idempotency_key=completion.idempotency_key,
                    for_update=True,
                )
            )
            if row is None:
                return MutationReservationCompletionResult(status="missing")
            current_record = self._read_payload(
                model_type=LaunchplaneIdempotencyRecord,
                payload=row.payload,
            )
            if current_record.request_fingerprint != completion.request_fingerprint:
                return MutationReservationCompletionResult(
                    status="conflict",
                    record=current_record,
                )
            if current_record.state == "completed":
                return MutationReservationCompletionResult(
                    status="replayed",
                    record=current_record,
                )
            if current_record.lease_owner != completion.lease_owner:
                return MutationReservationCompletionResult(
                    status="owner_mismatch",
                    record=current_record,
                )
            if not self._mutation_reservation_matches(current_record, completion):
                return MutationReservationCompletionResult(
                    status="reservation_mismatch",
                    record=current_record,
                )
            if current_record.state == "reconcile_required":
                return MutationReservationCompletionResult(
                    status="reconcile_required",
                    record=current_record,
                )
            if current_record.state != "running":
                return MutationReservationCompletionResult(
                    status="not_running",
                    record=current_record,
                )
            completed_at = self._database_mutation_timestamp(session)
            if parse_launchplane_mutation_timestamp(
                current_record.lease_expires_at,
                field_name="lease_expires_at",
            ) <= parse_launchplane_mutation_timestamp(
                completed_at,
                field_name="completed_at",
            ):
                if current_record.reconciliation_key:
                    reconcile_record = self._updated_idempotency_record(
                        current_record,
                        state="reconcile_required",
                        updated_at=completed_at,
                    )
                    self._sync_idempotency_row(row, reconcile_record)
                    session.commit()
                    return MutationReservationCompletionResult(
                        status="reconcile_required",
                        record=reconcile_record,
                    )
                return MutationReservationCompletionResult(
                    status="lease_expired",
                    record=current_record,
                )
            stored_completion = self._updated_idempotency_record(
                completion,
                updated_at=completed_at,
                recorded_at=completed_at,
            )
            self._sync_idempotency_row(row, stored_completion)
            session.commit()
            return MutationReservationCompletionResult(
                status="completed",
                record=stored_completion,
            )

    def adopt_reconciled_mutation(
        self,
        *,
        reservation: LaunchplaneIdempotencyRecord,
        response_status_code: int,
        response_trace_id: str,
        response_payload: dict[str, Any],
    ) -> MutationReservationAdoptionResult:
        normalized_response_trace_id = response_trace_id.strip()
        if not reservation.reconciliation_key or not normalized_response_trace_id:
            raise ValueError(
                "Reconciled mutation adoption requires reservation evidence and trace."
            )
        with self._session_factory() as session:
            self._begin_serialized_write(session)
            row = session.scalar(
                self._idempotency_statement(
                    scope=reservation.scope,
                    route_path=reservation.route_path,
                    idempotency_key=reservation.idempotency_key,
                    for_update=True,
                )
            )
            if row is None:
                return MutationReservationAdoptionResult(status="missing")
            current_record = self._read_payload(
                model_type=LaunchplaneIdempotencyRecord,
                payload=row.payload,
            )
            if current_record.request_fingerprint != reservation.request_fingerprint:
                return MutationReservationAdoptionResult(
                    status="conflict",
                    record=current_record,
                )
            if current_record.state == "completed":
                return MutationReservationAdoptionResult(
                    status="replayed",
                    record=current_record,
                )
            if current_record.state != "reconcile_required":
                return MutationReservationAdoptionResult(
                    status="not_reconcile_required",
                    record=current_record,
                )
            if not self._mutation_reservation_matches(current_record, reservation):
                return MutationReservationAdoptionResult(
                    status="reservation_mismatch",
                    record=current_record,
                )
            adopted_at = self._database_mutation_timestamp(session)
            adopted_record = self._updated_idempotency_record(
                current_record,
                state="completed",
                updated_at=adopted_at,
                response_status_code=response_status_code,
                response_trace_id=normalized_response_trace_id,
                recorded_at=adopted_at,
                response_payload=response_payload,
            )
            self._sync_idempotency_row(row, adopted_record)
            session.commit()
            return MutationReservationAdoptionResult(
                status="adopted",
                record=adopted_record,
            )

    def supersede_expired_reconciled_mutation_and_reserve(
        self,
        *,
        reservation: LaunchplaneIdempotencyRecord,
        response_status_code: int,
        response_trace_id: str,
        response_payload: dict[str, Any],
        scope: str,
        route_path: str,
        idempotency_key: str,
        request_fingerprint: str,
        lease_owner: str,
        lease_seconds: int,
        minimum_expired_seconds: int,
        reconciliation_key: str,
        provider_target_key: str,
    ) -> MutationReconciliationSupersessionResult:
        normalized_response_trace_id = response_trace_id.strip()
        normalized_reconciliation_key = reconciliation_key.strip()
        normalized_provider_target_key = provider_target_key.strip()
        if minimum_expired_seconds < 0:
            raise ValueError("Reconciled mutation supersession delays cannot be negative.")
        if reservation.state != "reconcile_required" or not normalized_response_trace_id:
            raise ValueError(
                "Reconciled mutation supersession requires reservation evidence and trace."
            )
        if (
            not normalized_reconciliation_key
            or not normalized_provider_target_key
            or normalized_reconciliation_key != reservation.reconciliation_key
            or normalized_provider_target_key != reservation.provider_target_key
        ):
            raise ValueError(
                "Reconciled mutation supersession requires the same provider target identity."
            )
        try:
            with self._session_factory() as session:
                self._begin_serialized_write(session)
                row = session.scalar(
                    self._idempotency_statement(
                        scope=reservation.scope,
                        route_path=reservation.route_path,
                        idempotency_key=reservation.idempotency_key,
                        for_update=True,
                    )
                )
                if row is None:
                    return MutationReconciliationSupersessionResult(status="missing")
                current_record = self._read_payload(
                    model_type=LaunchplaneIdempotencyRecord,
                    payload=row.payload,
                )
                if current_record.state == "completed":
                    return MutationReconciliationSupersessionResult(status="retry")
                if current_record.state != "reconcile_required":
                    return MutationReconciliationSupersessionResult(
                        status="not_reconcile_required",
                        record=current_record,
                    )
                if not self._mutation_reservation_matches(current_record, reservation):
                    return MutationReconciliationSupersessionResult(
                        status="reservation_mismatch",
                        record=current_record,
                    )
                observed_at = self._database_mutation_timestamp(session)
                if not current_record.lease_expires_at or parse_launchplane_mutation_timestamp(
                    current_record.lease_expires_at,
                    field_name="lease_expires_at",
                ) > parse_launchplane_mutation_timestamp(
                    observed_at,
                    field_name="observed_at",
                ):
                    return MutationReconciliationSupersessionResult(
                        status="lease_active",
                        record=current_record,
                    )
                if parse_launchplane_mutation_timestamp(
                    current_record.lease_expires_at,
                    field_name="lease_expires_at",
                ) + timedelta(
                    seconds=minimum_expired_seconds
                ) > parse_launchplane_mutation_timestamp(
                    observed_at,
                    field_name="observed_at",
                ):
                    return MutationReconciliationSupersessionResult(
                        status="grace_active",
                        record=current_record,
                    )
                superseded_record = self._updated_idempotency_record(
                    current_record,
                    state="completed",
                    updated_at=observed_at,
                    response_status_code=response_status_code,
                    response_trace_id=normalized_response_trace_id,
                    recorded_at=observed_at,
                    response_payload=response_payload,
                )
                replacement = build_launchplane_mutation_reservation(
                    scope=scope,
                    route_path=route_path,
                    idempotency_key=idempotency_key,
                    request_fingerprint=request_fingerprint,
                    lease_owner=lease_owner,
                    lease_expires_at=self._mutation_lease_expiry(
                        observed_at=observed_at,
                        lease_seconds=lease_seconds,
                    ),
                    reserved_at=observed_at,
                    reconciliation_key=normalized_reconciliation_key,
                    provider_target_key=normalized_provider_target_key,
                )
                self._sync_idempotency_row(row, superseded_record)
                session.add(self._idempotency_row(replacement))
                session.commit()
                return MutationReconciliationSupersessionResult(
                    status="acquired",
                    record=replacement,
                )
        except IntegrityError:
            return MutationReconciliationSupersessionResult(status="retry")

    def release_reserved_mutation(
        self,
        *,
        reservation: LaunchplaneIdempotencyRecord,
    ) -> MutationReservationReleaseResult:
        if reservation.state != "running":
            raise ValueError("Only running mutation reservations can be released.")
        with self._session_factory() as session:
            self._begin_serialized_write(session)
            row = session.scalar(
                self._idempotency_statement(
                    scope=reservation.scope,
                    route_path=reservation.route_path,
                    idempotency_key=reservation.idempotency_key,
                    for_update=True,
                )
            )
            if row is None:
                return MutationReservationReleaseResult(status="missing")
            current_record = self._read_payload(
                model_type=LaunchplaneIdempotencyRecord,
                payload=row.payload,
            )
            if current_record.state != "running":
                return MutationReservationReleaseResult(
                    status="not_running",
                    record=current_record,
                )
            if current_record.lease_owner != reservation.lease_owner:
                return MutationReservationReleaseResult(
                    status="owner_mismatch",
                    record=current_record,
                )
            if not self._mutation_reservation_matches(current_record, reservation):
                return MutationReservationReleaseResult(
                    status="reservation_mismatch",
                    record=current_record,
                )
            session.delete(row)
            session.commit()
            return MutationReservationReleaseResult(
                status="released",
                record=current_record,
            )

    def retry_reconciled_mutation(
        self,
        *,
        reservation: LaunchplaneIdempotencyRecord,
        lease_owner: str,
        lease_seconds: int = 300,
    ) -> MutationReconciliationRetryResult:
        if reservation.state != "reconcile_required":
            raise ValueError("Reconciled mutation retry requires reconcile-required state.")
        normalized_lease_owner = lease_owner.strip()
        if not normalized_lease_owner:
            raise ValueError("Reconciled mutation retry requires a lease owner.")
        with self._session_factory() as session:
            self._begin_serialized_write(session)
            row = session.scalar(
                self._idempotency_statement(
                    scope=reservation.scope,
                    route_path=reservation.route_path,
                    idempotency_key=reservation.idempotency_key,
                    for_update=True,
                )
            )
            if row is None:
                return MutationReconciliationRetryResult(status="missing")
            current_record = self._read_payload(
                model_type=LaunchplaneIdempotencyRecord,
                payload=row.payload,
            )
            if current_record.request_fingerprint != reservation.request_fingerprint:
                return MutationReconciliationRetryResult(
                    status="conflict",
                    record=current_record,
                )
            if current_record.state == "completed":
                return MutationReconciliationRetryResult(
                    status="replayed",
                    record=current_record,
                )
            if current_record.state != "reconcile_required":
                return MutationReconciliationRetryResult(
                    status="not_reconcile_required",
                    record=current_record,
                )
            if not self._mutation_reservation_matches(current_record, reservation):
                return MutationReconciliationRetryResult(
                    status="reservation_mismatch",
                    record=current_record,
                )
            observed_at = self._database_mutation_timestamp(session)
            retried_record = self._updated_idempotency_record(
                current_record,
                state="running",
                lease_owner=normalized_lease_owner,
                lease_expires_at=self._mutation_lease_expiry(
                    observed_at=observed_at,
                    lease_seconds=lease_seconds,
                ),
                attempt=current_record.attempt + 1,
                provider_effect_phase="",
                provider_effect_started_at="",
                updated_at=observed_at,
                response_status_code=None,
                response_trace_id="",
                recorded_at="",
                response_payload={},
            )
            self._sync_idempotency_row(row, retried_record)
            session.commit()
            return MutationReconciliationRetryResult(
                status="acquired",
                record=retried_record,
            )

    def write_odoo_stable_bootstrap_operation_record(
        self, record: OdooStableBootstrapOperationRecord
    ) -> None:
        self._write_row(
            LaunchplaneOdooStableBootstrapOperationRow(
                operation_id=record.operation_id,
                product=record.product,
                context=record.context,
                instance=record.instance,
                idempotency_key=record.idempotency_key,
                status=record.status,
                phase=record.phase,
                created_at=record.created_at,
                updated_at=record.updated_at,
                lease_owner=record.lease_owner,
                lease_expires_at=record.lease_expires_at,
                heartbeat_at=record.heartbeat_at,
                attempt=record.attempt,
                payload=self._payload_dict(record),
            )
        )

    def _sync_odoo_stable_bootstrap_operation_row(
        self,
        row: LaunchplaneOdooStableBootstrapOperationRow,
        record: OdooStableBootstrapOperationRecord,
    ) -> None:
        row.product = record.product
        row.context = record.context
        row.instance = record.instance
        row.idempotency_key = record.idempotency_key
        row.status = record.status
        row.phase = record.phase
        row.created_at = record.created_at
        row.updated_at = record.updated_at
        row.lease_owner = record.lease_owner
        row.lease_expires_at = record.lease_expires_at
        row.heartbeat_at = record.heartbeat_at
        row.attempt = record.attempt
        row.payload = self._payload_dict(record)

    @staticmethod
    def _lock_odoo_stable_lane(
        session: Any,
        *,
        product: str,
        context: str,
        instance: str,
    ) -> None:
        dialect_name = session.get_bind().dialect.name
        if dialect_name == "postgresql":
            lane_key = "".join(f"{len(value)}:{value}" for value in (product, context, instance))
            session.execute(
                text(
                    "SELECT pg_advisory_xact_lock("
                    "hashtext('launchplane-odoo-stable-lane'), hashtext(:lane_key))"
                ),
                {"lane_key": lane_key},
            )
        elif dialect_name == "sqlite":
            session.execute(text("BEGIN IMMEDIATE"))

    @staticmethod
    def _active_odoo_stable_lane_operation_owner(
        session: Any,
        *,
        product: str,
        context: str,
        instance: str,
    ) -> OdooStableLaneOperationOwner | None:
        operation_tables: tuple[tuple[OdooStableLaneOperationKind, Any], ...] = (
            ("stable_bootstrap", LaunchplaneOdooStableBootstrapOperationRow),
            ("target_replacement", LaunchplaneOdooStableTargetReplacementOperationRow),
            ("prod_backup_restore", LaunchplaneOdooProdBackupRestoreOperationRow),
            (
                "retained_volume_backup_import",
                LaunchplaneOdooProdRetainedVolumeBackupImportOperationRow,
            ),
        )
        candidates: list[tuple[tuple[int, str, int, str], OdooStableLaneOperationOwner]] = []
        for operation_kind, operation_table in operation_tables:
            rows = session.execute(
                select(
                    operation_table.operation_id,
                    operation_table.status,
                    operation_table.created_at,
                ).where(
                    operation_table.product == product,
                    operation_table.context == context,
                    operation_table.instance == instance,
                    operation_table.status.in_(ODOO_STABLE_LANE_BLOCKING_STATUSES),
                )
            ).all()
            for operation_id, status, created_at in rows:
                owner = OdooStableLaneOperationOwner(
                    operation_kind=operation_kind,
                    operation_id=str(operation_id),
                )
                candidates.append(
                    (
                        odoo_stable_lane_operation_priority(
                            status=str(status),
                            created_at=str(created_at),
                            operation_kind=operation_kind,
                            operation_id=str(operation_id),
                        ),
                        owner,
                    )
                )
        if not candidates:
            return None
        return min(candidates, key=lambda candidate: candidate[0])[1]

    def create_odoo_stable_bootstrap_operation_record_if_no_active_lane(
        self, record: OdooStableBootstrapOperationRecord
    ) -> tuple[OdooStableBootstrapOperationRecord, bool]:
        with self._session_factory() as session:
            self._lock_odoo_stable_lane(
                session,
                product=record.product,
                context=record.context,
                instance=record.instance,
            )
            active_owner = self._active_odoo_stable_lane_operation_owner(
                session,
                product=record.product,
                context=record.context,
                instance=record.instance,
            )
            if active_owner is not None:
                if active_owner.operation_kind == "stable_bootstrap":
                    active_row = session.get(
                        LaunchplaneOdooStableBootstrapOperationRow,
                        active_owner.operation_id,
                    )
                    if active_row is None:
                        raise RuntimeError("Active Odoo stable bootstrap operation disappeared.")
                    return (
                        OdooStableBootstrapOperationRecord.model_validate(active_row.payload),
                        False,
                    )
                raise OdooStableLaneOperationConflictError(active_owner)
            session.add(
                LaunchplaneOdooStableBootstrapOperationRow(
                    operation_id=record.operation_id,
                    product=record.product,
                    context=record.context,
                    instance=record.instance,
                    idempotency_key=record.idempotency_key,
                    status=record.status,
                    phase=record.phase,
                    created_at=record.created_at,
                    updated_at=record.updated_at,
                    lease_owner=record.lease_owner,
                    lease_expires_at=record.lease_expires_at,
                    heartbeat_at=record.heartbeat_at,
                    attempt=record.attempt,
                    payload=self._payload_dict(record),
                )
            )
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                active_records = self.list_odoo_stable_bootstrap_operation_records(
                    product=record.product,
                    context_name=record.context,
                    instance_name=record.instance,
                    statuses=ODOO_STABLE_LANE_BLOCKING_STATUSES,
                    limit=1,
                )
                if active_records:
                    return active_records[0], False
                return self.create_odoo_stable_bootstrap_operation_record_if_no_active_lane(record)
        return record, True

    def read_odoo_stable_bootstrap_operation_record(
        self, operation_id: str
    ) -> OdooStableBootstrapOperationRecord:
        return self._read_model(
            model_type=OdooStableBootstrapOperationRecord,
            orm_model=LaunchplaneOdooStableBootstrapOperationRow,
            filters=(LaunchplaneOdooStableBootstrapOperationRow.operation_id == operation_id,),
        )

    def list_odoo_stable_bootstrap_operation_records(
        self,
        *,
        product: str = "",
        context_name: str = "",
        instance_name: str = "",
        idempotency_key: str = "",
        statuses: tuple[str, ...] = (),
        limit: int | None = None,
    ) -> tuple[OdooStableBootstrapOperationRecord, ...]:
        filters: list[object] = []
        if product:
            filters.append(LaunchplaneOdooStableBootstrapOperationRow.product == product)
        if context_name:
            filters.append(LaunchplaneOdooStableBootstrapOperationRow.context == context_name)
        if instance_name:
            filters.append(LaunchplaneOdooStableBootstrapOperationRow.instance == instance_name)
        if idempotency_key:
            filters.append(
                LaunchplaneOdooStableBootstrapOperationRow.idempotency_key == idempotency_key
            )
        if statuses:
            filters.append(LaunchplaneOdooStableBootstrapOperationRow.status.in_(statuses))
        return self._list_models(
            model_type=OdooStableBootstrapOperationRecord,
            orm_model=LaunchplaneOdooStableBootstrapOperationRow,
            filters=filters,
            order_by=(
                LaunchplaneOdooStableBootstrapOperationRow.updated_at.desc(),
                LaunchplaneOdooStableBootstrapOperationRow.operation_id.desc(),
            ),
            limit=limit,
        )

    def cancel_pending_odoo_stable_bootstrap_operation_record(
        self, record: OdooStableBootstrapOperationRecord
    ) -> bool:
        if record.status != "cancelled" or record.phase != "cancelled":
            raise ValueError("Odoo stable bootstrap cancellation requires a cancelled record.")
        with self._session_factory() as session:
            self._begin_serialized_write(session)
            statement = (
                select(LaunchplaneOdooStableBootstrapOperationRow)
                .where(
                    LaunchplaneOdooStableBootstrapOperationRow.operation_id == record.operation_id
                )
                .limit(1)
            )
            if not self.database_url.startswith("sqlite"):
                statement = statement.with_for_update()
            row = session.scalar(statement)
            if row is None:
                raise FileNotFoundError(record.operation_id)
            current_record = self._read_payload(
                model_type=OdooStableBootstrapOperationRecord,
                payload=row.payload,
            )
            if not odoo_stable_lane_cancellation_is_allowed(
                current_status=current_record.status,
                reconciliation_required_at=current_record.updated_at,
                cancellation=record.cancellation,
            ):
                return False
            self._sync_odoo_stable_bootstrap_operation_row(row, record)
            session.commit()
            return True

    def claim_next_odoo_stable_bootstrap_operation_record(
        self,
        *,
        lease_owner: str,
        lease_expires_at: str,
        claimed_at: str,
    ) -> OdooStableBootstrapOperationRecord | None:
        normalized_lease_owner = lease_owner.strip()
        if not normalized_lease_owner:
            raise ValueError("Odoo stable bootstrap operation claim requires lease_owner.")
        if not lease_expires_at.strip():
            raise ValueError("Odoo stable bootstrap operation claim requires lease_expires_at.")
        if not claimed_at.strip():
            raise ValueError("Odoo stable bootstrap operation claim requires claimed_at.")
        statement = (
            select(LaunchplaneOdooStableBootstrapOperationRow)
            .where(LaunchplaneOdooStableBootstrapOperationRow.status == "pending")
            .order_by(
                LaunchplaneOdooStableBootstrapOperationRow.created_at.asc(),
                LaunchplaneOdooStableBootstrapOperationRow.operation_id.asc(),
            )
        )
        if not self.database_url.startswith("sqlite"):
            statement = statement.with_for_update(skip_locked=True)
        with self._session_factory() as session:
            if self.database_url.startswith("sqlite"):
                self._lock_odoo_stable_lane(
                    session,
                    product="",
                    context="",
                    instance="",
                )
            for row in session.scalars(statement).all():
                record = self._read_payload(
                    model_type=OdooStableBootstrapOperationRecord,
                    payload=row.payload,
                )
                if not self.database_url.startswith("sqlite"):
                    self._lock_odoo_stable_lane(
                        session,
                        product=record.product,
                        context=record.context,
                        instance=record.instance,
                    )
                active_owner = self._active_odoo_stable_lane_operation_owner(
                    session,
                    product=record.product,
                    context=record.context,
                    instance=record.instance,
                )
                if active_owner != OdooStableLaneOperationOwner(
                    operation_kind="stable_bootstrap",
                    operation_id=record.operation_id,
                ):
                    continue
                claimed_record = record.model_copy(
                    update={
                        "status": "running",
                        "phase": "running",
                        "started_at": record.started_at or claimed_at,
                        "updated_at": claimed_at,
                        "lease_owner": normalized_lease_owner,
                        "lease_expires_at": lease_expires_at.strip(),
                        "heartbeat_at": claimed_at.strip(),
                        "attempt": record.attempt + 1,
                    }
                )
                self._sync_odoo_stable_bootstrap_operation_row(row, claimed_record)
                session.commit()
                return claimed_record
            return None

    def heartbeat_odoo_stable_bootstrap_operation_record(
        self,
        *,
        operation_id: str,
        lease_owner: str,
        heartbeat_at: str,
        lease_expires_at: str,
    ) -> bool:
        with self._session_factory() as session:
            self._begin_serialized_write(session)
            statement = (
                select(LaunchplaneOdooStableBootstrapOperationRow)
                .where(LaunchplaneOdooStableBootstrapOperationRow.operation_id == operation_id)
                .limit(1)
            )
            if not self.database_url.startswith("sqlite"):
                statement = statement.with_for_update()
            row = session.scalar(statement)
            if row is None:
                raise FileNotFoundError(operation_id)
            record = self._read_payload(
                model_type=OdooStableBootstrapOperationRecord,
                payload=row.payload,
            )
            if (
                record.status != "running"
                or record.lease_owner != lease_owner.strip()
                or not record.lease_expires_at
                or record.lease_expires_at <= heartbeat_at.strip()
            ):
                return False
            heartbeat_record = record.model_copy(
                update={
                    "heartbeat_at": heartbeat_at.strip(),
                    "lease_expires_at": lease_expires_at.strip(),
                    "updated_at": heartbeat_at.strip(),
                }
            )
            self._sync_odoo_stable_bootstrap_operation_row(row, heartbeat_record)
            session.commit()
            return True

    def complete_odoo_stable_bootstrap_operation_record(
        self,
        *,
        record: OdooStableBootstrapOperationRecord,
        lease_owner: str,
    ) -> bool:
        with self._session_factory() as session:
            self._begin_serialized_write(session)
            statement = (
                select(LaunchplaneOdooStableBootstrapOperationRow)
                .where(
                    LaunchplaneOdooStableBootstrapOperationRow.operation_id == record.operation_id
                )
                .limit(1)
            )
            if not self.database_url.startswith("sqlite"):
                statement = statement.with_for_update()
            row = session.scalar(statement)
            if row is None:
                raise FileNotFoundError(record.operation_id)
            current_record = self._read_payload(
                model_type=OdooStableBootstrapOperationRecord,
                payload=row.payload,
            )
            completed_at = _utc_now_timestamp()
            if (
                current_record.status != "running"
                or current_record.lease_owner != lease_owner.strip()
                or not current_record.lease_expires_at
                or current_record.lease_expires_at <= completed_at
            ):
                return False
            self._sync_odoo_stable_bootstrap_operation_row(row, record)
            session.commit()
            return True

    def recover_expired_odoo_stable_bootstrap_operation_records(
        self,
        *,
        now: str,
        safe_phases: tuple[str, ...],
        max_attempts: int,
    ) -> tuple[str, ...]:
        filters = (
            LaunchplaneOdooStableBootstrapOperationRow.status == "running",
            (
                (LaunchplaneOdooStableBootstrapOperationRow.lease_expires_at == "")
                | (LaunchplaneOdooStableBootstrapOperationRow.lease_expires_at < now)
            ),
        )
        statement = select(LaunchplaneOdooStableBootstrapOperationRow).where(*filters)
        if not self.database_url.startswith("sqlite"):
            statement = statement.with_for_update(skip_locked=True)
        affected_operation_ids: list[str] = []
        with self._session_factory() as session:
            self._begin_serialized_write(session)
            rows = session.scalars(statement).all()
            for row in rows:
                record = self._read_payload(
                    model_type=OdooStableBootstrapOperationRecord,
                    payload=row.payload,
                )
                affected_operation_ids.append(record.operation_id)
                if record.phase in safe_phases and record.attempt < max_attempts:
                    recovered_record = record.model_copy(
                        update={
                            "status": "pending",
                            "started_at": "",
                            "updated_at": now,
                            "lease_owner": "",
                            "lease_expires_at": "",
                            "heartbeat_at": "",
                        }
                    )
                elif record.phase in safe_phases:
                    recovered_record = record.model_copy(
                        update={
                            "status": "fail",
                            "phase": "failed",
                            "updated_at": now,
                            "finished_at": now,
                            "lease_owner": "",
                            "lease_expires_at": "",
                            "heartbeat_at": "",
                            "error_code": "operation_attempts_exhausted",
                            "error_message": (
                                "Odoo stable bootstrap operation exhausted automatic attempts "
                                f"in safe phase {record.phase!r}."
                            ),
                        }
                    )
                else:
                    recovered_record = record.model_copy(
                        update={
                            "status": "reconciliation_required",
                            "updated_at": now,
                            "lease_owner": "",
                            "lease_expires_at": "",
                            "heartbeat_at": "",
                            "error_code": "operation_reconciliation_required",
                            "error_message": (
                                f"Odoo stable bootstrap operation lease expired in "
                                f"phase {record.phase!r}; provider state requires operator "
                                "reconciliation before the lane can be released."
                            ),
                        }
                    )
                self._sync_odoo_stable_bootstrap_operation_row(row, recovered_record)
            session.commit()
        return tuple(affected_operation_ids)

    def write_odoo_stable_target_replacement_operation_record(
        self, record: OdooStableTargetReplacementOperationRecord
    ) -> None:
        self._write_row(
            LaunchplaneOdooStableTargetReplacementOperationRow(
                operation_id=record.operation_id,
                product=record.product,
                context=record.context,
                instance=record.instance,
                idempotency_key=record.idempotency_key,
                idempotency_scope=record.idempotency_scope,
                status=record.status,
                phase=record.phase,
                created_at=record.created_at,
                updated_at=record.updated_at,
                lease_owner=record.lease_owner,
                lease_expires_at=record.lease_expires_at,
                heartbeat_at=record.heartbeat_at,
                attempt=record.attempt,
                payload=self._payload_dict(record),
            )
        )

    def _sync_odoo_stable_target_replacement_operation_row(
        self,
        row: LaunchplaneOdooStableTargetReplacementOperationRow,
        record: OdooStableTargetReplacementOperationRecord,
    ) -> None:
        row.product = record.product
        row.context = record.context
        row.instance = record.instance
        row.idempotency_key = record.idempotency_key
        row.idempotency_scope = record.idempotency_scope
        row.status = record.status
        row.phase = record.phase
        row.created_at = record.created_at
        row.updated_at = record.updated_at
        row.lease_owner = record.lease_owner
        row.lease_expires_at = record.lease_expires_at
        row.heartbeat_at = record.heartbeat_at
        row.attempt = record.attempt
        row.payload = self._payload_dict(record)

    def create_odoo_stable_target_replacement_operation_record_if_no_active_lane(
        self, record: OdooStableTargetReplacementOperationRecord
    ) -> tuple[OdooStableTargetReplacementOperationRecord, bool]:
        with self._session_factory() as session:
            self._lock_odoo_stable_lane(
                session,
                product=record.product,
                context=record.context,
                instance=record.instance,
            )
            active_owner = self._active_odoo_stable_lane_operation_owner(
                session,
                product=record.product,
                context=record.context,
                instance=record.instance,
            )
            if active_owner is not None:
                if active_owner.operation_kind == "target_replacement":
                    active_row = session.get(
                        LaunchplaneOdooStableTargetReplacementOperationRow,
                        active_owner.operation_id,
                    )
                    if active_row is None:
                        raise RuntimeError("Active Odoo target replacement operation disappeared.")
                    return (
                        OdooStableTargetReplacementOperationRecord.model_validate(
                            active_row.payload
                        ),
                        False,
                    )
                raise OdooStableLaneOperationConflictError(active_owner)
            session.add(
                LaunchplaneOdooStableTargetReplacementOperationRow(
                    operation_id=record.operation_id,
                    product=record.product,
                    context=record.context,
                    instance=record.instance,
                    idempotency_key=record.idempotency_key,
                    idempotency_scope=record.idempotency_scope,
                    status=record.status,
                    phase=record.phase,
                    created_at=record.created_at,
                    updated_at=record.updated_at,
                    lease_owner=record.lease_owner,
                    lease_expires_at=record.lease_expires_at,
                    heartbeat_at=record.heartbeat_at,
                    attempt=record.attempt,
                    payload=self._payload_dict(record),
                )
            )
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                active_records = self.list_odoo_stable_target_replacement_operation_records(
                    product=record.product,
                    context_name=record.context,
                    instance_name=record.instance,
                    statuses=ODOO_STABLE_LANE_BLOCKING_STATUSES,
                    limit=1,
                )
                if active_records:
                    return active_records[0], False
                return (
                    self.create_odoo_stable_target_replacement_operation_record_if_no_active_lane(
                        record
                    )
                )
        return record, True

    def read_odoo_stable_target_replacement_operation_record(
        self, operation_id: str
    ) -> OdooStableTargetReplacementOperationRecord:
        return self._read_model(
            model_type=OdooStableTargetReplacementOperationRecord,
            orm_model=LaunchplaneOdooStableTargetReplacementOperationRow,
            filters=(
                LaunchplaneOdooStableTargetReplacementOperationRow.operation_id == operation_id,
            ),
        )

    def list_odoo_stable_target_replacement_operation_records(
        self,
        *,
        product: str = "",
        context_name: str = "",
        instance_name: str = "",
        idempotency_key: str = "",
        idempotency_scope: str = "",
        statuses: tuple[str, ...] = (),
        limit: int | None = None,
    ) -> tuple[OdooStableTargetReplacementOperationRecord, ...]:
        filters: list[object] = []
        if product:
            filters.append(LaunchplaneOdooStableTargetReplacementOperationRow.product == product)
        if context_name:
            filters.append(
                LaunchplaneOdooStableTargetReplacementOperationRow.context == context_name
            )
        if instance_name:
            filters.append(
                LaunchplaneOdooStableTargetReplacementOperationRow.instance == instance_name
            )
        if idempotency_key:
            filters.append(
                LaunchplaneOdooStableTargetReplacementOperationRow.idempotency_key
                == idempotency_key
            )
        if idempotency_scope:
            filters.append(
                LaunchplaneOdooStableTargetReplacementOperationRow.idempotency_scope
                == idempotency_scope
            )
        if statuses:
            filters.append(LaunchplaneOdooStableTargetReplacementOperationRow.status.in_(statuses))
        return self._list_models(
            model_type=OdooStableTargetReplacementOperationRecord,
            orm_model=LaunchplaneOdooStableTargetReplacementOperationRow,
            filters=filters,
            order_by=(
                LaunchplaneOdooStableTargetReplacementOperationRow.updated_at.desc(),
                LaunchplaneOdooStableTargetReplacementOperationRow.operation_id.desc(),
            ),
            limit=limit,
        )

    def cancel_pending_odoo_stable_target_replacement_operation_record(
        self, record: OdooStableTargetReplacementOperationRecord
    ) -> bool:
        if record.status != "cancelled" or record.phase != "cancelled":
            raise ValueError("Odoo target replacement cancellation requires a cancelled record.")
        with self._session_factory() as session:
            self._begin_serialized_write(session)
            statement = (
                select(LaunchplaneOdooStableTargetReplacementOperationRow)
                .where(
                    LaunchplaneOdooStableTargetReplacementOperationRow.operation_id
                    == record.operation_id
                )
                .limit(1)
            )
            if not self.database_url.startswith("sqlite"):
                statement = statement.with_for_update()
            row = session.scalar(statement)
            if row is None:
                raise FileNotFoundError(record.operation_id)
            current_record = self._read_payload(
                model_type=OdooStableTargetReplacementOperationRecord,
                payload=row.payload,
            )
            if not odoo_stable_lane_cancellation_is_allowed(
                current_status=current_record.status,
                reconciliation_required_at=current_record.updated_at,
                cancellation=record.cancellation,
            ):
                return False
            self._sync_odoo_stable_target_replacement_operation_row(row, record)
            session.commit()
            return True

    def claim_next_odoo_stable_target_replacement_operation_record(
        self,
        *,
        lease_owner: str,
        lease_expires_at: str,
        claimed_at: str,
    ) -> OdooStableTargetReplacementOperationRecord | None:
        normalized_lease_owner = lease_owner.strip()
        if not normalized_lease_owner:
            raise ValueError("Odoo stable target replacement claim requires lease_owner.")
        if not lease_expires_at.strip():
            raise ValueError("Odoo stable target replacement claim requires lease_expires_at.")
        if not claimed_at.strip():
            raise ValueError("Odoo stable target replacement claim requires claimed_at.")
        statement = (
            select(LaunchplaneOdooStableTargetReplacementOperationRow)
            .where(LaunchplaneOdooStableTargetReplacementOperationRow.status == "pending")
            .order_by(
                LaunchplaneOdooStableTargetReplacementOperationRow.created_at.asc(),
                LaunchplaneOdooStableTargetReplacementOperationRow.operation_id.asc(),
            )
        )
        if not self.database_url.startswith("sqlite"):
            statement = statement.with_for_update(skip_locked=True)
        with self._session_factory() as session:
            if self.database_url.startswith("sqlite"):
                self._lock_odoo_stable_lane(
                    session,
                    product="",
                    context="",
                    instance="",
                )
            for row in session.scalars(statement).all():
                record = self._read_payload(
                    model_type=OdooStableTargetReplacementOperationRecord,
                    payload=row.payload,
                )
                if not self.database_url.startswith("sqlite"):
                    self._lock_odoo_stable_lane(
                        session,
                        product=record.product,
                        context=record.context,
                        instance=record.instance,
                    )
                active_owner = self._active_odoo_stable_lane_operation_owner(
                    session,
                    product=record.product,
                    context=record.context,
                    instance=record.instance,
                )
                if active_owner != OdooStableLaneOperationOwner(
                    operation_kind="target_replacement",
                    operation_id=record.operation_id,
                ):
                    continue
                claimed_record = record.model_copy(
                    update={
                        "status": "running",
                        "phase": "running",
                        "started_at": record.started_at or claimed_at,
                        "updated_at": claimed_at,
                        "lease_owner": normalized_lease_owner,
                        "lease_expires_at": lease_expires_at.strip(),
                        "heartbeat_at": claimed_at.strip(),
                        "attempt": record.attempt + 1,
                    }
                )
                self._sync_odoo_stable_target_replacement_operation_row(row, claimed_record)
                session.commit()
                return claimed_record
            return None

    def heartbeat_odoo_stable_target_replacement_operation_record(
        self,
        *,
        operation_id: str,
        lease_owner: str,
        heartbeat_at: str,
        lease_expires_at: str,
    ) -> bool:
        with self._session_factory() as session:
            self._begin_serialized_write(session)
            statement = (
                select(LaunchplaneOdooStableTargetReplacementOperationRow)
                .where(
                    LaunchplaneOdooStableTargetReplacementOperationRow.operation_id == operation_id
                )
                .limit(1)
            )
            if not self.database_url.startswith("sqlite"):
                statement = statement.with_for_update()
            row = session.scalar(statement)
            if row is None:
                raise FileNotFoundError(operation_id)
            record = self._read_payload(
                model_type=OdooStableTargetReplacementOperationRecord,
                payload=row.payload,
            )
            if (
                record.status != "running"
                or record.lease_owner != lease_owner.strip()
                or not record.lease_expires_at
                or record.lease_expires_at <= heartbeat_at.strip()
            ):
                return False
            heartbeat_record = record.model_copy(
                update={
                    "heartbeat_at": heartbeat_at.strip(),
                    "lease_expires_at": lease_expires_at.strip(),
                    "updated_at": heartbeat_at.strip(),
                }
            )
            self._sync_odoo_stable_target_replacement_operation_row(row, heartbeat_record)
            session.commit()
            return True

    def complete_odoo_stable_target_replacement_operation_record(
        self,
        *,
        record: OdooStableTargetReplacementOperationRecord,
        lease_owner: str,
    ) -> bool:
        with self._session_factory() as session:
            self._begin_serialized_write(session)
            statement = (
                select(LaunchplaneOdooStableTargetReplacementOperationRow)
                .where(
                    LaunchplaneOdooStableTargetReplacementOperationRow.operation_id
                    == record.operation_id
                )
                .limit(1)
            )
            if not self.database_url.startswith("sqlite"):
                statement = statement.with_for_update()
            row = session.scalar(statement)
            if row is None:
                raise FileNotFoundError(record.operation_id)
            current_record = self._read_payload(
                model_type=OdooStableTargetReplacementOperationRecord,
                payload=row.payload,
            )
            completed_at = _utc_now_timestamp()
            if (
                current_record.status != "running"
                or current_record.lease_owner != lease_owner.strip()
                or not current_record.lease_expires_at
                or current_record.lease_expires_at <= completed_at
            ):
                return False
            self._sync_odoo_stable_target_replacement_operation_row(row, record)
            session.commit()
            return True

    def recover_expired_odoo_stable_target_replacement_operation_records(
        self,
        *,
        now: str,
        safe_phases: tuple[str, ...],
        max_attempts: int,
    ) -> tuple[str, ...]:
        filters = (
            LaunchplaneOdooStableTargetReplacementOperationRow.status == "running",
            (
                (LaunchplaneOdooStableTargetReplacementOperationRow.lease_expires_at == "")
                | (LaunchplaneOdooStableTargetReplacementOperationRow.lease_expires_at < now)
            ),
        )
        statement = select(LaunchplaneOdooStableTargetReplacementOperationRow).where(*filters)
        if not self.database_url.startswith("sqlite"):
            statement = statement.with_for_update(skip_locked=True)
        affected_operation_ids: list[str] = []
        with self._session_factory() as session:
            self._begin_serialized_write(session)
            rows = session.scalars(statement).all()
            for row in rows:
                record = self._read_payload(
                    model_type=OdooStableTargetReplacementOperationRecord,
                    payload=row.payload,
                )
                affected_operation_ids.append(record.operation_id)
                if record.phase in safe_phases and record.attempt < max_attempts:
                    recovered_record = record.model_copy(
                        update={
                            "status": "pending",
                            "started_at": "",
                            "updated_at": now,
                            "lease_owner": "",
                            "lease_expires_at": "",
                            "heartbeat_at": "",
                        }
                    )
                elif record.phase in safe_phases:
                    recovered_record = record.model_copy(
                        update={
                            "status": "fail",
                            "phase": "failed",
                            "updated_at": now,
                            "finished_at": now,
                            "lease_owner": "",
                            "lease_expires_at": "",
                            "heartbeat_at": "",
                            "error_code": "operation_attempts_exhausted",
                            "error_message": (
                                "Odoo stable target replacement operation exhausted automatic "
                                f"attempts in safe phase {record.phase!r}."
                            ),
                        }
                    )
                else:
                    recovered_record = record.model_copy(
                        update={
                            "status": "reconciliation_required",
                            "updated_at": now,
                            "lease_owner": "",
                            "lease_expires_at": "",
                            "heartbeat_at": "",
                            "error_code": "operation_reconciliation_required",
                            "error_message": (
                                f"Odoo stable target replacement operation lease expired in "
                                f"phase {record.phase!r}; provider state requires operator "
                                "reconciliation before the lane can be released."
                            ),
                        }
                    )
                self._sync_odoo_stable_target_replacement_operation_row(row, recovered_record)
            session.commit()
        return tuple(affected_operation_ids)

    def write_odoo_prod_backup_restore_operation_record(
        self, record: OdooProdBackupRestoreOperationRecord
    ) -> None:
        self._write_row(
            LaunchplaneOdooProdBackupRestoreOperationRow(
                operation_id=record.operation_id,
                product=record.product,
                context=record.context,
                instance=record.instance,
                idempotency_key=record.idempotency_key,
                idempotency_scope=record.idempotency_scope,
                status=record.status,
                phase=record.phase,
                created_at=record.created_at,
                updated_at=record.updated_at,
                lease_owner=record.lease_owner,
                lease_expires_at=record.lease_expires_at,
                heartbeat_at=record.heartbeat_at,
                attempt=record.attempt,
                payload=self._payload_dict(record),
            )
        )

    def _sync_odoo_prod_backup_restore_operation_row(
        self,
        row: LaunchplaneOdooProdBackupRestoreOperationRow,
        record: OdooProdBackupRestoreOperationRecord,
    ) -> None:
        row.product = record.product
        row.context = record.context
        row.instance = record.instance
        row.idempotency_key = record.idempotency_key
        row.idempotency_scope = record.idempotency_scope
        row.status = record.status
        row.phase = record.phase
        row.created_at = record.created_at
        row.updated_at = record.updated_at
        row.lease_owner = record.lease_owner
        row.lease_expires_at = record.lease_expires_at
        row.heartbeat_at = record.heartbeat_at
        row.attempt = record.attempt
        row.payload = self._payload_dict(record)

    def create_odoo_prod_backup_restore_operation_record_if_no_active_lane(
        self, record: OdooProdBackupRestoreOperationRecord
    ) -> tuple[OdooProdBackupRestoreOperationRecord, bool]:
        with self._session_factory() as session:
            self._lock_odoo_stable_lane(
                session,
                product=record.product,
                context=record.context,
                instance=record.instance,
            )
            active_owner = self._active_odoo_stable_lane_operation_owner(
                session,
                product=record.product,
                context=record.context,
                instance=record.instance,
            )
            if active_owner is not None:
                if active_owner.operation_kind == "prod_backup_restore":
                    active_row = session.get(
                        LaunchplaneOdooProdBackupRestoreOperationRow,
                        active_owner.operation_id,
                    )
                    if active_row is None:
                        raise RuntimeError("Active Odoo backup restore operation disappeared.")
                    return (
                        OdooProdBackupRestoreOperationRecord.model_validate(active_row.payload),
                        False,
                    )
                raise OdooStableLaneOperationConflictError(active_owner)
            session.add(
                LaunchplaneOdooProdBackupRestoreOperationRow(
                    operation_id=record.operation_id,
                    product=record.product,
                    context=record.context,
                    instance=record.instance,
                    idempotency_key=record.idempotency_key,
                    idempotency_scope=record.idempotency_scope,
                    status=record.status,
                    phase=record.phase,
                    created_at=record.created_at,
                    updated_at=record.updated_at,
                    lease_owner=record.lease_owner,
                    lease_expires_at=record.lease_expires_at,
                    heartbeat_at=record.heartbeat_at,
                    attempt=record.attempt,
                    payload=self._payload_dict(record),
                )
            )
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                active_records = self.list_odoo_prod_backup_restore_operation_records(
                    product=record.product,
                    context_name=record.context,
                    instance_name=record.instance,
                    statuses=ODOO_STABLE_LANE_BLOCKING_STATUSES,
                    limit=1,
                )
                if active_records:
                    return active_records[0], False
                return self.create_odoo_prod_backup_restore_operation_record_if_no_active_lane(
                    record
                )
        return record, True

    def requeue_terminal_failed_odoo_prod_backup_restore_operation_record(
        self,
        *,
        operation_id: str,
        queued_at: str,
        authorization: DurableOperationAuthorization,
    ) -> OdooProdBackupRestoreOperationRecord | None:
        existing_record = self.read_odoo_prod_backup_restore_operation_record(operation_id.strip())
        with self._session_factory() as session:
            self._lock_odoo_stable_lane(
                session,
                product=existing_record.product,
                context=existing_record.context,
                instance=existing_record.instance,
            )
            statement = (
                select(LaunchplaneOdooProdBackupRestoreOperationRow)
                .where(
                    LaunchplaneOdooProdBackupRestoreOperationRow.operation_id
                    == existing_record.operation_id
                )
                .limit(1)
            )
            if not self.database_url.startswith("sqlite"):
                statement = statement.with_for_update()
            row = session.scalar(statement)
            if row is None:
                raise FileNotFoundError(operation_id)
            record = self._read_payload(
                model_type=OdooProdBackupRestoreOperationRecord,
                payload=row.payload,
            )
            active_owner = self._active_odoo_stable_lane_operation_owner(
                session,
                product=record.product,
                context=record.context,
                instance=record.instance,
            )
            if active_owner is not None or record.status != "fail":
                return None
            requeued_record = requeue_odoo_prod_backup_restore_verification_replay(
                operation=record,
                queued_at=queued_at,
                authorization=authorization,
            )
            self._sync_odoo_prod_backup_restore_operation_row(row, requeued_record)
            session.commit()
            return requeued_record

    def read_odoo_prod_backup_restore_operation_record(
        self, operation_id: str
    ) -> OdooProdBackupRestoreOperationRecord:
        return self._read_model(
            model_type=OdooProdBackupRestoreOperationRecord,
            orm_model=LaunchplaneOdooProdBackupRestoreOperationRow,
            filters=(LaunchplaneOdooProdBackupRestoreOperationRow.operation_id == operation_id,),
        )

    def list_odoo_prod_backup_restore_operation_records(
        self,
        *,
        product: str = "",
        context_name: str = "",
        instance_name: str = "",
        idempotency_key: str = "",
        idempotency_scope: str = "",
        statuses: tuple[str, ...] = (),
        limit: int | None = None,
    ) -> tuple[OdooProdBackupRestoreOperationRecord, ...]:
        filters: list[object] = []
        if product:
            filters.append(LaunchplaneOdooProdBackupRestoreOperationRow.product == product)
        if context_name:
            filters.append(LaunchplaneOdooProdBackupRestoreOperationRow.context == context_name)
        if instance_name:
            filters.append(LaunchplaneOdooProdBackupRestoreOperationRow.instance == instance_name)
        if idempotency_key:
            filters.append(
                LaunchplaneOdooProdBackupRestoreOperationRow.idempotency_key == idempotency_key
            )
        if idempotency_scope:
            filters.append(
                LaunchplaneOdooProdBackupRestoreOperationRow.idempotency_scope == idempotency_scope
            )
        if statuses:
            filters.append(LaunchplaneOdooProdBackupRestoreOperationRow.status.in_(statuses))
        return self._list_models(
            model_type=OdooProdBackupRestoreOperationRecord,
            orm_model=LaunchplaneOdooProdBackupRestoreOperationRow,
            filters=filters,
            order_by=(
                LaunchplaneOdooProdBackupRestoreOperationRow.updated_at.desc(),
                LaunchplaneOdooProdBackupRestoreOperationRow.operation_id.desc(),
            ),
            limit=limit,
        )

    def cancel_pending_odoo_prod_backup_restore_operation_record(
        self, record: OdooProdBackupRestoreOperationRecord
    ) -> bool:
        if record.status != "cancelled" or record.phase != "cancelled":
            raise ValueError("Odoo backup restore cancellation requires a cancelled record.")
        with self._session_factory() as session:
            self._begin_serialized_write(session)
            statement = (
                select(LaunchplaneOdooProdBackupRestoreOperationRow)
                .where(
                    LaunchplaneOdooProdBackupRestoreOperationRow.operation_id == record.operation_id
                )
                .limit(1)
            )
            if not self.database_url.startswith("sqlite"):
                statement = statement.with_for_update()
            row = session.scalar(statement)
            if row is None:
                raise FileNotFoundError(record.operation_id)
            current_record = self._read_payload(
                model_type=OdooProdBackupRestoreOperationRecord,
                payload=row.payload,
            )
            if not odoo_stable_lane_cancellation_is_allowed(
                current_status=current_record.status,
                reconciliation_required_at=current_record.updated_at,
                cancellation=record.cancellation,
            ):
                return False
            self._sync_odoo_prod_backup_restore_operation_row(row, record)
            session.commit()
            return True

    def claim_next_odoo_prod_backup_restore_operation_record(
        self,
        *,
        lease_owner: str,
        lease_expires_at: str,
        claimed_at: str,
    ) -> OdooProdBackupRestoreOperationRecord | None:
        normalized_lease_owner = lease_owner.strip()
        if not normalized_lease_owner or not lease_expires_at.strip() or not claimed_at.strip():
            raise ValueError("Odoo backup restore claim requires lease evidence.")
        statement = (
            select(LaunchplaneOdooProdBackupRestoreOperationRow)
            .where(LaunchplaneOdooProdBackupRestoreOperationRow.status == "pending")
            .order_by(
                LaunchplaneOdooProdBackupRestoreOperationRow.created_at.asc(),
                LaunchplaneOdooProdBackupRestoreOperationRow.operation_id.asc(),
            )
        )
        if not self.database_url.startswith("sqlite"):
            statement = statement.with_for_update(skip_locked=True)
        with self._session_factory() as session:
            if self.database_url.startswith("sqlite"):
                self._lock_odoo_stable_lane(
                    session,
                    product="",
                    context="",
                    instance="",
                )
            for row in session.scalars(statement).all():
                record = self._read_payload(
                    model_type=OdooProdBackupRestoreOperationRecord,
                    payload=row.payload,
                )
                if not self.database_url.startswith("sqlite"):
                    self._lock_odoo_stable_lane(
                        session,
                        product=record.product,
                        context=record.context,
                        instance=record.instance,
                    )
                active_owner = self._active_odoo_stable_lane_operation_owner(
                    session,
                    product=record.product,
                    context=record.context,
                    instance=record.instance,
                )
                if active_owner != OdooStableLaneOperationOwner(
                    operation_kind="prod_backup_restore",
                    operation_id=record.operation_id,
                ):
                    continue
                claimed_record = record.model_copy(
                    update={
                        "status": "running",
                        "phase": record.phase if record.checkpoints else "running",
                        "started_at": record.started_at or claimed_at,
                        "updated_at": claimed_at,
                        "lease_owner": normalized_lease_owner,
                        "lease_expires_at": lease_expires_at.strip(),
                        "heartbeat_at": claimed_at.strip(),
                        "attempt": record.attempt + 1,
                    }
                )
                self._sync_odoo_prod_backup_restore_operation_row(row, claimed_record)
                session.commit()
                return claimed_record
            return None

    def heartbeat_odoo_prod_backup_restore_operation_record(
        self,
        *,
        operation_id: str,
        lease_owner: str,
        heartbeat_at: str,
        lease_expires_at: str,
    ) -> bool:
        with self._session_factory() as session:
            self._begin_serialized_write(session)
            statement = (
                select(LaunchplaneOdooProdBackupRestoreOperationRow)
                .where(LaunchplaneOdooProdBackupRestoreOperationRow.operation_id == operation_id)
                .limit(1)
            )
            if not self.database_url.startswith("sqlite"):
                statement = statement.with_for_update()
            row = session.scalar(statement)
            if row is None:
                raise FileNotFoundError(operation_id)
            record = self._read_payload(
                model_type=OdooProdBackupRestoreOperationRecord,
                payload=row.payload,
            )
            if (
                record.status != "running"
                or record.lease_owner != lease_owner.strip()
                or not record.lease_expires_at
                or record.lease_expires_at <= heartbeat_at.strip()
            ):
                return False
            heartbeat_record = record.model_copy(
                update={
                    "heartbeat_at": heartbeat_at.strip(),
                    "lease_expires_at": lease_expires_at.strip(),
                    "updated_at": heartbeat_at.strip(),
                }
            )
            self._sync_odoo_prod_backup_restore_operation_row(row, heartbeat_record)
            session.commit()
            return True

    def checkpoint_odoo_prod_backup_restore_operation_record(
        self,
        *,
        operation_id: str,
        lease_owner: str,
        phase: OdooProdBackupRestoreOperationPhase,
        checkpointed_at: str,
        evidence: dict[str, str],
    ) -> OdooProdBackupRestoreOperationRecord | None:
        with self._session_factory() as session:
            self._begin_serialized_write(session)
            statement = (
                select(LaunchplaneOdooProdBackupRestoreOperationRow)
                .where(LaunchplaneOdooProdBackupRestoreOperationRow.operation_id == operation_id)
                .limit(1)
            )
            if not self.database_url.startswith("sqlite"):
                statement = statement.with_for_update()
            row = session.scalar(statement)
            if row is None:
                raise FileNotFoundError(operation_id)
            record = self._read_payload(
                model_type=OdooProdBackupRestoreOperationRecord,
                payload=row.payload,
            )
            if (
                record.status != "running"
                or record.lease_owner != lease_owner.strip()
                or not record.lease_expires_at
                or record.lease_expires_at <= checkpointed_at.strip()
            ):
                return None
            phase_indexes = {
                phase_name: index
                for index, phase_name in enumerate(
                    ODOO_PROD_BACKUP_RESTORE_OPERATION_PHASE_SEQUENCE
                )
            }
            replay_restart = (
                phase == "post_deploy_started"
                and odoo_prod_backup_restore_operation_is_verification_replay_claim(record)
            )
            if not replay_restart and phase_indexes[phase] < phase_indexes[record.phase]:
                return None
            checkpoint = OdooProdBackupRestoreCheckpoint(
                phase=phase,
                recorded_at=checkpointed_at,
                evidence=evidence,
            )
            checkpointed_record = record.model_copy(
                update={
                    "phase": phase,
                    "checkpoints": (*record.checkpoints, checkpoint),
                    "updated_at": checkpointed_at,
                }
            )
            self._sync_odoo_prod_backup_restore_operation_row(row, checkpointed_record)
            session.commit()
            return checkpointed_record

    def complete_odoo_prod_backup_restore_operation_record(
        self,
        *,
        record: OdooProdBackupRestoreOperationRecord,
        lease_owner: str,
    ) -> bool:
        with self._session_factory() as session:
            self._begin_serialized_write(session)
            statement = (
                select(LaunchplaneOdooProdBackupRestoreOperationRow)
                .where(
                    LaunchplaneOdooProdBackupRestoreOperationRow.operation_id == record.operation_id
                )
                .limit(1)
            )
            if not self.database_url.startswith("sqlite"):
                statement = statement.with_for_update()
            row = session.scalar(statement)
            if row is None:
                raise FileNotFoundError(record.operation_id)
            current_record = self._read_payload(
                model_type=OdooProdBackupRestoreOperationRecord,
                payload=row.payload,
            )
            completed_at = _utc_now_timestamp()
            if (
                current_record.status != "running"
                or current_record.lease_owner != lease_owner.strip()
                or not current_record.lease_expires_at
                or current_record.lease_expires_at <= completed_at
            ):
                return False
            self._sync_odoo_prod_backup_restore_operation_row(row, record)
            session.commit()
            return True

    def recover_expired_odoo_prod_backup_restore_operation_records(
        self,
        *,
        now: str,
        safe_phases: tuple[str, ...],
        max_attempts: int,
    ) -> tuple[str, ...]:
        filters = (
            LaunchplaneOdooProdBackupRestoreOperationRow.status == "running",
            (
                (LaunchplaneOdooProdBackupRestoreOperationRow.lease_expires_at == "")
                | (LaunchplaneOdooProdBackupRestoreOperationRow.lease_expires_at < now)
            ),
        )
        statement = select(LaunchplaneOdooProdBackupRestoreOperationRow).where(*filters)
        if not self.database_url.startswith("sqlite"):
            statement = statement.with_for_update(skip_locked=True)
        affected_operation_ids: list[str] = []
        with self._session_factory() as session:
            self._begin_serialized_write(session)
            rows = session.scalars(statement).all()
            for row in rows:
                record = self._read_payload(
                    model_type=OdooProdBackupRestoreOperationRecord,
                    payload=row.payload,
                )
                affected_operation_ids.append(record.operation_id)
                if record.phase in safe_phases and record.attempt < max_attempts:
                    recovered_record = record.model_copy(
                        update={
                            "status": "pending",
                            "phase": "created",
                            "checkpoints": (),
                            "started_at": "",
                            "updated_at": now,
                            "lease_owner": "",
                            "lease_expires_at": "",
                            "heartbeat_at": "",
                        }
                    )
                elif record.phase in safe_phases:
                    recovered_record = record.model_copy(
                        update={
                            "status": "fail",
                            "phase": "failed",
                            "updated_at": now,
                            "finished_at": now,
                            "lease_owner": "",
                            "lease_expires_at": "",
                            "heartbeat_at": "",
                            "error_code": "operation_attempts_exhausted",
                            "error_message": (
                                "Odoo production backup restore exhausted automatic attempts in "
                                f"safe phase {record.phase!r}."
                            ),
                        }
                    )
                else:
                    recovered_record = record.model_copy(
                        update={
                            "status": "reconciliation_required",
                            "updated_at": now,
                            "lease_owner": "",
                            "lease_expires_at": "",
                            "heartbeat_at": "",
                            "result": None,
                            "error_code": "operation_reconciliation_required",
                            "error_message": (
                                "Odoo production backup restore lease expired in "
                                f"phase {record.phase!r}; provider state requires operator "
                                "reconciliation before the lane can be released."
                            ),
                        }
                    )
                self._sync_odoo_prod_backup_restore_operation_row(row, recovered_record)
            session.commit()
        return tuple(affected_operation_ids)

    def write_odoo_prod_retained_volume_backup_import_operation_record(
        self, record: OdooProdRetainedVolumeBackupImportOperationRecord
    ) -> None:
        self._write_row(
            LaunchplaneOdooProdRetainedVolumeBackupImportOperationRow(
                operation_id=record.operation_id,
                operation_kind=record.operation_kind,
                product=record.product,
                context=record.context,
                instance=record.instance,
                idempotency_key=record.idempotency_key,
                idempotency_scope=record.idempotency_scope,
                status=record.status,
                phase=record.phase,
                created_at=record.created_at,
                updated_at=record.updated_at,
                lease_owner=record.lease_owner,
                lease_expires_at=record.lease_expires_at,
                heartbeat_at=record.heartbeat_at,
                attempt=record.attempt,
                payload=self._payload_dict(record),
            )
        )

    def _sync_odoo_prod_retained_volume_backup_import_operation_row(
        self,
        row: LaunchplaneOdooProdRetainedVolumeBackupImportOperationRow,
        record: OdooProdRetainedVolumeBackupImportOperationRecord,
    ) -> None:
        row.operation_kind = record.operation_kind
        row.product = record.product
        row.context = record.context
        row.instance = record.instance
        row.idempotency_key = record.idempotency_key
        row.idempotency_scope = record.idempotency_scope
        row.status = record.status
        row.phase = record.phase
        row.created_at = record.created_at
        row.updated_at = record.updated_at
        row.lease_owner = record.lease_owner
        row.lease_expires_at = record.lease_expires_at
        row.heartbeat_at = record.heartbeat_at
        row.attempt = record.attempt
        row.payload = self._payload_dict(record)

    def create_odoo_prod_retained_volume_backup_import_operation_record_if_no_active_lane(
        self, record: OdooProdRetainedVolumeBackupImportOperationRecord
    ) -> tuple[OdooProdRetainedVolumeBackupImportOperationRecord, bool]:
        with self._session_factory() as session:
            self._lock_odoo_stable_lane(
                session,
                product=record.product,
                context=record.context,
                instance=record.instance,
            )
            active_owner = self._active_odoo_stable_lane_operation_owner(
                session,
                product=record.product,
                context=record.context,
                instance=record.instance,
            )
            if active_owner is not None:
                if active_owner.operation_kind == "retained_volume_backup_import":
                    active_row = session.get(
                        LaunchplaneOdooProdRetainedVolumeBackupImportOperationRow,
                        active_owner.operation_id,
                    )
                    if active_row is None:
                        raise RuntimeError(
                            "Active Odoo retained-volume backup import operation disappeared."
                        )
                    return (
                        OdooProdRetainedVolumeBackupImportOperationRecord.model_validate(
                            active_row.payload
                        ),
                        False,
                    )
                raise OdooStableLaneOperationConflictError(active_owner)
            session.add(
                LaunchplaneOdooProdRetainedVolumeBackupImportOperationRow(
                    operation_id=record.operation_id,
                    operation_kind=record.operation_kind,
                    product=record.product,
                    context=record.context,
                    instance=record.instance,
                    idempotency_key=record.idempotency_key,
                    idempotency_scope=record.idempotency_scope,
                    status=record.status,
                    phase=record.phase,
                    created_at=record.created_at,
                    updated_at=record.updated_at,
                    lease_owner=record.lease_owner,
                    lease_expires_at=record.lease_expires_at,
                    heartbeat_at=record.heartbeat_at,
                    attempt=record.attempt,
                    payload=self._payload_dict(record),
                )
            )
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                active_records = (
                    self.list_odoo_prod_retained_volume_backup_import_operation_records(
                        product=record.product,
                        context_name=record.context,
                        instance_name=record.instance,
                        statuses=ODOO_STABLE_LANE_BLOCKING_STATUSES,
                        limit=1,
                    )
                )
                if active_records:
                    return active_records[0], False
                return self.create_odoo_prod_retained_volume_backup_import_operation_record_if_no_active_lane(
                    record
                )
        return record, True

    def read_odoo_prod_retained_volume_backup_import_operation_record(
        self, operation_id: str
    ) -> OdooProdRetainedVolumeBackupImportOperationRecord:
        return self._read_model(
            model_type=OdooProdRetainedVolumeBackupImportOperationRecord,
            orm_model=LaunchplaneOdooProdRetainedVolumeBackupImportOperationRow,
            filters=(
                LaunchplaneOdooProdRetainedVolumeBackupImportOperationRow.operation_id
                == operation_id,
            ),
        )

    def list_odoo_prod_retained_volume_backup_import_operation_records(
        self,
        *,
        operation_kind: str = "",
        product: str = "",
        context_name: str = "",
        instance_name: str = "",
        idempotency_key: str = "",
        idempotency_scope: str = "",
        statuses: tuple[str, ...] = (),
        limit: int | None = None,
    ) -> tuple[OdooProdRetainedVolumeBackupImportOperationRecord, ...]:
        row_type = LaunchplaneOdooProdRetainedVolumeBackupImportOperationRow
        filters: list[object] = []
        if operation_kind:
            filters.append(row_type.operation_kind == operation_kind)
        if product:
            filters.append(row_type.product == product)
        if context_name:
            filters.append(row_type.context == context_name)
        if instance_name:
            filters.append(row_type.instance == instance_name)
        if idempotency_key:
            filters.append(row_type.idempotency_key == idempotency_key)
        if idempotency_scope:
            filters.append(row_type.idempotency_scope == idempotency_scope)
        if statuses:
            filters.append(row_type.status.in_(statuses))
        return self._list_models(
            model_type=OdooProdRetainedVolumeBackupImportOperationRecord,
            orm_model=row_type,
            filters=filters,
            order_by=(row_type.updated_at.desc(), row_type.operation_id.desc()),
            limit=limit,
        )

    def cancel_pending_odoo_prod_retained_volume_backup_import_operation_record(
        self, record: OdooProdRetainedVolumeBackupImportOperationRecord
    ) -> bool:
        if record.status != "cancelled" or record.phase != "cancelled":
            raise ValueError(
                "Odoo retained-volume backup import cancellation requires a cancelled record."
            )
        row_type = LaunchplaneOdooProdRetainedVolumeBackupImportOperationRow
        with self._session_factory() as session:
            self._begin_serialized_write(session)
            statement = (
                select(row_type).where(row_type.operation_id == record.operation_id).limit(1)
            )
            if not self.database_url.startswith("sqlite"):
                statement = statement.with_for_update()
            row = session.scalar(statement)
            if row is None:
                raise FileNotFoundError(record.operation_id)
            current_record = self._read_payload(
                model_type=OdooProdRetainedVolumeBackupImportOperationRecord,
                payload=row.payload,
            )
            if not odoo_stable_lane_cancellation_is_allowed(
                current_status=current_record.status,
                reconciliation_required_at=current_record.updated_at,
                cancellation=record.cancellation,
            ):
                return False
            self._sync_odoo_prod_retained_volume_backup_import_operation_row(row, record)
            session.commit()
            return True

    def claim_next_odoo_prod_retained_volume_backup_import_operation_record(
        self,
        *,
        lease_owner: str,
        lease_expires_at: str,
        claimed_at: str,
    ) -> OdooProdRetainedVolumeBackupImportOperationRecord | None:
        normalized_lease_owner = lease_owner.strip()
        if not normalized_lease_owner or not lease_expires_at.strip() or not claimed_at.strip():
            raise ValueError("Odoo retained-volume backup import claim requires lease evidence.")
        row_type = LaunchplaneOdooProdRetainedVolumeBackupImportOperationRow
        statement = (
            select(row_type)
            .where(row_type.status == "pending")
            .order_by(row_type.created_at.asc(), row_type.operation_id.asc())
        )
        if not self.database_url.startswith("sqlite"):
            statement = statement.with_for_update(skip_locked=True)
        with self._session_factory() as session:
            if self.database_url.startswith("sqlite"):
                self._lock_odoo_stable_lane(
                    session,
                    product="",
                    context="",
                    instance="",
                )
            for row in session.scalars(statement).all():
                record = self._read_payload(
                    model_type=OdooProdRetainedVolumeBackupImportOperationRecord,
                    payload=row.payload,
                )
                if not self.database_url.startswith("sqlite"):
                    self._lock_odoo_stable_lane(
                        session,
                        product=record.product,
                        context=record.context,
                        instance=record.instance,
                    )
                active_owner = self._active_odoo_stable_lane_operation_owner(
                    session,
                    product=record.product,
                    context=record.context,
                    instance=record.instance,
                )
                if active_owner != OdooStableLaneOperationOwner(
                    operation_kind="retained_volume_backup_import",
                    operation_id=record.operation_id,
                ):
                    continue
                claimed_record = record.model_copy(
                    update={
                        "status": "running",
                        "phase": "running",
                        "started_at": record.started_at or claimed_at,
                        "updated_at": claimed_at,
                        "lease_owner": normalized_lease_owner,
                        "lease_expires_at": lease_expires_at.strip(),
                        "heartbeat_at": claimed_at.strip(),
                        "attempt": record.attempt + 1,
                    }
                )
                self._sync_odoo_prod_retained_volume_backup_import_operation_row(
                    row,
                    claimed_record,
                )
                session.commit()
                return claimed_record
            return None

    def heartbeat_odoo_prod_retained_volume_backup_import_operation_record(
        self,
        *,
        operation_id: str,
        lease_owner: str,
        heartbeat_at: str,
        lease_expires_at: str,
    ) -> bool:
        row_type = LaunchplaneOdooProdRetainedVolumeBackupImportOperationRow
        with self._session_factory() as session:
            self._begin_serialized_write(session)
            statement = select(row_type).where(row_type.operation_id == operation_id).limit(1)
            if not self.database_url.startswith("sqlite"):
                statement = statement.with_for_update()
            row = session.scalar(statement)
            if row is None:
                raise FileNotFoundError(operation_id)
            record = self._read_payload(
                model_type=OdooProdRetainedVolumeBackupImportOperationRecord,
                payload=row.payload,
            )
            if (
                record.status != "running"
                or record.lease_owner != lease_owner.strip()
                or not record.lease_expires_at
                or record.lease_expires_at <= heartbeat_at.strip()
            ):
                return False
            heartbeat_record = record.model_copy(
                update={
                    "heartbeat_at": heartbeat_at.strip(),
                    "lease_expires_at": lease_expires_at.strip(),
                    "updated_at": heartbeat_at.strip(),
                }
            )
            self._sync_odoo_prod_retained_volume_backup_import_operation_row(row, heartbeat_record)
            session.commit()
            return True

    def checkpoint_odoo_prod_retained_volume_backup_import_operation_record(
        self,
        *,
        operation_id: str,
        lease_owner: str,
        phase: OdooProdRetainedVolumeBackupImportOperationPhase,
        checkpointed_at: str,
        evidence: dict[str, str],
    ) -> OdooProdRetainedVolumeBackupImportOperationRecord | None:
        row_type = LaunchplaneOdooProdRetainedVolumeBackupImportOperationRow
        with self._session_factory() as session:
            self._begin_serialized_write(session)
            statement = select(row_type).where(row_type.operation_id == operation_id).limit(1)
            if not self.database_url.startswith("sqlite"):
                statement = statement.with_for_update()
            row = session.scalar(statement)
            if row is None:
                raise FileNotFoundError(operation_id)
            record = self._read_payload(
                model_type=OdooProdRetainedVolumeBackupImportOperationRecord,
                payload=row.payload,
            )
            if (
                record.status != "running"
                or record.lease_owner != lease_owner.strip()
                or not record.lease_expires_at
                or record.lease_expires_at <= checkpointed_at.strip()
            ):
                return None
            phase_indexes = {
                phase_name: index
                for index, phase_name in enumerate(
                    ODOO_PROD_RETAINED_VOLUME_BACKUP_IMPORT_OPERATION_PHASE_SEQUENCE
                )
            }
            if phase_indexes[phase] < phase_indexes[record.phase]:
                return None
            checkpoint = OdooProdRetainedVolumeBackupImportCheckpoint(
                phase=phase,
                recorded_at=checkpointed_at,
                evidence=evidence,
            )
            checkpointed_record = record.model_copy(
                update={
                    "phase": phase,
                    "checkpoints": (*record.checkpoints, checkpoint),
                    "updated_at": checkpointed_at,
                }
            )
            self._sync_odoo_prod_retained_volume_backup_import_operation_row(
                row, checkpointed_record
            )
            session.commit()
            return checkpointed_record

    def complete_odoo_prod_retained_volume_backup_import_operation_record(
        self,
        *,
        record: OdooProdRetainedVolumeBackupImportOperationRecord,
        lease_owner: str,
    ) -> bool:
        row_type = LaunchplaneOdooProdRetainedVolumeBackupImportOperationRow
        with self._session_factory() as session:
            self._begin_serialized_write(session)
            statement = (
                select(row_type).where(row_type.operation_id == record.operation_id).limit(1)
            )
            if not self.database_url.startswith("sqlite"):
                statement = statement.with_for_update()
            row = session.scalar(statement)
            if row is None:
                raise FileNotFoundError(record.operation_id)
            current_record = self._read_payload(
                model_type=OdooProdRetainedVolumeBackupImportOperationRecord,
                payload=row.payload,
            )
            completed_at = _utc_now_timestamp()
            if (
                current_record.status != "running"
                or current_record.lease_owner != lease_owner.strip()
                or not current_record.lease_expires_at
                or current_record.lease_expires_at <= completed_at
            ):
                return False
            self._sync_odoo_prod_retained_volume_backup_import_operation_row(row, record)
            session.commit()
            return True

    def recover_expired_odoo_prod_retained_volume_backup_import_operation_records(
        self,
        *,
        now: str,
        safe_phases: tuple[str, ...],
        max_attempts: int,
    ) -> tuple[str, ...]:
        row_type = LaunchplaneOdooProdRetainedVolumeBackupImportOperationRow
        filters = (
            row_type.status == "running",
            (row_type.lease_expires_at == "") | (row_type.lease_expires_at < now),
        )
        statement = select(row_type).where(*filters)
        if not self.database_url.startswith("sqlite"):
            statement = statement.with_for_update(skip_locked=True)
        affected_operation_ids: list[str] = []
        with self._session_factory() as session:
            self._begin_serialized_write(session)
            rows = session.scalars(statement).all()
            for row in rows:
                record = self._read_payload(
                    model_type=OdooProdRetainedVolumeBackupImportOperationRecord,
                    payload=row.payload,
                )
                affected_operation_ids.append(record.operation_id)
                if record.phase in safe_phases and record.attempt < max_attempts:
                    recovered_record = record.model_copy(
                        update={
                            "status": "pending",
                            "phase": "created",
                            "checkpoints": (),
                            "started_at": "",
                            "updated_at": now,
                            "lease_owner": "",
                            "lease_expires_at": "",
                            "heartbeat_at": "",
                        }
                    )
                elif record.phase in safe_phases:
                    recovered_record = record.model_copy(
                        update={
                            "status": "fail",
                            "phase": "failed",
                            "updated_at": now,
                            "finished_at": now,
                            "lease_owner": "",
                            "lease_expires_at": "",
                            "heartbeat_at": "",
                            "error_code": "operation_attempts_exhausted",
                            "error_message": (
                                "Odoo retained-volume backup import exhausted automatic attempts "
                                f"in safe phase {record.phase!r}."
                            ),
                        }
                    )
                else:
                    recovered_record = record.model_copy(
                        update={
                            "status": "reconciliation_required",
                            "updated_at": now,
                            "lease_owner": "",
                            "lease_expires_at": "",
                            "heartbeat_at": "",
                            "error_code": "operation_reconciliation_required",
                            "error_message": (
                                "Odoo retained-volume backup import lease expired in "
                                f"phase {record.phase!r}; provider state requires operator "
                                "reconciliation before the lane can be released."
                            ),
                        }
                    )
                self._sync_odoo_prod_retained_volume_backup_import_operation_row(
                    row, recovered_record
                )
            session.commit()
        return tuple(affected_operation_ids)

    def write_verireel_prod_backup_gate_operation_record(
        self, record: VeriReelProdBackupGateOperationRecord
    ) -> None:
        self._write_row(
            LaunchplaneVeriReelProdBackupGateOperationRow(
                operation_id=record.operation_id,
                product=record.product,
                context=record.context,
                instance=record.instance,
                backup_record_id=record.backup_record_id,
                status=record.status,
                phase=record.phase,
                created_at=record.created_at,
                updated_at=record.updated_at,
                lease_owner=record.lease_owner,
                lease_expires_at=record.lease_expires_at,
                heartbeat_at=record.heartbeat_at,
                attempt=record.attempt,
                payload=self._payload_dict(record),
            )
        )

    def _sync_verireel_prod_backup_gate_operation_row(
        self,
        row: LaunchplaneVeriReelProdBackupGateOperationRow,
        record: VeriReelProdBackupGateOperationRecord,
    ) -> None:
        row.product = record.product
        row.context = record.context
        row.instance = record.instance
        row.backup_record_id = record.backup_record_id
        row.status = record.status
        row.phase = record.phase
        row.created_at = record.created_at
        row.updated_at = record.updated_at
        row.lease_owner = record.lease_owner
        row.lease_expires_at = record.lease_expires_at
        row.heartbeat_at = record.heartbeat_at
        row.attempt = record.attempt
        row.payload = self._payload_dict(record)

    def create_verireel_prod_backup_gate_operation_record_if_no_active_record(
        self, record: VeriReelProdBackupGateOperationRecord
    ) -> tuple[VeriReelProdBackupGateOperationRecord, bool]:
        try:
            return self.read_verireel_prod_backup_gate_operation_record(record.operation_id), False
        except FileNotFoundError:
            pass
        with self._session_factory() as session:
            session.add(
                LaunchplaneVeriReelProdBackupGateOperationRow(
                    operation_id=record.operation_id,
                    product=record.product,
                    context=record.context,
                    instance=record.instance,
                    backup_record_id=record.backup_record_id,
                    status=record.status,
                    phase=record.phase,
                    created_at=record.created_at,
                    updated_at=record.updated_at,
                    lease_owner=record.lease_owner,
                    lease_expires_at=record.lease_expires_at,
                    heartbeat_at=record.heartbeat_at,
                    attempt=record.attempt,
                    payload=self._payload_dict(record),
                )
            )
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                active_records = self.list_verireel_prod_backup_gate_operation_records(
                    backup_record_id=record.backup_record_id,
                    statuses=("pending", "running"),
                    limit=1,
                )
                if active_records:
                    return active_records[0], False
                return self.create_verireel_prod_backup_gate_operation_record_if_no_active_record(
                    record
                )
        return record, True

    def read_verireel_prod_backup_gate_operation_record(
        self, operation_id: str
    ) -> VeriReelProdBackupGateOperationRecord:
        return self._read_model(
            model_type=VeriReelProdBackupGateOperationRecord,
            orm_model=LaunchplaneVeriReelProdBackupGateOperationRow,
            filters=(LaunchplaneVeriReelProdBackupGateOperationRow.operation_id == operation_id,),
        )

    def list_verireel_prod_backup_gate_operation_records(
        self,
        *,
        product: str = "",
        context_name: str = "",
        instance_name: str = "",
        backup_record_id: str = "",
        statuses: tuple[str, ...] = (),
        limit: int | None = None,
    ) -> tuple[VeriReelProdBackupGateOperationRecord, ...]:
        filters: list[object] = []
        if product:
            filters.append(LaunchplaneVeriReelProdBackupGateOperationRow.product == product)
        if context_name:
            filters.append(LaunchplaneVeriReelProdBackupGateOperationRow.context == context_name)
        if instance_name:
            filters.append(LaunchplaneVeriReelProdBackupGateOperationRow.instance == instance_name)
        if backup_record_id:
            filters.append(
                LaunchplaneVeriReelProdBackupGateOperationRow.backup_record_id == backup_record_id
            )
        if statuses:
            filters.append(LaunchplaneVeriReelProdBackupGateOperationRow.status.in_(statuses))
        return self._list_models(
            model_type=VeriReelProdBackupGateOperationRecord,
            orm_model=LaunchplaneVeriReelProdBackupGateOperationRow,
            filters=filters,
            order_by=(
                LaunchplaneVeriReelProdBackupGateOperationRow.updated_at.desc(),
                LaunchplaneVeriReelProdBackupGateOperationRow.operation_id.desc(),
            ),
            limit=limit,
        )

    def cancel_pending_verireel_prod_backup_gate_operation_record(
        self, record: VeriReelProdBackupGateOperationRecord
    ) -> bool:
        if record.status != "cancelled" or record.phase != "cancelled":
            raise ValueError("VeriReel backup gate cancellation requires a cancelled record.")
        with self._session_factory() as session:
            statement = (
                select(LaunchplaneVeriReelProdBackupGateOperationRow)
                .where(
                    LaunchplaneVeriReelProdBackupGateOperationRow.operation_id
                    == record.operation_id
                )
                .limit(1)
            )
            if not self.database_url.startswith("sqlite"):
                statement = statement.with_for_update()
            row = session.scalar(statement)
            if row is None:
                raise FileNotFoundError(record.operation_id)
            current_record = self._read_payload(
                model_type=VeriReelProdBackupGateOperationRecord,
                payload=row.payload,
            )
            if current_record.status != "pending":
                return False
            backup_gate_record = build_cancelled_verireel_prod_backup_gate_record(record)
            session.merge(
                LaunchplaneBackupGateRow(
                    record_id=backup_gate_record.record_id,
                    context=backup_gate_record.context,
                    instance=backup_gate_record.instance,
                    created_at=backup_gate_record.created_at,
                    status=backup_gate_record.status,
                    payload=self._payload_dict(backup_gate_record),
                )
            )
            self._sync_verireel_prod_backup_gate_operation_row(row, record)
            session.commit()
            return True

    def claim_next_verireel_prod_backup_gate_operation_record(
        self,
        *,
        lease_owner: str,
        lease_expires_at: str,
        claimed_at: str,
    ) -> VeriReelProdBackupGateOperationRecord | None:
        normalized_lease_owner = lease_owner.strip()
        if not normalized_lease_owner:
            raise ValueError("VeriReel prod backup gate operation claim requires lease_owner.")
        if not lease_expires_at.strip():
            raise ValueError("VeriReel prod backup gate operation claim requires lease_expires_at.")
        if not claimed_at.strip():
            raise ValueError("VeriReel prod backup gate operation claim requires claimed_at.")
        statement = (
            select(LaunchplaneVeriReelProdBackupGateOperationRow)
            .where(LaunchplaneVeriReelProdBackupGateOperationRow.status == "pending")
            .order_by(
                LaunchplaneVeriReelProdBackupGateOperationRow.created_at.asc(),
                LaunchplaneVeriReelProdBackupGateOperationRow.operation_id.asc(),
            )
            .limit(1)
        )
        if not self.database_url.startswith("sqlite"):
            statement = statement.with_for_update(skip_locked=True)
        with self._session_factory() as session:
            row = session.scalar(statement)
            if row is None:
                return None
            record = self._read_payload(
                model_type=VeriReelProdBackupGateOperationRecord,
                payload=row.payload,
            )
            claimed_record = record.model_copy(
                update={
                    "status": "running",
                    "phase": "running",
                    "started_at": record.started_at or claimed_at,
                    "updated_at": claimed_at,
                    "lease_owner": normalized_lease_owner,
                    "lease_expires_at": lease_expires_at.strip(),
                    "heartbeat_at": claimed_at.strip(),
                    "attempt": record.attempt + 1,
                }
            )
            self._sync_verireel_prod_backup_gate_operation_row(row, claimed_record)
            session.commit()
            return claimed_record

    def heartbeat_verireel_prod_backup_gate_operation_record(
        self,
        *,
        operation_id: str,
        lease_owner: str,
        heartbeat_at: str,
        lease_expires_at: str,
    ) -> bool:
        with self._session_factory() as session:
            statement = (
                select(LaunchplaneVeriReelProdBackupGateOperationRow)
                .where(LaunchplaneVeriReelProdBackupGateOperationRow.operation_id == operation_id)
                .limit(1)
            )
            if not self.database_url.startswith("sqlite"):
                statement = statement.with_for_update()
            row = session.scalar(statement)
            if row is None:
                raise FileNotFoundError(operation_id)
            record = self._read_payload(
                model_type=VeriReelProdBackupGateOperationRecord,
                payload=row.payload,
            )
            if (
                record.status != "running"
                or record.lease_owner != lease_owner.strip()
                or not record.lease_expires_at
                or record.lease_expires_at <= heartbeat_at.strip()
            ):
                return False
            heartbeat_record = record.model_copy(
                update={
                    "heartbeat_at": heartbeat_at.strip(),
                    "lease_expires_at": lease_expires_at.strip(),
                    "updated_at": heartbeat_at.strip(),
                }
            )
            self._sync_verireel_prod_backup_gate_operation_row(row, heartbeat_record)
            session.commit()
            return True

    def mark_verireel_prod_backup_gate_operation_phase(
        self,
        *,
        operation_id: str,
        lease_owner: str,
        phase: str,
        updated_at: str,
    ) -> VeriReelProdBackupGateOperationRecord | None:
        with self._session_factory() as session:
            statement = (
                select(LaunchplaneVeriReelProdBackupGateOperationRow)
                .where(LaunchplaneVeriReelProdBackupGateOperationRow.operation_id == operation_id)
                .limit(1)
            )
            if not self.database_url.startswith("sqlite"):
                statement = statement.with_for_update()
            row = session.scalar(statement)
            if row is None:
                raise FileNotFoundError(operation_id)
            record = self._read_payload(
                model_type=VeriReelProdBackupGateOperationRecord,
                payload=row.payload,
            )
            if (
                record.status != "running"
                or record.lease_owner != lease_owner.strip()
                or not record.lease_expires_at
                or record.lease_expires_at <= updated_at.strip()
            ):
                return None
            updated_record = record.model_copy(
                update={
                    "phase": phase.strip(),
                    "updated_at": updated_at.strip(),
                }
            )
            self._sync_verireel_prod_backup_gate_operation_row(row, updated_record)
            session.commit()
            return updated_record

    def complete_verireel_prod_backup_gate_operation_record(
        self,
        *,
        record: VeriReelProdBackupGateOperationRecord,
        lease_owner: str,
    ) -> bool:
        with self._session_factory() as session:
            statement = (
                select(LaunchplaneVeriReelProdBackupGateOperationRow)
                .where(
                    LaunchplaneVeriReelProdBackupGateOperationRow.operation_id
                    == record.operation_id
                )
                .limit(1)
            )
            if not self.database_url.startswith("sqlite"):
                statement = statement.with_for_update()
            row = session.scalar(statement)
            if row is None:
                raise FileNotFoundError(record.operation_id)
            current_record = self._read_payload(
                model_type=VeriReelProdBackupGateOperationRecord,
                payload=row.payload,
            )
            completed_at = _utc_now_timestamp()
            if (
                current_record.status != "running"
                or current_record.lease_owner != lease_owner.strip()
                or not current_record.lease_expires_at
                or current_record.lease_expires_at <= completed_at
            ):
                return False
            self._sync_verireel_prod_backup_gate_operation_row(row, record)
            session.commit()
            return True

    def complete_verireel_prod_backup_gate_operation_with_backup_gate_record(
        self,
        *,
        operation_record: VeriReelProdBackupGateOperationRecord,
        backup_gate_record: BackupGateRecord,
        lease_owner: str,
    ) -> bool:
        with self._session_factory() as session:
            statement = (
                select(LaunchplaneVeriReelProdBackupGateOperationRow)
                .where(
                    LaunchplaneVeriReelProdBackupGateOperationRow.operation_id
                    == operation_record.operation_id
                )
                .limit(1)
            )
            if not self.database_url.startswith("sqlite"):
                statement = statement.with_for_update()
            row = session.scalar(statement)
            if row is None:
                raise FileNotFoundError(operation_record.operation_id)
            current_record = self._read_payload(
                model_type=VeriReelProdBackupGateOperationRecord,
                payload=row.payload,
            )
            completed_at = _utc_now_timestamp()
            if (
                current_record.status != "running"
                or current_record.lease_owner != lease_owner.strip()
                or not current_record.lease_expires_at
                or current_record.lease_expires_at <= completed_at
            ):
                return False
            session.merge(
                LaunchplaneBackupGateRow(
                    record_id=backup_gate_record.record_id,
                    context=backup_gate_record.context,
                    instance=backup_gate_record.instance,
                    created_at=backup_gate_record.created_at,
                    status=backup_gate_record.status,
                    payload=self._payload_dict(backup_gate_record),
                )
            )
            self._sync_verireel_prod_backup_gate_operation_row(row, operation_record)
            session.commit()
            return True

    def recover_expired_verireel_prod_backup_gate_operation_records(
        self,
        *,
        now: str,
        safe_phases: tuple[str, ...],
        max_attempts: int,
    ) -> tuple[str, ...]:
        filters = (
            LaunchplaneVeriReelProdBackupGateOperationRow.status == "running",
            (
                (LaunchplaneVeriReelProdBackupGateOperationRow.lease_expires_at == "")
                | (LaunchplaneVeriReelProdBackupGateOperationRow.lease_expires_at < now)
            ),
        )
        statement = select(LaunchplaneVeriReelProdBackupGateOperationRow).where(*filters)
        if not self.database_url.startswith("sqlite"):
            statement = statement.with_for_update(skip_locked=True)
        affected_operation_ids: list[str] = []
        with self._session_factory() as session:
            rows = session.scalars(statement).all()
            for row in rows:
                record = self._read_payload(
                    model_type=VeriReelProdBackupGateOperationRecord,
                    payload=row.payload,
                )
                affected_operation_ids.append(record.operation_id)
                if record.phase in safe_phases and record.attempt < max_attempts:
                    recovered_record = record.model_copy(
                        update={
                            "status": "pending",
                            "started_at": "",
                            "updated_at": now,
                            "lease_owner": "",
                            "lease_expires_at": "",
                            "heartbeat_at": "",
                        }
                    )
                else:
                    error_message = (
                        "VeriReel prod backup gate operation lease expired in "
                        f"phase {record.phase!r}; unsafe to retry automatically."
                    )
                    recovered_record = record.model_copy(
                        update={
                            "status": "fail",
                            "phase": "failed",
                            "updated_at": now,
                            "finished_at": now,
                            "lease_owner": "",
                            "lease_expires_at": "",
                            "heartbeat_at": "",
                            "error_message": error_message,
                        }
                    )
                    session.merge(
                        LaunchplaneBackupGateRow(
                            record_id=record.backup_record_id,
                            context=record.context,
                            instance=record.instance,
                            created_at=now,
                            status="fail",
                            payload=self._payload_dict(
                                BackupGateRecord(
                                    record_id=record.backup_record_id,
                                    context=record.context,
                                    instance=record.instance,
                                    created_at=now,
                                    source="launchplane-verireel-prod-backup-gate",
                                    required=True,
                                    status="fail",
                                    evidence={"error_message": error_message},
                                )
                            ),
                        )
                    )
                self._sync_verireel_prod_backup_gate_operation_row(row, recovered_record)
            session.commit()
        return tuple(affected_operation_ids)

    def write_session(self, session: LaunchplaneHumanSession) -> None:
        self._write_row(
            LaunchplaneHumanSessionRow(
                session_id=session.session_id,
                login=session.identity.login,
                github_id=session.identity.github_id,
                role=session.identity.role,
                created_at=session.created_at.isoformat(),
                expires_at=session.expires_at.isoformat(),
                payload=_human_session_payload(session),
            )
        )

    def read_session(self, session_id: str) -> LaunchplaneHumanSession | None:
        statement = (
            select(LaunchplaneHumanSessionRow)
            .where(LaunchplaneHumanSessionRow.session_id == session_id)
            .limit(1)
        )
        with self._session_factory() as session:
            row = session.scalar(statement)
            if row is None:
                return None
            human_session = _human_session_from_payload(row.payload)
            if human_session.expires_at <= datetime.now(timezone.utc):
                session.delete(row)
                session.commit()
                return None
            return human_session

    def read_session_without_cleanup(
        self,
        session_id: str,
    ) -> LaunchplaneHumanSession | None:
        statement = (
            select(LaunchplaneHumanSessionRow.payload)
            .where(LaunchplaneHumanSessionRow.session_id == session_id)
            .limit(1)
        )
        with self._session_factory() as session:
            payload = session.scalar(statement)
            if payload is None:
                return None
            return _human_session_from_payload(payload)

    def delete_session(self, session_id: str) -> None:
        with self._session_factory() as session:
            session.execute(
                delete(LaunchplaneHumanSessionRow).where(
                    LaunchplaneHumanSessionRow.session_id == session_id
                )
            )
            session.commit()

    def write_session_if_csrf_generation(
        self,
        human_session: LaunchplaneHumanSession,
        *,
        expected_generation: int,
    ) -> bool:
        statement = (
            select(LaunchplaneHumanSessionRow)
            .where(LaunchplaneHumanSessionRow.session_id == human_session.session_id)
            .with_for_update()
            .limit(1)
        )
        with self._session_factory() as session:
            row = session.scalar(statement)
            if row is None:
                return False
            current_session = _human_session_from_payload(row.payload)
            if current_session.expires_at <= datetime.now(timezone.utc):
                session.delete(row)
                session.commit()
                return False
            if current_session.csrf_generation != expected_generation:
                return False
            row.login = human_session.identity.login
            row.github_id = human_session.identity.github_id
            row.role = human_session.identity.role
            row.created_at = human_session.created_at.isoformat()
            row.expires_at = human_session.expires_at.isoformat()
            row.payload = _human_session_payload(human_session)
            session.commit()
        return True

    def write_deployment_record(self, record: DeploymentRecord) -> None:
        self._write_row(
            LaunchplaneDeploymentRow(
                record_id=record.record_id,
                context=record.context,
                instance=record.instance,
                artifact_id=_artifact_id_from_model(record),
                source_git_ref=record.source_git_ref,
                deploy_started_at=record.deploy.started_at,
                deploy_finished_at=record.deploy.finished_at,
                payload=self._payload_dict(record),
            )
        )

    def read_deployment_record(self, record_id: str) -> DeploymentRecord:
        return self._read_model(
            model_type=DeploymentRecord,
            orm_model=LaunchplaneDeploymentRow,
            filters=(LaunchplaneDeploymentRow.record_id == record_id,),
        )

    def list_deployment_records(
        self,
        *,
        context_name: str = "",
        instance_name: str = "",
        limit: int | None = None,
    ) -> tuple[DeploymentRecord, ...]:
        filters: list[object] = []
        if context_name:
            filters.append(LaunchplaneDeploymentRow.context == context_name)
        if instance_name:
            filters.append(LaunchplaneDeploymentRow.instance == instance_name)
        return self._list_models(
            model_type=DeploymentRecord,
            orm_model=LaunchplaneDeploymentRow,
            filters=filters,
            order_by=(
                LaunchplaneDeploymentRow.deploy_finished_at.desc(),
                LaunchplaneDeploymentRow.deploy_started_at.desc(),
                LaunchplaneDeploymentRow.record_id.desc(),
            ),
            limit=limit,
        )

    def write_generic_web_rollback_plan_record(self, record: GenericWebRollbackPlanRecord) -> None:
        self._write_row(
            LaunchplaneGenericWebRollbackPlanRow(
                plan_id=record.plan_id,
                product=record.product,
                context=record.context,
                instance=record.instance,
                created_at=record.created_at,
                status=record.status,
                rollback_deployment_record_id=record.rollback_deployment_record_id,
                payload=self._payload_dict(record),
            )
        )

    def list_generic_web_rollback_plan_records(
        self,
        *,
        context_name: str = "",
        instance_name: str = "",
        limit: int | None = None,
    ) -> tuple[GenericWebRollbackPlanRecord, ...]:
        filters: list[object] = []
        if context_name:
            filters.append(LaunchplaneGenericWebRollbackPlanRow.context == context_name)
        if instance_name:
            filters.append(LaunchplaneGenericWebRollbackPlanRow.instance == instance_name)
        return self._list_models(
            model_type=GenericWebRollbackPlanRecord,
            orm_model=LaunchplaneGenericWebRollbackPlanRow,
            filters=filters,
            order_by=(
                LaunchplaneGenericWebRollbackPlanRow.created_at.desc(),
                LaunchplaneGenericWebRollbackPlanRow.plan_id.desc(),
            ),
            limit=limit,
        )

    def write_promotion_record(self, record: PromotionRecord) -> None:
        self._write_row(
            LaunchplanePromotionRow(
                record_id=record.record_id,
                context=record.context,
                from_instance=record.from_instance,
                to_instance=record.to_instance,
                artifact_id=record.artifact_identity.artifact_id,
                deploy_started_at=record.deploy.started_at,
                deploy_finished_at=record.deploy.finished_at,
                payload=self._payload_dict(record),
            )
        )

    def write_promotion_evidence_records(
        self,
        *,
        promotion_record: PromotionRecord,
        inventory: EnvironmentInventory,
    ) -> None:
        with self._session_factory() as session:
            session.merge(
                LaunchplanePromotionRow(
                    record_id=promotion_record.record_id,
                    context=promotion_record.context,
                    from_instance=promotion_record.from_instance,
                    to_instance=promotion_record.to_instance,
                    artifact_id=promotion_record.artifact_identity.artifact_id,
                    deploy_started_at=promotion_record.deploy.started_at,
                    deploy_finished_at=promotion_record.deploy.finished_at,
                    payload=self._payload_dict(promotion_record),
                )
            )
            session.merge(
                LaunchplaneInventoryRow(
                    context=inventory.context,
                    instance=inventory.instance,
                    artifact_id=_artifact_id_from_model(inventory),
                    source_git_ref=inventory.source_git_ref,
                    updated_at=inventory.updated_at,
                    deployment_record_id=inventory.deployment_record_id,
                    promotion_record_id=inventory.promotion_record_id,
                    promoted_from_instance=inventory.promoted_from_instance,
                    payload=self._payload_dict(inventory),
                )
            )
            session.commit()

    def read_promotion_record(self, record_id: str) -> PromotionRecord:
        return self._read_model(
            model_type=PromotionRecord,
            orm_model=LaunchplanePromotionRow,
            filters=(LaunchplanePromotionRow.record_id == record_id,),
        )

    def list_promotion_records(
        self,
        *,
        context_name: str = "",
        from_instance_name: str = "",
        to_instance_name: str = "",
        limit: int | None = None,
    ) -> tuple[PromotionRecord, ...]:
        filters: list[object] = []
        if context_name:
            filters.append(LaunchplanePromotionRow.context == context_name)
        if from_instance_name:
            filters.append(LaunchplanePromotionRow.from_instance == from_instance_name)
        if to_instance_name:
            filters.append(LaunchplanePromotionRow.to_instance == to_instance_name)
        return self._list_models(
            model_type=PromotionRecord,
            orm_model=LaunchplanePromotionRow,
            filters=filters,
            order_by=(
                LaunchplanePromotionRow.deploy_finished_at.desc(),
                LaunchplanePromotionRow.deploy_started_at.desc(),
                LaunchplanePromotionRow.record_id.desc(),
            ),
            limit=limit,
        )

    def write_environment_inventory(self, record: EnvironmentInventory) -> None:
        self._write_row(self._environment_inventory_row(record))

    def read_environment_inventory(
        self, *, context_name: str, instance_name: str
    ) -> EnvironmentInventory:
        return self._read_model(
            model_type=EnvironmentInventory,
            orm_model=LaunchplaneInventoryRow,
            filters=(
                LaunchplaneInventoryRow.context == context_name,
                LaunchplaneInventoryRow.instance == instance_name,
            ),
        )

    def list_environment_inventory(self) -> tuple[EnvironmentInventory, ...]:
        return self._list_models(
            model_type=EnvironmentInventory,
            orm_model=LaunchplaneInventoryRow,
            order_by=(
                LaunchplaneInventoryRow.context.asc(),
                LaunchplaneInventoryRow.instance.asc(),
            ),
        )

    def write_preview_record(self, record: PreviewRecord) -> None:
        self._write_row(
            LaunchplanePreviewRow(
                preview_id=record.preview_id,
                context=record.context,
                anchor_repo=record.anchor_repo,
                anchor_pr_number=record.anchor_pr_number,
                state=record.state,
                updated_at=record.updated_at,
                payload=self._payload_dict(record),
            )
        )

    def read_preview_record(self, preview_id: str) -> PreviewRecord:
        return self._read_model(
            model_type=PreviewRecord,
            orm_model=LaunchplanePreviewRow,
            filters=(LaunchplanePreviewRow.preview_id == preview_id,),
        )

    def list_preview_records(
        self,
        *,
        context_name: str = "",
        anchor_repo: str = "",
        anchor_pr_number: int | None = None,
        limit: int | None = None,
    ) -> tuple[PreviewRecord, ...]:
        filters: list[object] = []
        if context_name:
            filters.append(LaunchplanePreviewRow.context == context_name)
        if anchor_repo:
            filters.append(LaunchplanePreviewRow.anchor_repo == anchor_repo)
        if anchor_pr_number is not None:
            filters.append(LaunchplanePreviewRow.anchor_pr_number == anchor_pr_number)
        return self._list_models(
            model_type=PreviewRecord,
            orm_model=LaunchplanePreviewRow,
            filters=filters,
            order_by=(
                LaunchplanePreviewRow.updated_at.desc(),
                LaunchplanePreviewRow.preview_id.desc(),
            ),
            limit=limit,
        )

    def write_preview_generation_record(self, record: PreviewGenerationRecord) -> None:
        self._write_row(
            LaunchplanePreviewGenerationRow(
                generation_id=record.generation_id,
                preview_id=record.preview_id,
                sequence=record.sequence,
                state=record.state,
                requested_at=record.requested_at,
                finished_at=record.finished_at,
                artifact_id=record.artifact_id,
                payload=self._payload_dict(record),
            )
        )

    @contextmanager
    def serialize_preview_refresh(self, *, preview_id: str) -> Iterator[None]:
        normalized_preview_id = preview_id.strip()
        if not normalized_preview_id:
            raise ValueError("Preview refresh serialization requires preview_id.")
        if self._engine.dialect.name != "postgresql":
            yield
            return
        with self._session_factory() as session:
            session.execute(
                text("select pg_advisory_xact_lock(hashtextextended(:lock_name, 0))"),
                {"lock_name": f"launchplane-preview-refresh:{normalized_preview_id}"},
            )
            try:
                yield
            finally:
                session.commit()

    def write_preview_generation_evidence_records(
        self,
        *,
        preview_record: PreviewRecord,
        generation_record: PreviewGenerationRecord,
    ) -> tuple[None, None]:
        with self._session_factory() as session:
            session.merge(
                LaunchplanePreviewGenerationRow(
                    generation_id=generation_record.generation_id,
                    preview_id=generation_record.preview_id,
                    sequence=generation_record.sequence,
                    state=generation_record.state,
                    requested_at=generation_record.requested_at,
                    finished_at=generation_record.finished_at,
                    artifact_id=generation_record.artifact_id,
                    payload=self._payload_dict(generation_record),
                )
            )
            session.merge(
                LaunchplanePreviewRow(
                    preview_id=preview_record.preview_id,
                    context=preview_record.context,
                    anchor_repo=preview_record.anchor_repo,
                    anchor_pr_number=preview_record.anchor_pr_number,
                    state=preview_record.state,
                    updated_at=preview_record.updated_at,
                    payload=self._payload_dict(preview_record),
                )
            )
            session.commit()
        return None, None

    def read_preview_generation_record(self, generation_id: str) -> PreviewGenerationRecord:
        return self._read_model(
            model_type=PreviewGenerationRecord,
            orm_model=LaunchplanePreviewGenerationRow,
            filters=(LaunchplanePreviewGenerationRow.generation_id == generation_id,),
        )

    def list_preview_generation_records(
        self,
        *,
        preview_id: str = "",
        limit: int | None = None,
    ) -> tuple[PreviewGenerationRecord, ...]:
        filters: list[object] = []
        if preview_id:
            filters.append(LaunchplanePreviewGenerationRow.preview_id == preview_id)
        return self._list_models(
            model_type=PreviewGenerationRecord,
            orm_model=LaunchplanePreviewGenerationRow,
            filters=filters,
            order_by=(
                LaunchplanePreviewGenerationRow.sequence.desc(),
                LaunchplanePreviewGenerationRow.requested_at.desc(),
                LaunchplanePreviewGenerationRow.generation_id.desc(),
            ),
            limit=limit,
        )

    def write_manager_preview_approval_event_record(
        self, record: ManagerPreviewApprovalEventRecord
    ) -> ManagerPreviewApprovalEventWriteStatus:
        row = LaunchplaneManagerPreviewApprovalEventRow(
            event_id=record.event_id,
            approval_id=record.approval_id,
            product=record.binding.product,
            context=record.binding.context,
            repository=record.binding.repository,
            pr_number=record.binding.pr_number,
            head_sha=record.binding.head_sha,
            preview_id=record.binding.preview_id,
            serving_generation_id=record.binding.serving_generation_id,
            artifact_id=record.binding.artifact_id,
            artifact_image_digest=record.binding.artifact_image_digest,
            manifest_fingerprint=record.binding.manifest_fingerprint,
            runtime_identity_sha256=record.binding.runtime_identity_sha256,
            action=record.action,
            manager_github_id=record.manager_github_id,
            manager_login=record.manager_login,
            policy_record_id=record.policy_record_id,
            policy_sha256=record.policy_sha256,
            occurred_at=record.occurred_at,
            payload=self._payload_dict(record),
        )
        with self._session_factory() as session:
            session.add(row)
            try:
                session.commit()
                return "written"
            except IntegrityError:
                session.rollback()
                existing_row = session.get(
                    LaunchplaneManagerPreviewApprovalEventRow,
                    record.event_id,
                )
                if existing_row is None:
                    raise
                existing = self._read_payload(
                    model_type=ManagerPreviewApprovalEventRecord,
                    payload=existing_row.payload,
                )
                if existing != record:
                    raise ManagerPreviewApprovalEventConflictError(
                        "Manager preview approval event replay changed the persisted payload."
                    )
                return "replayed"

    @contextmanager
    def owner_acceptance_projection_lock(
        self,
        *,
        repository_id: str,
        pull_request_number: int,
    ) -> Iterator[None]:
        normalized_repository_id = repository_id.strip()
        if not normalized_repository_id or pull_request_number < 1:
            raise ValueError("Owner acceptance projection lock requires an exact pull request")
        lock_subject = f"repository-id:{normalized_repository_id}:{pull_request_number}"
        if self._engine.url.get_backend_name() == "sqlite":
            database = self._engine.url.database
            database_identity = (
                str(Path(database).expanduser().resolve())
                if database and database != ":memory:"
                else f"memory:{id(self._engine)}"
            )
            lock_key = f"{database_identity}:{lock_subject}"
            with _SQLITE_OWNER_ACCEPTANCE_PROJECTION_LOCKS_GUARD:
                thread_lock = _SQLITE_OWNER_ACCEPTANCE_PROJECTION_LOCKS.setdefault(
                    lock_key,
                    Lock(),
                )
            with thread_lock:
                if not database or database == ":memory:":
                    yield
                    return
                lock_digest = hashlib.sha256(lock_key.encode()).hexdigest()
                database_path = Path(database).expanduser().resolve()
                lock_path = database_path.parent / (
                    f".{database_path.name}.owner-acceptance-{lock_digest}.lock"
                )
                with lock_path.open("a+b") as lock_file:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                    try:
                        yield
                    finally:
                        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            return
        lock_engine = self._owner_acceptance_projection_lock_engine
        if lock_engine is None:
            raise RuntimeError("PostgreSQL projection lock engine is unavailable")
        lock_name = f"launchplane:owner-acceptance-projection:{lock_subject}"
        while True:
            with lock_engine.connect() as connection:
                acquired = bool(
                    connection.scalar(
                        text("select pg_try_advisory_lock(hashtextextended(:lock_name, 0))"),
                        {"lock_name": lock_name},
                    )
                )
                if acquired:
                    connection.commit()
                    try:
                        yield
                    finally:
                        unlocked = bool(
                            connection.scalar(
                                text("select pg_advisory_unlock(hashtextextended(:lock_name, 0))"),
                                {"lock_name": lock_name},
                            )
                        )
                        connection.commit()
                        if not unlocked:
                            raise RuntimeError(
                                "PostgreSQL Owner acceptance projection lock cleanup failed"
                            )
                    return
            time.sleep(0.05)

    def _owner_control_channel_session_row(
        self,
        session: Any,
        *,
        channel_session_id: str,
        for_update: bool = False,
    ) -> LaunchplaneOwnerControlChannelSessionRow | None:
        statement = select(LaunchplaneOwnerControlChannelSessionRow).where(
            LaunchplaneOwnerControlChannelSessionRow.channel_session_id == channel_session_id
        )
        if for_update:
            statement = statement.execution_options(populate_existing=True)
            if not self.database_url.startswith("sqlite"):
                statement = statement.with_for_update()
        return cast(LaunchplaneOwnerControlChannelSessionRow | None, session.scalar(statement))

    def _owner_control_enrollment_provenance_row(
        self,
        session: Any,
        *,
        channel_session_id: str,
        for_update: bool = False,
    ) -> LaunchplaneOwnerControlEnrollmentProvenanceRow | None:
        statement = select(LaunchplaneOwnerControlEnrollmentProvenanceRow).where(
            LaunchplaneOwnerControlEnrollmentProvenanceRow.channel_session_id == channel_session_id
        )
        if for_update:
            statement = statement.execution_options(populate_existing=True)
            if not self.database_url.startswith("sqlite"):
                statement = statement.with_for_update()
        return cast(
            LaunchplaneOwnerControlEnrollmentProvenanceRow | None,
            session.scalar(statement),
        )

    def _owner_control_issued_challenge_row(
        self,
        session: Any,
        *,
        challenge_nonce: str,
        for_update: bool = False,
    ) -> LaunchplaneOwnerControlIssuedChallengeRow | None:
        statement = select(LaunchplaneOwnerControlIssuedChallengeRow).where(
            LaunchplaneOwnerControlIssuedChallengeRow.challenge_nonce == challenge_nonce
        )
        if for_update:
            statement = statement.execution_options(populate_existing=True)
            if not self.database_url.startswith("sqlite"):
                statement = statement.with_for_update()
        return cast(LaunchplaneOwnerControlIssuedChallengeRow | None, session.scalar(statement))

    def _owner_control_active_challenge_row_for_operation(
        self,
        session: Any,
        *,
        operation_id: str,
        for_update: bool = False,
    ) -> LaunchplaneOwnerControlIssuedChallengeRow | None:
        statement = select(LaunchplaneOwnerControlIssuedChallengeRow).where(
            LaunchplaneOwnerControlIssuedChallengeRow.operation_id == operation_id,
            LaunchplaneOwnerControlIssuedChallengeRow.state == "issued",
        )
        if for_update:
            statement = statement.execution_options(populate_existing=True)
            if not self.database_url.startswith("sqlite"):
                statement = statement.with_for_update()
        return cast(LaunchplaneOwnerControlIssuedChallengeRow | None, session.scalar(statement))

    def _locked_privileged_operation_record(
        self,
        session: Any,
        *,
        operation_id: str,
    ) -> PrivilegedOperationRecord:
        statement = select(LaunchplanePrivilegedOperationRow).where(
            LaunchplanePrivilegedOperationRow.operation_id == operation_id
        )
        if not self.database_url.startswith("sqlite"):
            statement = statement.with_for_update()
        row = session.scalar(statement)
        if row is None:
            raise FileNotFoundError(f"Privileged operation {operation_id} was not found.")
        return self._read_payload(model_type=PrivilegedOperationRecord, payload=row.payload)

    def _locked_active_authz_policy_record(
        self,
        session: Any,
    ) -> LaunchplaneAuthzPolicyRecord:
        statement = (
            select(LaunchplaneAuthzPolicyRow)
            .where(LaunchplaneAuthzPolicyRow.status == "active")
            .order_by(desc(LaunchplaneAuthzPolicyRow.revision))
            .limit(2)
        )
        if not self.database_url.startswith("sqlite"):
            statement = statement.with_for_update()
        rows = tuple(session.scalars(statement))
        if len(rows) != 1:
            raise OwnerControlShadowVerifierConflictError(
                "Owner-control issuance requires exactly one active authorization policy."
            )
        return self._read_authz_policy_row(rows[0])

    @staticmethod
    def _owner_control_channel_session_record_from_row(
        row: LaunchplaneOwnerControlChannelSessionRow,
    ) -> OwnerControlChannelSessionRecord:
        return OwnerControlChannelSessionRecord.model_validate(row.payload)

    @staticmethod
    def _owner_control_enrollment_provenance_record_from_row(
        row: LaunchplaneOwnerControlEnrollmentProvenanceRow,
    ) -> OwnerControlEnrollmentProvenanceRecord:
        return OwnerControlEnrollmentProvenanceRecord.model_validate(row.payload)

    @staticmethod
    def _owner_control_issued_challenge_record_from_row(
        row: LaunchplaneOwnerControlIssuedChallengeRow,
    ) -> OwnerControlIssuedChallengeRecord:
        return OwnerControlIssuedChallengeRecord.model_validate(row.payload)

    def _owner_control_channel_session_row_from_record(
        self,
        record: OwnerControlChannelSessionRecord,
    ) -> LaunchplaneOwnerControlChannelSessionRow:
        binding = record.channel_binding()
        return LaunchplaneOwnerControlChannelSessionRow(
            channel_session_id=record.channel_session_id,
            owner_github_id=record.owner_github_id,
            status=record.status,
            session_issued_at=binding.session_issued_at,
            session_expires_at=binding.session_expires_at,
            binding_sha256=record.binding_sha256,
            enrolled_at=record.enrolled_at,
            revoked_at=record.revoked_at,
            authority_state=record.authority_state,
            payload=self._payload_dict(record),
        )

    def _owner_control_enrollment_provenance_row_from_record(
        self,
        record: OwnerControlEnrollmentProvenanceRecord,
    ) -> LaunchplaneOwnerControlEnrollmentProvenanceRow:
        return LaunchplaneOwnerControlEnrollmentProvenanceRow(
            channel_session_id=record.channel_session_id,
            owner_github_id=record.owner_github_id,
            binding_sha256=record.binding_sha256,
            host_principal_claim_sha256=record.host_principal_claim_sha256,
            enrolled_at=record.enrolled_at,
            enrollment_context=record.enrollment_context,
            server_observed_corroboration=record.server_observed_corroboration,
            provenance_tier=record.provenance_tier,
            authority_state=record.authority_state,
            authorizes_execution=record.authorizes_execution,
            payload=self._payload_dict(record),
        )

    def _sync_owner_control_channel_session_row(
        self,
        row: LaunchplaneOwnerControlChannelSessionRow,
        record: OwnerControlChannelSessionRecord,
    ) -> None:
        binding = record.channel_binding()
        row.owner_github_id = record.owner_github_id
        row.status = record.status
        row.session_issued_at = binding.session_issued_at
        row.session_expires_at = binding.session_expires_at
        row.binding_sha256 = record.binding_sha256
        row.enrolled_at = record.enrolled_at
        row.revoked_at = record.revoked_at
        row.authority_state = record.authority_state
        row.payload = self._payload_dict(record)

    def _owner_control_issued_challenge_row_from_record(
        self,
        record: OwnerControlIssuedChallengeRecord,
    ) -> LaunchplaneOwnerControlIssuedChallengeRow:
        return LaunchplaneOwnerControlIssuedChallengeRow(
            challenge_id=record.challenge_id,
            challenge_nonce=record.challenge_nonce,
            channel_session_id=record.channel_session_id,
            operation_id=record.operation_id,
            descriptor_id=record.descriptor_id,
            owner_github_id=record.owner_github_id,
            issued_at=record.issued_at,
            expires_at=record.expires_at,
            approval_request_sha256=record.approval_request_sha256,
            binding_sha256=record.binding_sha256,
            state=record.state,
            attempt_count=record.attempt_count,
            consumed_at=record.consumed_at,
            terminal_event_id=record.terminal_event_id,
            authority_state=record.authority_state,
            payload=self._payload_dict(record),
        )

    def _sync_owner_control_issued_challenge_row(
        self,
        row: LaunchplaneOwnerControlIssuedChallengeRow,
        record: OwnerControlIssuedChallengeRecord,
    ) -> None:
        row.state = record.state
        row.attempt_count = record.attempt_count
        row.consumed_at = record.consumed_at
        row.terminal_event_id = record.terminal_event_id
        row.authority_state = record.authority_state
        row.payload = self._payload_dict(record)

    def _owner_control_challenge_lifecycle_event_row_from_record(
        self,
        record: OwnerControlChallengeLifecycleEventRecord,
    ) -> LaunchplaneOwnerControlChallengeLifecycleEventRow:
        return LaunchplaneOwnerControlChallengeLifecycleEventRow(
            event_id=record.event_id,
            challenge_id=record.challenge_id,
            challenge_nonce=record.challenge_nonce,
            channel_session_id=record.channel_session_id,
            operation_id=record.operation_id,
            approval_request_sha256=record.approval_request_sha256,
            binding_sha256=record.binding_sha256,
            from_state=record.from_state,
            to_state=record.to_state,
            transition_reason=record.transition_reason,
            challenge_expires_at=record.challenge_expires_at,
            occurred_at=record.occurred_at,
            authorizes_execution=record.authorizes_execution,
            authority_state=record.authority_state,
            payload=self._payload_dict(record),
        )

    def enroll_owner_control_channel_session(
        self,
        binding: ChannelBindingRecord,
        *,
        host_principal_claim: OwnerControlHostPrincipalClaim,
    ) -> OwnerControlChannelEnrollment:
        with self._session_factory() as session:
            self._begin_serialized_write(session)
            existing_row = self._owner_control_channel_session_row(
                session,
                channel_session_id=binding.channel_session_id,
                for_update=True,
            )
            if existing_row is not None:
                existing_record = self._owner_control_channel_session_record_from_row(existing_row)
                if not (
                    existing_record.binding_sha256 == owner_control_channel_binding_sha256(binding)
                    and existing_record.channel_binding() == binding
                ):
                    raise OwnerControlShadowVerifierConflictError(
                        "Channel-session enrollment changed the stored binding."
                    )
                provenance_row = self._owner_control_enrollment_provenance_row(
                    session,
                    channel_session_id=binding.channel_session_id,
                    for_update=True,
                )
                if provenance_row is None:
                    raise OwnerControlEnrollmentProvenanceConflictError(
                        "Channel-session enrollment provenance is missing."
                    )
                existing_provenance = self._owner_control_enrollment_provenance_record_from_row(
                    provenance_row
                )
                candidate_provenance = build_owner_control_enrollment_provenance_record(
                    binding=binding,
                    claim=host_principal_claim,
                    enrolled_at=existing_record.enrolled_at,
                )
                if existing_provenance != candidate_provenance:
                    raise OwnerControlEnrollmentProvenanceConflictError(
                        "Channel-session re-enrollment changed immutable provenance."
                    )
                session.rollback()
                return OwnerControlChannelEnrollment(
                    session=existing_record,
                    provenance=existing_provenance,
                )
            enrolled_at = self._owner_control_shadow_timestamp(session)
            record = build_owner_control_channel_session_record(
                binding=binding,
                enrolled_at=enrolled_at,
            )
            provenance = build_owner_control_enrollment_provenance_record(
                binding=binding,
                claim=host_principal_claim,
                enrolled_at=enrolled_at,
            )
            session.add(self._owner_control_channel_session_row_from_record(record))
            session.add(self._owner_control_enrollment_provenance_row_from_record(provenance))
            session.commit()
            return OwnerControlChannelEnrollment(session=record, provenance=provenance)

    def revoke_owner_control_channel_session(
        self,
        *,
        channel_session_id: str,
    ) -> OwnerControlChannelSessionRecord:
        with self._session_factory() as session:
            self._begin_serialized_write(session)
            row = self._owner_control_channel_session_row(
                session,
                channel_session_id=channel_session_id,
                for_update=True,
            )
            if row is None:
                raise FileNotFoundError(
                    f"Owner-control channel session {channel_session_id} was not found."
                )
            record = self._owner_control_channel_session_record_from_row(row)
            revoked_record = revoke_owner_control_channel_session_record(
                record,
                revoked_at=self._owner_control_shadow_timestamp(session),
            )
            if revoked_record != record:
                self._sync_owner_control_channel_session_row(row, revoked_record)
                session.commit()
            else:
                session.rollback()
            return revoked_record

    def issue_owner_control_challenge(
        self,
        issue_request: OwnerControlChallengeIssueRequest,
    ) -> OwnerControlIssuedChallengeRecord:
        with self._session_factory() as session:
            self._begin_serialized_write(session)
            session_row = self._owner_control_channel_session_row(
                session,
                channel_session_id=issue_request.channel_session_id,
                for_update=True,
            )
            if session_row is None:
                raise FileNotFoundError(
                    f"Owner-control channel session {issue_request.channel_session_id} was not found."
                )
            session_record = self._owner_control_channel_session_record_from_row(session_row)
            provenance_row = self._owner_control_enrollment_provenance_row(
                session,
                channel_session_id=issue_request.channel_session_id,
                for_update=True,
            )
            if provenance_row is None:
                raise OwnerControlEnrollmentProvenanceConflictError(
                    "Owner-control challenge issuance requires enrollment provenance."
                )
            OwnerControlChannelEnrollment(
                session=session_record,
                provenance=self._owner_control_enrollment_provenance_record_from_row(
                    provenance_row
                ),
            )
            operation = self._locked_privileged_operation_record(
                session,
                operation_id=issue_request.operation_id,
            )
            policy_record = self._locked_active_authz_policy_record(session)
            existing_row = self._owner_control_active_challenge_row_for_operation(
                session,
                operation_id=operation.operation_id,
                for_update=True,
            )
            issued_at = self._owner_control_shadow_timestamp(session)
            issued_at_value = datetime.fromisoformat(issued_at)
            operation_expires_at = datetime.fromisoformat(operation.expires_at).astimezone(
                timezone.utc
            )
            session_expires_at = datetime.fromisoformat(
                session_record.channel_binding().session_expires_at
            ).astimezone(timezone.utc)
            expires_at_value = min(
                issued_at_value + timedelta(seconds=issue_request.expires_in_seconds),
                operation_expires_at,
                session_expires_at,
            ).replace(microsecond=0)
            expires_at = expires_at_value.isoformat()
            if session_record.status != "enrolled":
                raise OwnerControlShadowVerifierConflictError("Channel session is not enrolled.")
            if operation.status != "planned" or operation_expires_at <= issued_at_value:
                raise OwnerControlShadowVerifierConflictError(
                    "Owner-control challenges require an unexpired planned operation."
                )
            if expires_at_value <= issued_at_value:
                raise OwnerControlShadowVerifierConflictError(
                    "Owner-control challenge expiry does not remain within live provenance."
                )
            try:
                candidate = derive_owner_control_approval_request(
                    operation=operation,
                    policy_record=policy_record,
                    owner_github_id=session_record.owner_github_id,
                    nonce=secrets.token_urlsafe(32),
                    issued_at=issued_at,
                    expires_at=expires_at,
                )
            except OwnerControlChallengeProvenanceError as error:
                raise OwnerControlShadowVerifierConflictError(str(error)) from error
            if existing_row is not None:
                existing_record = self._owner_control_issued_challenge_record_from_row(existing_row)
                existing_expires_at = datetime.fromisoformat(existing_record.expires_at)
                if existing_expires_at <= issued_at_value:
                    terminalized_record, lifecycle_event = (
                        terminalize_expired_owner_control_challenge_record(
                            existing_record,
                            observed_at=issued_at,
                        )
                    )
                    self._sync_owner_control_issued_challenge_row(
                        existing_row,
                        terminalized_record,
                    )
                    session.add(
                        self._owner_control_challenge_lifecycle_event_row_from_record(
                            lifecycle_event
                        )
                    )
                    session.flush()
                else:
                    if (
                        existing_record.channel_session_id == session_record.channel_session_id
                        and existing_record.binding_sha256 == session_record.binding_sha256
                        and existing_expires_at <= expires_at_value
                        and owner_control_challenge_semantics(existing_record.approval_request())
                        == owner_control_challenge_semantics(candidate)
                    ):
                        session.rollback()
                        return existing_record
                    raise OwnerControlShadowVerifierConflictError(
                        "An active owner-control challenge already binds this operation."
                    )
            record = issue_owner_control_challenge_record(
                issue_request=issue_request,
                session=session_record,
                approval_request=candidate,
            )
            session.add(self._owner_control_issued_challenge_row_from_record(record))
            session.flush()
            final_observed_at = self._owner_control_shadow_timestamp(session)
            if datetime.fromisoformat(record.expires_at) <= datetime.fromisoformat(
                final_observed_at
            ):
                session.rollback()
                raise OwnerControlShadowVerifierConflictError(
                    "Owner-control challenge expired before issuance committed."
                )
            session.commit()
            return record

    def verify_owner_control_confirmation_shadow(
        self,
        envelope: OwnerControlConfirmationEnvelope,
    ) -> OwnerControlShadowVerificationResult:
        with self._session_factory() as session:
            self._begin_serialized_write(session)
            observed_challenge_row = self._owner_control_issued_challenge_row(
                session,
                challenge_nonce=envelope.challenge_response.approval_request.nonce,
            )
            if observed_challenge_row is None:
                session.rollback()
                raise FileNotFoundError("Owner-control challenge was not issued.")
            observed_challenge = self._owner_control_issued_challenge_record_from_row(
                observed_challenge_row
            )
            session_row = self._owner_control_channel_session_row(
                session,
                channel_session_id=observed_challenge.channel_session_id,
                for_update=True,
            )
            if session_row is not None:
                provenance_row = self._owner_control_enrollment_provenance_row(
                    session,
                    channel_session_id=observed_challenge.channel_session_id,
                    for_update=True,
                )
                if provenance_row is None:
                    raise OwnerControlEnrollmentProvenanceConflictError(
                        "Owner-control shadow verification requires enrollment provenance."
                    )
                OwnerControlChannelEnrollment(
                    session=self._owner_control_channel_session_record_from_row(session_row),
                    provenance=self._owner_control_enrollment_provenance_record_from_row(
                        provenance_row
                    ),
                )
            challenge_row = self._owner_control_issued_challenge_row(
                session,
                challenge_nonce=observed_challenge.challenge_nonce,
                for_update=True,
            )
            if challenge_row is None:
                session.rollback()
                raise FileNotFoundError("Owner-control challenge was not issued.")
            challenge_record = self._owner_control_issued_challenge_record_from_row(challenge_row)
            if challenge_record.attempt_count >= OWNER_CONTROL_SHADOW_MAX_ATTEMPTS:
                session.rollback()
                raise OwnerControlShadowVerifierConflictError(
                    "Owner-control challenge verification attempt budget is exhausted."
                )
            observed_at = self._owner_control_shadow_timestamp(session)
            evaluation = evaluate_owner_control_shadow_verification(
                envelope=envelope,
                channel_session=(
                    self._owner_control_channel_session_record_from_row(session_row)
                    if session_row is not None
                    else None
                ),
                issued_challenge=challenge_record,
                observed_at=observed_at,
            )
            sequence = challenge_record.attempt_count + 1
            if (
                evaluation.verification_status == "rejected"
                and evaluation.resulting_challenge_state == "issued"
                and sequence == OWNER_CONTROL_SHADOW_MAX_ATTEMPTS
            ):
                evaluation = OwnerControlShadowVerificationEvaluation(
                    verification_status="rejected",
                    rejection_reason="attempt_budget_exhausted",
                    resulting_challenge_state="rejected",
                )
            envelope_sha256 = owner_control_confirmation_envelope_sha256(envelope)
            event_id = owner_control_verification_event_id(
                challenge_id=challenge_record.challenge_id,
                sequence=sequence,
                envelope_sha256=envelope_sha256,
                verification_status=evaluation.verification_status,
                rejection_reason=evaluation.rejection_reason,
            )
            first_terminal_transition = (
                challenge_record.state == "issued"
                and evaluation.resulting_challenge_state != "issued"
            )
            updated_challenge = challenge_record.model_copy(
                update={
                    "attempt_count": sequence,
                    "state": evaluation.resulting_challenge_state,
                    "consumed_at": (
                        observed_at
                        if evaluation.resulting_challenge_state == "consumed"
                        and challenge_record.consumed_at is None
                        else challenge_record.consumed_at
                    ),
                    "terminal_event_id": (
                        event_id
                        if first_terminal_transition
                        else challenge_record.terminal_event_id
                    ),
                }
            )
            self._sync_owner_control_issued_challenge_row(challenge_row, updated_challenge)
            event = OwnerControlShadowVerificationEventRecord(
                event_id=event_id,
                challenge_id=challenge_record.challenge_id,
                sequence=sequence,
                channel_session_id=challenge_record.channel_session_id,
                challenge_nonce=challenge_record.challenge_nonce,
                envelope_sha256=envelope_sha256,
                approval_request_sha256=challenge_record.approval_request_sha256,
                binding_sha256=challenge_record.binding_sha256,
                verification_status=evaluation.verification_status,
                rejection_reason=evaluation.rejection_reason,
                resulting_challenge_state=evaluation.resulting_challenge_state,
                occurred_at=observed_at,
            )
            session.add(
                LaunchplaneOwnerControlShadowVerificationEventRow(
                    event_id=event.event_id,
                    challenge_id=event.challenge_id,
                    sequence=event.sequence,
                    channel_session_id=event.channel_session_id,
                    challenge_nonce=event.challenge_nonce,
                    envelope_sha256=event.envelope_sha256,
                    approval_request_sha256=event.approval_request_sha256,
                    binding_sha256=event.binding_sha256,
                    verification_status=event.verification_status,
                    rejection_reason=event.rejection_reason,
                    resulting_challenge_state=event.resulting_challenge_state,
                    occurred_at=event.occurred_at,
                    verifier_mode=event.verifier_mode,
                    authorizes_execution=event.authorizes_execution,
                    authority_state=event.authority_state,
                    payload=self._payload_dict(event),
                )
            )
            session.commit()
            return OwnerControlShadowVerificationResult(
                event_id=event.event_id,
                challenge_id=event.challenge_id,
                sequence=event.sequence,
                verification_status=event.verification_status,
                rejection_reason=event.rejection_reason,
                resulting_challenge_state=event.resulting_challenge_state,
            )

    def read_owner_control_channel_session(
        self,
        *,
        channel_session_id: str,
    ) -> OwnerControlChannelSessionRecord:
        return self._read_model(
            model_type=OwnerControlChannelSessionRecord,
            orm_model=LaunchplaneOwnerControlChannelSessionRow,
            filters=(
                LaunchplaneOwnerControlChannelSessionRow.channel_session_id == channel_session_id,
            ),
        )

    def read_owner_control_enrollment_provenance(
        self,
        *,
        channel_session_id: str,
    ) -> OwnerControlEnrollmentProvenanceRecord:
        return self._read_model(
            model_type=OwnerControlEnrollmentProvenanceRecord,
            orm_model=LaunchplaneOwnerControlEnrollmentProvenanceRow,
            filters=(
                LaunchplaneOwnerControlEnrollmentProvenanceRow.channel_session_id
                == channel_session_id,
            ),
        )

    def read_owner_control_issued_challenge(
        self,
        *,
        challenge_nonce: str,
    ) -> OwnerControlIssuedChallengeRecord:
        return self._read_model(
            model_type=OwnerControlIssuedChallengeRecord,
            orm_model=LaunchplaneOwnerControlIssuedChallengeRow,
            filters=(LaunchplaneOwnerControlIssuedChallengeRow.challenge_nonce == challenge_nonce,),
        )

    @staticmethod
    def _administrator_enrollment_row(
        record: AdministratorEnrollmentRecord,
    ) -> LaunchplaneAdministratorEnrollmentRow:
        return LaunchplaneAdministratorEnrollmentRow(
            enrollment_id=record.enrollment_id,
            state=record.state,
            proposer_github_id=record.proposer_github_id,
            candidate_github_id=record.candidate_github_id,
            challenge_sha256=record.challenge_sha256,
            reason=record.reason,
            provenance_sha256=record.provenance_sha256,
            created_at=record.created_at,
            expires_at=record.expires_at,
            control_proven_at=record.control_proven_at,
            withdrawn_at=record.withdrawn_at,
            expired_at=record.expired_at,
            enrolled_at=record.enrolled_at,
            enrolled_policy_record_id=record.enrolled_policy_record_id,
            enrolled_policy_revision=record.enrolled_policy_revision,
            enrolled_policy_sha256=record.enrolled_policy_sha256,
            reviewed_plan_sha256=record.reviewed_plan_sha256,
            bridge_idempotency_key_sha256=record.bridge_idempotency_key_sha256,
            authority_state=record.authority_state,
            authorizes_policy=record.authorizes_policy,
            policy_bridge_state=record.policy_bridge_state,
            payload=PostgresRecordStore._payload_dict(record),
        )

    @staticmethod
    def _sync_administrator_enrollment_row(
        row: LaunchplaneAdministratorEnrollmentRow, record: AdministratorEnrollmentRecord
    ) -> None:
        row.state = record.state
        row.candidate_github_id = record.candidate_github_id
        row.control_proven_at = record.control_proven_at
        row.withdrawn_at = record.withdrawn_at
        row.expired_at = record.expired_at
        row.enrolled_at = record.enrolled_at
        row.enrolled_policy_record_id = record.enrolled_policy_record_id
        row.enrolled_policy_revision = record.enrolled_policy_revision
        row.enrolled_policy_sha256 = record.enrolled_policy_sha256
        row.reviewed_plan_sha256 = record.reviewed_plan_sha256
        row.bridge_idempotency_key_sha256 = record.bridge_idempotency_key_sha256
        row.policy_bridge_state = record.policy_bridge_state
        row.payload = PostgresRecordStore._payload_dict(record)

    def create_administrator_enrollment_if_absent(
        self, record: AdministratorEnrollmentRecord
    ) -> tuple[AdministratorEnrollmentRecord, bool]:
        with self._session_factory() as session:
            session.add(self._administrator_enrollment_row(record))
            try:
                session.commit()
                return record, True
            except IntegrityError as error:
                session.rollback()
                existing_row = session.get(
                    LaunchplaneAdministratorEnrollmentRow, record.enrollment_id
                )
                if existing_row is None:
                    raise AdministratorEnrollmentConflictError(
                        "administrator enrollment challenge digest is already reserved"
                    ) from error
                existing = AdministratorEnrollmentRecord.model_validate(existing_row.payload)
                if existing != record:
                    raise AdministratorEnrollmentConflictError(
                        "administrator enrollment creation conflicts with persisted record"
                    ) from error
                return existing, False

    def read_administrator_enrollment(self, enrollment_id: str) -> AdministratorEnrollmentRecord:
        return self._read_model(
            model_type=AdministratorEnrollmentRecord,
            orm_model=LaunchplaneAdministratorEnrollmentRow,
            filters=(LaunchplaneAdministratorEnrollmentRow.enrollment_id == enrollment_id,),
        )

    def _transition_administrator_enrollment(
        self,
        enrollment_id: str,
        transition: Callable[[AdministratorEnrollmentRecord], AdministratorEnrollmentRecord],
    ) -> AdministratorEnrollmentRecord:
        with self._session_factory() as session:
            self._begin_serialized_write(session)
            statement = (
                select(LaunchplaneAdministratorEnrollmentRow)
                .where(LaunchplaneAdministratorEnrollmentRow.enrollment_id == enrollment_id)
                .limit(1)
            )
            if not self.database_url.startswith("sqlite"):
                statement = statement.with_for_update()
            row = session.scalar(statement)
            if row is None:
                raise FileNotFoundError(enrollment_id)
            current = AdministratorEnrollmentRecord.model_validate(row.payload)
            updated = transition(current)
            if updated != current:
                self._sync_administrator_enrollment_row(row, updated)
                session.commit()
            return updated

    def prove_administrator_enrollment_control(
        self,
        *,
        enrollment_id: str,
        challenge: str,
        server_derived_candidate_github_id: int,
        control_proven_at: str,
    ) -> AdministratorEnrollmentRecord:
        return self._transition_administrator_enrollment(
            enrollment_id,
            lambda record: prove_administrator_enrollment_control(
                record,
                challenge=challenge,
                server_derived_candidate_github_id=server_derived_candidate_github_id,
                control_proven_at=control_proven_at,
            ),
        )

    def expire_administrator_enrollment(
        self, *, enrollment_id: str, expired_at: str
    ) -> AdministratorEnrollmentRecord:
        return self._transition_administrator_enrollment(
            enrollment_id,
            lambda record: expire_administrator_enrollment(record, expired_at=expired_at),
        )

    def withdraw_administrator_enrollment(
        self, *, enrollment_id: str, proposer_github_id: int, withdrawn_at: str
    ) -> AdministratorEnrollmentRecord:
        return self._transition_administrator_enrollment(
            enrollment_id,
            lambda record: withdraw_administrator_enrollment(
                record, proposer_github_id=proposer_github_id, withdrawn_at=withdrawn_at
            ),
        )

    def complete_administrator_enrollment(
        self,
        *,
        enrollment_id: str,
        server_derived_candidate_github_id: int,
        enrolled_at: str,
        enrolled_policy_record_id: str,
        enrolled_policy_revision: int,
        enrolled_policy_sha256: str,
        reviewed_plan_sha256: str,
        bridge_idempotency_key_sha256: str,
    ) -> AdministratorEnrollmentRecord:
        return self._transition_administrator_enrollment(
            enrollment_id,
            lambda record: complete_administrator_enrollment(
                record,
                server_derived_candidate_github_id=server_derived_candidate_github_id,
                enrolled_at=enrolled_at,
                enrolled_policy_record_id=enrolled_policy_record_id,
                enrolled_policy_revision=enrolled_policy_revision,
                enrolled_policy_sha256=enrolled_policy_sha256,
                reviewed_plan_sha256=reviewed_plan_sha256,
                bridge_idempotency_key_sha256=bridge_idempotency_key_sha256,
            ),
        )

    @staticmethod
    def _solo_administration_confirmation_row(
        record: SoloAdministrationConfirmationRecord,
    ) -> LaunchplaneSoloAdministrationConfirmationRow:
        return LaunchplaneSoloAdministrationConfirmationRow(
            confirmation_id=record.confirmation_id,
            state=record.state,
            active_policy_record_id=record.active_policy_record_id,
            active_policy_revision=record.active_policy_revision,
            active_policy_sha256=record.active_policy_sha256,
            candidate_policy_sha256=record.candidate_policy_sha256,
            candidate_administrator_quorum=record.candidate_administrator_quorum,
            candidate_distinct_human_administrator_count=(
                record.candidate_distinct_human_administrator_count
            ),
            reviewed_plan_sha256=record.reviewed_plan_sha256,
            human_session_id_sha256=record.human_session_id_sha256,
            github_id=record.github_id,
            idempotency_scope_sha256=record.idempotency_scope_sha256,
            idempotency_key_sha256=record.idempotency_key_sha256,
            acknowledgement_sha256=record.acknowledgement_sha256,
            secret_sha256=record.secret_sha256,
            created_at=record.created_at,
            expires_at=record.expires_at,
            terminal_at=record.terminal_at,
            authority_state=record.authority_state,
            authorizes_policy=record.authorizes_policy,
            payload=PostgresRecordStore._payload_dict(record),
        )

    @staticmethod
    def _solo_administration_confirmation_event_row(
        event: SoloAdministrationConfirmationLifecycleEventRecord,
    ) -> LaunchplaneSoloAdministrationConfirmationLifecycleEventRow:
        return LaunchplaneSoloAdministrationConfirmationLifecycleEventRow(
            event_id=event.event_id,
            confirmation_id=event.confirmation_id,
            event_type=event.event_type,
            from_state=event.from_state,
            to_state=event.to_state,
            occurred_at=event.occurred_at,
            authority_state=event.authority_state,
            authorizes_policy=event.authorizes_policy,
            payload=PostgresRecordStore._payload_dict(event),
        )

    @staticmethod
    def _sync_solo_administration_confirmation_row(
        row: LaunchplaneSoloAdministrationConfirmationRow,
        record: SoloAdministrationConfirmationRecord,
    ) -> None:
        row.state = record.state
        row.terminal_at = record.terminal_at
        row.payload = PostgresRecordStore._payload_dict(record)

    def issue_solo_administration_confirmation(
        self,
        record: SoloAdministrationConfirmationRecord,
    ) -> tuple[SoloAdministrationConfirmationRecord, bool]:
        with self._session_factory() as session:
            self._begin_serialized_write(session)
            session.add(self._solo_administration_confirmation_row(record))
            session.add(
                self._solo_administration_confirmation_event_row(
                    build_solo_administration_confirmation_lifecycle_event(
                        record=record,
                        event_type="issued",
                        occurred_at=record.created_at,
                    )
                )
            )
            try:
                session.commit()
                return record, True
            except IntegrityError as error:
                session.rollback()
                existing_row = session.get(
                    LaunchplaneSoloAdministrationConfirmationRow,
                    record.confirmation_id,
                )
                if existing_row is None:
                    existing_row = session.scalar(
                        select(LaunchplaneSoloAdministrationConfirmationRow)
                        .where(
                            LaunchplaneSoloAdministrationConfirmationRow.state == "issued",
                            LaunchplaneSoloAdministrationConfirmationRow.reviewed_plan_sha256
                            == record.reviewed_plan_sha256,
                            LaunchplaneSoloAdministrationConfirmationRow.human_session_id_sha256
                            == record.human_session_id_sha256,
                            LaunchplaneSoloAdministrationConfirmationRow.idempotency_scope_sha256
                            == record.idempotency_scope_sha256,
                            LaunchplaneSoloAdministrationConfirmationRow.idempotency_key_sha256
                            == record.idempotency_key_sha256,
                        )
                        .limit(1)
                    )
                if existing_row is None:
                    raise SoloAdministrationConfirmationConflictError(
                        "solo-administration confirmation binding is already reserved"
                    ) from error
                existing = SoloAdministrationConfirmationRecord.model_validate(existing_row.payload)
                if existing != record:
                    raise SoloAdministrationConfirmationConflictError(
                        "solo-administration confirmation creation conflicts with persisted evidence"
                    ) from error
                return existing, False

    def read_solo_administration_confirmation(
        self,
        confirmation_id: str,
    ) -> SoloAdministrationConfirmationRecord:
        return self._read_model(
            model_type=SoloAdministrationConfirmationRecord,
            orm_model=LaunchplaneSoloAdministrationConfirmationRow,
            filters=(
                LaunchplaneSoloAdministrationConfirmationRow.confirmation_id == confirmation_id,
            ),
        )

    def has_consumed_solo_administration_confirmation(
        self,
        *,
        candidate_policy_sha256: str,
        github_id: int,
        idempotency_scope_sha256: str,
    ) -> bool:
        normalized_digest = candidate_policy_sha256.strip().lower()
        if len(normalized_digest) != 64 or any(
            character not in "0123456789abcdef" for character in normalized_digest
        ):
            raise ValueError("candidate_policy_sha256 must be a lowercase SHA-256 digest")
        normalized_scope_digest = idempotency_scope_sha256.strip().lower()
        if len(normalized_scope_digest) != 64 or any(
            character not in "0123456789abcdef" for character in normalized_scope_digest
        ):
            raise ValueError("idempotency_scope_sha256 must be a lowercase SHA-256 digest")
        if github_id < 1:
            raise ValueError("github_id must be positive")
        with self._session_factory() as session:
            return (
                session.scalar(
                    select(LaunchplaneSoloAdministrationConfirmationRow.confirmation_id)
                    .where(
                        LaunchplaneSoloAdministrationConfirmationRow.state == "consumed",
                        LaunchplaneSoloAdministrationConfirmationRow.candidate_policy_sha256
                        == normalized_digest,
                        LaunchplaneSoloAdministrationConfirmationRow.github_id == github_id,
                        LaunchplaneSoloAdministrationConfirmationRow.idempotency_scope_sha256
                        == normalized_scope_digest,
                    )
                    .limit(1)
                )
                is not None
            )

    def _transition_solo_administration_confirmation(
        self,
        confirmation_id: str,
        transition: Callable[
            [SoloAdministrationConfirmationRecord], SoloAdministrationConfirmationRecord
        ],
    ) -> SoloAdministrationConfirmationRecord:
        with self._session_factory() as session:
            self._begin_serialized_write(session)
            statement = (
                select(LaunchplaneSoloAdministrationConfirmationRow)
                .where(
                    LaunchplaneSoloAdministrationConfirmationRow.confirmation_id == confirmation_id
                )
                .limit(1)
            )
            if not self.database_url.startswith("sqlite"):
                statement = statement.with_for_update()
            row = session.scalar(statement)
            if row is None:
                raise FileNotFoundError(confirmation_id)
            current = SoloAdministrationConfirmationRecord.model_validate(row.payload)
            updated = transition(current)
            if updated != current:
                self._sync_solo_administration_confirmation_row(row, updated)
                if updated.state == "issued" or updated.terminal_at is None:
                    raise RuntimeError("Confirmation transition did not produce terminal evidence.")
                session.add(
                    self._solo_administration_confirmation_event_row(
                        build_solo_administration_confirmation_lifecycle_event(
                            record=updated,
                            event_type=updated.state,
                            occurred_at=updated.terminal_at,
                        )
                    )
                )
                session.commit()
            return updated

    def revoke_solo_administration_confirmation(
        self, *, confirmation_id: str, terminal_at: str
    ) -> SoloAdministrationConfirmationRecord:
        return self._transition_solo_administration_confirmation(
            confirmation_id,
            lambda record: revoke_solo_administration_confirmation(record, terminal_at=terminal_at),
        )

    def expire_solo_administration_confirmation(
        self, *, confirmation_id: str, terminal_at: str
    ) -> SoloAdministrationConfirmationRecord:
        return self._transition_solo_administration_confirmation(
            confirmation_id,
            lambda record: expire_solo_administration_confirmation(record, terminal_at=terminal_at),
        )

    def consume_solo_administration_confirmation(
        self,
        *,
        confirmation_id: str,
        active_policy_record_id: str,
        active_policy_revision: int,
        active_policy_sha256: str,
        candidate_policy_sha256: str,
        candidate_administrator_quorum: int,
        candidate_distinct_human_administrator_count: int,
        reviewed_plan_sha256: str,
        human_session_id_sha256: str,
        github_id: int,
        idempotency_scope_sha256: str,
        idempotency_key_sha256: str,
        acknowledgement_sha256: str,
        secret_sha256: str,
        terminal_at: str,
    ) -> SoloAdministrationConfirmationRecord:
        return self._transition_solo_administration_confirmation(
            confirmation_id,
            lambda record: consume_solo_administration_confirmation(
                record,
                active_policy_record_id=active_policy_record_id,
                active_policy_revision=active_policy_revision,
                active_policy_sha256=active_policy_sha256,
                candidate_policy_sha256=candidate_policy_sha256,
                candidate_administrator_quorum=candidate_administrator_quorum,
                candidate_distinct_human_administrator_count=(
                    candidate_distinct_human_administrator_count
                ),
                reviewed_plan_sha256=reviewed_plan_sha256,
                human_session_id_sha256=human_session_id_sha256,
                github_id=github_id,
                idempotency_scope_sha256=idempotency_scope_sha256,
                idempotency_key_sha256=idempotency_key_sha256,
                acknowledgement_sha256=acknowledgement_sha256,
                secret_sha256=secret_sha256,
                terminal_at=terminal_at,
            ),
        )

    def list_solo_administration_confirmation_lifecycle_events(
        self,
        *,
        confirmation_id: str = "",
        limit: int | None = None,
    ) -> tuple[SoloAdministrationConfirmationLifecycleEventRecord, ...]:
        filters: list[object] = []
        if confirmation_id:
            filters.append(
                LaunchplaneSoloAdministrationConfirmationLifecycleEventRow.confirmation_id
                == confirmation_id
            )
        return self._list_models(
            model_type=SoloAdministrationConfirmationLifecycleEventRecord,
            orm_model=LaunchplaneSoloAdministrationConfirmationLifecycleEventRow,
            filters=filters,
            order_by=(
                LaunchplaneSoloAdministrationConfirmationLifecycleEventRow.occurred_at,
                LaunchplaneSoloAdministrationConfirmationLifecycleEventRow.event_id,
            ),
            limit=limit,
        )

    def list_owner_control_challenge_lifecycle_events(
        self,
        *,
        challenge_nonce: str = "",
        operation_id: str = "",
        limit: int | None = None,
    ) -> tuple[OwnerControlChallengeLifecycleEventRecord, ...]:
        filters: list[object] = []
        if challenge_nonce:
            filters.append(
                LaunchplaneOwnerControlChallengeLifecycleEventRow.challenge_nonce == challenge_nonce
            )
        if operation_id:
            filters.append(
                LaunchplaneOwnerControlChallengeLifecycleEventRow.operation_id == operation_id
            )
        return self._list_models(
            model_type=OwnerControlChallengeLifecycleEventRecord,
            orm_model=LaunchplaneOwnerControlChallengeLifecycleEventRow,
            filters=filters,
            order_by=(
                LaunchplaneOwnerControlChallengeLifecycleEventRow.occurred_at.desc(),
                LaunchplaneOwnerControlChallengeLifecycleEventRow.event_id.desc(),
            ),
            limit=limit,
        )

    def list_owner_control_shadow_verification_events(
        self,
        *,
        challenge_nonce: str = "",
        channel_session_id: str = "",
        limit: int | None = None,
    ) -> tuple[OwnerControlShadowVerificationEventRecord, ...]:
        filters: list[object] = []
        if challenge_nonce:
            filters.append(
                LaunchplaneOwnerControlShadowVerificationEventRow.challenge_nonce == challenge_nonce
            )
        if channel_session_id:
            filters.append(
                LaunchplaneOwnerControlShadowVerificationEventRow.channel_session_id
                == channel_session_id
            )
        return self._list_models(
            model_type=OwnerControlShadowVerificationEventRecord,
            orm_model=LaunchplaneOwnerControlShadowVerificationEventRow,
            filters=filters,
            order_by=(
                LaunchplaneOwnerControlShadowVerificationEventRow.occurred_at.desc(),
                LaunchplaneOwnerControlShadowVerificationEventRow.event_id.desc(),
            ),
            limit=limit,
        )

    def _sync_privileged_operation_row(
        self,
        row: LaunchplanePrivilegedOperationRow,
        record: PrivilegedOperationRecord,
    ) -> None:
        row.descriptor_id = record.descriptor_id
        row.status = record.status
        row.requester_github_id = getattr(record.requested_by, "github_id", 0)
        row.created_at = record.created_at
        row.updated_at = record.updated_at
        row.expires_at = record.expires_at
        row.payload = self._payload_dict(record)

    def write_privileged_operation_plan(
        self,
        record: PrivilegedOperationRecord,
        event: PrivilegedOperationEventRecord,
    ) -> PrivilegedOperationEventWriteStatus:
        try:
            with self._session_factory() as session:
                self._begin_serialized_write(session)
                existing_row = session.get(
                    LaunchplanePrivilegedOperationRow,
                    record.operation_id,
                )
                if existing_row is not None:
                    existing_record = self._read_payload(
                        model_type=PrivilegedOperationRecord,
                        payload=existing_row.payload,
                    )
                    if privileged_operation_plan_replay_digest(
                        existing_record
                    ) != privileged_operation_plan_replay_digest(record):
                        raise PrivilegedOperationConflictError(
                            "Privileged-operation plan replay changed the persisted payload."
                        )
                    existing_event_row = session.get(
                        LaunchplanePrivilegedOperationEventRow,
                        event.event_id,
                    )
                    if existing_event_row is None:
                        raise PrivilegedOperationConflictError(
                            "Privileged-operation plan exists without its planned event."
                        )
                    existing_event = self._read_payload(
                        model_type=PrivilegedOperationEventRecord,
                        payload=existing_event_row.payload,
                    )
                    if privileged_operation_event_replay_digest(
                        existing_event
                    ) != privileged_operation_event_replay_digest(event):
                        raise PrivilegedOperationConflictError(
                            "Privileged-operation planned event replay changed the persisted payload."
                        )
                    return "replayed"
                validate_privileged_operation_transition(
                    previous=None,
                    proposed=record,
                    event=event,
                )
                session.add(
                    LaunchplanePrivilegedOperationRow(
                        operation_id=record.operation_id,
                        descriptor_id=record.descriptor_id,
                        status=record.status,
                        requester_github_id=getattr(record.requested_by, "github_id", 0),
                        created_at=record.created_at,
                        updated_at=record.updated_at,
                        expires_at=record.expires_at,
                        payload=self._payload_dict(record),
                    )
                )
                session.add(
                    LaunchplanePrivilegedOperationEventRow(
                        event_id=event.event_id,
                        operation_id=event.operation_id,
                        sequence=event.sequence,
                        action=event.action,
                        occurred_at=event.occurred_at,
                        payload=self._payload_dict(event),
                    )
                )
                session.commit()
                return "written"
        except IntegrityError as error:
            try:
                existing_record = self.read_privileged_operation_record(record.operation_id)
                existing_event = next(
                    stored_event
                    for stored_event in self.list_privileged_operation_event_records(
                        operation_id=record.operation_id,
                        limit=2,
                    )
                    if stored_event.event_id == event.event_id
                )
            except (FileNotFoundError, StopIteration) as replay_error:
                raise PrivilegedOperationConflictError(
                    "Privileged-operation plan write conflicted with another request."
                ) from replay_error
            if privileged_operation_plan_replay_digest(
                existing_record
            ) == privileged_operation_plan_replay_digest(
                record
            ) and privileged_operation_event_replay_digest(
                existing_event
            ) == privileged_operation_event_replay_digest(event):
                return "replayed"
            raise PrivilegedOperationConflictError(
                "Privileged-operation plan write conflicted with another payload."
            ) from error

    def transition_privileged_operation(
        self,
        record: PrivilegedOperationRecord,
        event: PrivilegedOperationEventRecord,
    ) -> PrivilegedOperationEventWriteStatus:
        with self._session_factory() as session:
            self._begin_serialized_write(session)
            statement = select(LaunchplanePrivilegedOperationRow).where(
                LaunchplanePrivilegedOperationRow.operation_id == record.operation_id
            )
            if not self.database_url.startswith("sqlite"):
                statement = statement.with_for_update()
            row = session.scalar(statement)
            if row is None:
                raise FileNotFoundError(record.operation_id)
            previous = self._read_payload(
                model_type=PrivilegedOperationRecord,
                payload=row.payload,
            )
            existing_event_row = session.get(
                LaunchplanePrivilegedOperationEventRow,
                event.event_id,
            )
            if existing_event_row is not None:
                existing_event = self._read_payload(
                    model_type=PrivilegedOperationEventRecord,
                    payload=existing_event_row.payload,
                )
                if privileged_operation_event_replay_digest(
                    existing_event
                ) != privileged_operation_event_replay_digest(event):
                    raise PrivilegedOperationConflictError(
                        "Privileged-operation event replay changed the persisted payload."
                    )
                if privileged_operation_record_digest(
                    previous
                ) != privileged_operation_record_digest(record):
                    raise PrivilegedOperationConflictError(
                        "Privileged-operation event replay does not match current state."
                    )
                return "replayed"
            try:
                validate_privileged_operation_transition(
                    previous=previous,
                    proposed=record,
                    event=event,
                )
            except PrivilegedOperationTransitionError as error:
                raise PrivilegedOperationConflictError(str(error)) from error
            session.add(
                LaunchplanePrivilegedOperationEventRow(
                    event_id=event.event_id,
                    operation_id=event.operation_id,
                    sequence=event.sequence,
                    action=event.action,
                    occurred_at=event.occurred_at,
                    payload=self._payload_dict(event),
                )
            )
            self._sync_privileged_operation_row(row, record)
            session.commit()
            return "written"

    def read_privileged_operation_record(
        self,
        operation_id: str,
    ) -> PrivilegedOperationRecord:
        with self._session_factory() as session:
            row = session.get(LaunchplanePrivilegedOperationRow, operation_id)
        if row is None:
            raise FileNotFoundError(operation_id)
        return self._read_payload(
            model_type=PrivilegedOperationRecord,
            payload=row.payload,
        )

    def list_privileged_operation_records(
        self,
        *,
        status: str = "",
        descriptor_id: str = "",
        limit: int | None = None,
    ) -> tuple[PrivilegedOperationRecord, ...]:
        statement = select(LaunchplanePrivilegedOperationRow)
        if status:
            statement = statement.where(LaunchplanePrivilegedOperationRow.status == status)
        if descriptor_id:
            statement = statement.where(
                LaunchplanePrivilegedOperationRow.descriptor_id == descriptor_id
            )
        statement = statement.order_by(
            LaunchplanePrivilegedOperationRow.created_at.desc(),
            LaunchplanePrivilegedOperationRow.operation_id.desc(),
        )
        if limit is not None:
            statement = statement.limit(limit)
        with self._session_factory() as session:
            rows = session.scalars(statement).all()
        return tuple(
            self._read_payload(model_type=PrivilegedOperationRecord, payload=row.payload)
            for row in rows
        )

    def list_privileged_operation_event_records(
        self,
        *,
        operation_id: str = "",
        limit: int | None = None,
    ) -> tuple[PrivilegedOperationEventRecord, ...]:
        statement = select(LaunchplanePrivilegedOperationEventRow)
        if operation_id:
            statement = statement.where(
                LaunchplanePrivilegedOperationEventRow.operation_id == operation_id
            )
        statement = statement.order_by(
            LaunchplanePrivilegedOperationEventRow.occurred_at.desc(),
            LaunchplanePrivilegedOperationEventRow.sequence.desc(),
            LaunchplanePrivilegedOperationEventRow.event_id.desc(),
        )
        if limit is not None:
            statement = statement.limit(limit)
        with self._session_factory() as session:
            rows = session.scalars(statement).all()
        return tuple(
            self._read_payload(model_type=PrivilegedOperationEventRecord, payload=row.payload)
            for row in rows
        )

    def write_privileged_operation_worker_heartbeat_record(
        self,
        record: PrivilegedOperationWorkerHeartbeatRecord,
        *,
        prune_before: str,
        prune_after: str,
    ) -> None:
        with self._session_factory() as session:
            session.execute(
                delete(LaunchplanePrivilegedOperationWorkerHeartbeatRow).where(
                    LaunchplanePrivilegedOperationWorkerHeartbeatRow.worker_kind
                    == record.worker_kind,
                    or_(
                        LaunchplanePrivilegedOperationWorkerHeartbeatRow.last_poll_succeeded_at
                        < prune_before,
                        LaunchplanePrivilegedOperationWorkerHeartbeatRow.last_poll_succeeded_at
                        > prune_after,
                    ),
                )
            )
            session.merge(
                LaunchplanePrivilegedOperationWorkerHeartbeatRow(
                    worker_identity_sha256=record.worker_identity_sha256,
                    worker_kind=record.worker_kind,
                    image_reference=record.image_reference,
                    last_poll_succeeded_at=record.last_poll_succeeded_at,
                    payload=self._payload_dict(record),
                )
            )
            session.commit()

    def list_privileged_operation_worker_heartbeat_records(
        self,
        *,
        worker_kind: str = "",
        limit: int | None = None,
    ) -> tuple[PrivilegedOperationWorkerHeartbeatRecord, ...]:
        filters: list[object] = []
        if worker_kind:
            filters.append(
                LaunchplanePrivilegedOperationWorkerHeartbeatRow.worker_kind == worker_kind
            )
        return self._list_models(
            model_type=PrivilegedOperationWorkerHeartbeatRecord,
            orm_model=LaunchplanePrivilegedOperationWorkerHeartbeatRow,
            filters=filters,
            order_by=(
                LaunchplanePrivilegedOperationWorkerHeartbeatRow.last_poll_succeeded_at.desc(),
                LaunchplanePrivilegedOperationWorkerHeartbeatRow.worker_identity_sha256.asc(),
            ),
            limit=limit,
        )

    def write_owner_acceptance_event_record(
        self, record: OwnerAcceptanceEventRecord
    ) -> OwnerAcceptanceEventWriteStatus:
        with self._session_factory() as session:
            existing_row = session.get(LaunchplaneOwnerAcceptanceEventRow, record.event_id)
            if existing_row is not None:
                existing = self._owner_acceptance_record_from_row(existing_row)
                if not owner_acceptance_event_replay_matches(existing, record):
                    raise OwnerAcceptanceEventConflictError(
                        "Owner acceptance event replay changed the persisted payload."
                    )
                return "replayed"

            subject_sequence = self._next_owner_acceptance_subject_sequence(
                session=session,
                record=record,
            )
            existing_row = session.get(LaunchplaneOwnerAcceptanceEventRow, record.event_id)
            if existing_row is not None:
                session.rollback()
                existing = self._owner_acceptance_record_from_row(existing_row)
                if not owner_acceptance_event_replay_matches(existing, record):
                    raise OwnerAcceptanceEventConflictError(
                        "Owner acceptance event replay changed the persisted payload."
                    )
                return "replayed"

            previous_row = session.scalars(
                select(LaunchplaneOwnerAcceptanceEventRow)
                .where(
                    LaunchplaneOwnerAcceptanceEventRow.repository_id
                    == record.binding.repository_id,
                    LaunchplaneOwnerAcceptanceEventRow.pr_number
                    == record.binding.pull_request_number,
                    LaunchplaneOwnerAcceptanceEventRow.product == record.binding.product,
                    LaunchplaneOwnerAcceptanceEventRow.system == record.binding.system,
                    LaunchplaneOwnerAcceptanceEventRow.owner_action == record.binding.action,
                    LaunchplaneOwnerAcceptanceEventRow.environment == record.binding.environment,
                )
                .order_by(LaunchplaneOwnerAcceptanceEventRow.subject_sequence.desc())
                .limit(1)
            ).first()
            persisted_record = record.model_copy(update={"subject_sequence": subject_sequence})
            validate_owner_acceptance_event_transition(
                previous=(
                    self._owner_acceptance_record_from_row(previous_row)
                    if previous_row is not None
                    else None
                ),
                proposed=persisted_record,
            )
            session.add(self._owner_acceptance_row(persisted_record))
            try:
                session.commit()
                return "written"
            except IntegrityError:
                session.rollback()
                existing_row = session.get(LaunchplaneOwnerAcceptanceEventRow, record.event_id)
                if existing_row is None:
                    raise
                existing = self._owner_acceptance_record_from_row(existing_row)
                if not owner_acceptance_event_replay_matches(existing, record):
                    raise OwnerAcceptanceEventConflictError(
                        "Owner acceptance event replay changed the persisted payload."
                    )
                return "replayed"

    @staticmethod
    def _next_owner_acceptance_subject_sequence(
        *,
        session: Any,
        record: OwnerAcceptanceEventRecord,
    ) -> int:
        values = {
            "repository_id": record.binding.repository_id,
            "pr_number": record.binding.pull_request_number,
            "product": record.binding.product,
            "system": record.binding.system,
            "owner_action": record.binding.action,
            "environment": record.binding.environment,
            "last_sequence": 1,
        }
        index_elements = (
            LaunchplaneOwnerAcceptanceSubjectSequenceRow.repository_id,
            LaunchplaneOwnerAcceptanceSubjectSequenceRow.pr_number,
            LaunchplaneOwnerAcceptanceSubjectSequenceRow.product,
            LaunchplaneOwnerAcceptanceSubjectSequenceRow.system,
            LaunchplaneOwnerAcceptanceSubjectSequenceRow.owner_action,
            LaunchplaneOwnerAcceptanceSubjectSequenceRow.environment,
        )
        dialect_name = session.get_bind().dialect.name
        if dialect_name == "postgresql":
            statement = (
                postgresql_insert(LaunchplaneOwnerAcceptanceSubjectSequenceRow)
                .values(**values)
                .on_conflict_do_update(
                    index_elements=index_elements,
                    set_={
                        "last_sequence": (
                            LaunchplaneOwnerAcceptanceSubjectSequenceRow.last_sequence + 1
                        )
                    },
                )
                .returning(LaunchplaneOwnerAcceptanceSubjectSequenceRow.last_sequence)
            )
            return int(session.execute(statement).scalar_one())
        if dialect_name == "sqlite":
            sqlite_statement = (
                sqlite_insert(LaunchplaneOwnerAcceptanceSubjectSequenceRow)
                .values(**values)
                .on_conflict_do_update(
                    index_elements=index_elements,
                    set_={
                        "last_sequence": (
                            LaunchplaneOwnerAcceptanceSubjectSequenceRow.last_sequence + 1
                        )
                    },
                )
                .returning(LaunchplaneOwnerAcceptanceSubjectSequenceRow.last_sequence)
            )
            return int(session.execute(sqlite_statement).scalar_one())
        raise RuntimeError(
            "Owner acceptance subject sequencing requires PostgreSQL or SQLite storage."
        )

    def _owner_acceptance_row(
        self,
        record: OwnerAcceptanceEventRecord,
    ) -> LaunchplaneOwnerAcceptanceEventRow:
        review_context = record.binding.review_context
        return LaunchplaneOwnerAcceptanceEventRow(
            event_id=record.event_id,
            acceptance_id=record.acceptance_id,
            subject_sequence=record.subject_sequence,
            binding_sha256=record.binding.binding_sha256,
            repository_id=record.binding.repository_id,
            repository_owner_id=record.binding.repository_owner_id,
            repository=record.binding.repository,
            pr_number=record.binding.pull_request_number,
            head_sha=record.binding.head_sha,
            tree_sha=record.binding.tree_sha,
            product=record.binding.product,
            system=record.binding.system,
            owner_action=record.binding.action,
            environment=record.binding.environment,
            action=record.action,
            owner_github_id=(record.authorization.owner_github_id if record.authorization else 0),
            owner_login=(record.authorization.owner_login if record.authorization else ""),
            base_ref=(review_context.base_ref if review_context else ""),
            base_sha=(review_context.base_sha if review_context else ""),
            change_class=(review_context.change_class if review_context else ""),
            review_max_age_seconds=(review_context.review_max_age_seconds if review_context else 0),
            contribution_resolution=(
                review_context.contributions.resolution if review_context else ""
            ),
            preview_isolation_class=(
                review_context.preview_isolation.isolation_class if review_context else ""
            ),
            self_review=bool(record.authorization and record.authorization.self_review),
            occurred_at=record.occurred_at,
            payload=self._payload_dict(record),
        )

    def _owner_acceptance_record_from_row(
        self,
        row: LaunchplaneOwnerAcceptanceEventRow,
    ) -> OwnerAcceptanceEventRecord:
        payload = dict(row.payload)
        payload["subject_sequence"] = row.subject_sequence
        return self._read_payload(
            model_type=OwnerAcceptanceEventRecord,
            payload=payload,
        )

    def read_owner_acceptance_event_record(
        self,
        event_id: str,
    ) -> OwnerAcceptanceEventRecord:
        with self._session_factory() as session:
            row = session.get(LaunchplaneOwnerAcceptanceEventRow, event_id)
            if row is None:
                raise FileNotFoundError(event_id)
            return self._owner_acceptance_record_from_row(row)

    def _repository_human_role_policy_row(
        self, record: RepositoryHumanRolePolicyRecord
    ) -> LaunchplaneRepositoryHumanRolePolicyRow:
        return LaunchplaneRepositoryHumanRolePolicyRow(
            record_id=record.record_id,
            repository_id=record.repository_id,
            repository_owner_id=record.repository_owner_id,
            repository=record.repository,
            product=record.product,
            context=record.context,
            status=record.status,
            role_policy_revision=record.role_policy_revision,
            effective_at=record.effective_at,
            source=record.source,
            supersedes_record_id=record.supersedes_record_id,
            role_policy_digest=record.role_policy_digest,
            payload=self._payload_dict(record),
        )

    def _sync_repository_human_role_policy_row(
        self,
        row: LaunchplaneRepositoryHumanRolePolicyRow,
        record: RepositoryHumanRolePolicyRecord,
    ) -> None:
        row.repository_id = record.repository_id
        row.repository_owner_id = record.repository_owner_id
        row.repository = record.repository
        row.product = record.product
        row.context = record.context
        row.status = record.status
        row.role_policy_revision = record.role_policy_revision
        row.effective_at = record.effective_at
        row.source = record.source
        row.supersedes_record_id = record.supersedes_record_id
        row.role_policy_digest = record.role_policy_digest
        row.payload = self._payload_dict(record)

    def _tenant_technical_human_waiver_event_row(
        self, record: TenantTechnicalHumanWaiverEventRecord
    ) -> LaunchplaneTenantTechnicalHumanWaiverEventRow:
        binding = record.binding
        authorization = record.authorization
        return LaunchplaneTenantTechnicalHumanWaiverEventRow(
            event_id=record.event_id,
            repository_id=binding.repository_id,
            repository_owner_id=binding.repository_owner_id,
            repository=binding.repository,
            product=binding.product,
            context=binding.context,
            waiver_id=record.waiver_id,
            binding_sha256=binding.binding_sha256,
            pull_request_number=binding.pull_request_number,
            head_sha=binding.head_sha,
            classification_revision=binding.classification_revision,
            classification_digest=binding.classification_digest,
            role_policy_record_id=binding.role_policy_record_id,
            role_policy_revision=binding.role_policy_revision,
            role_policy_digest=binding.role_policy_digest,
            authz_policy_record_id=binding.authz_policy_record_id,
            authz_policy_revision=binding.authz_policy_revision,
            authz_policy_digest=binding.authz_policy_digest,
            action=record.action,
            author_github_id=authorization.author_github_id,
            author_login=authorization.author_login,
            managed_set_id=authorization.managed_set_id,
            managed_rule_id=authorization.managed_rule_id,
            authorized_at=authorization.authorized_at,
            occurred_at=record.occurred_at,
            expires_at=record.expires_at,
            source_event_kind=record.source_event_kind,
            source_event_id=record.source_event_id,
            event_digest=record.event_digest,
            payload=self._payload_dict(record),
        )

    def _lock_tenant_technical_human_waiver_binding(
        self,
        session: Any,
        *,
        binding_sha256: str,
    ) -> None:
        if self.database_url.startswith("sqlite"):
            return
        lock_parts = (
            "launchplane",
            "tenant-technical-human-waiver",
            binding_sha256,
        )
        session.execute(
            text("select pg_advisory_xact_lock(hashtextextended(:lock_name, 0))"),
            {"lock_name": "".join(f"{len(lock_part)}:{lock_part}" for lock_part in lock_parts)},
        )

    def _locked_current_classification_rows(
        self,
        *,
        session: Any,
        repository_id: str,
    ) -> tuple[LaunchplaneTenantRepositoryClassificationRow, ...]:
        statement = (
            select(LaunchplaneTenantRepositoryClassificationRow)
            .where(LaunchplaneTenantRepositoryClassificationRow.repository_id == repository_id)
            .order_by(LaunchplaneTenantRepositoryClassificationRow.classification_revision.asc())
        )
        if not self.database_url.startswith("sqlite"):
            statement = statement.with_for_update()
        return tuple(session.scalars(statement).all())

    def _locked_active_authz_policy_rows(
        self,
        *,
        session: Any,
    ) -> tuple[LaunchplaneAuthzPolicyRow, ...]:
        statement = (
            select(LaunchplaneAuthzPolicyRow)
            .where(LaunchplaneAuthzPolicyRow.status == "active")
            .order_by(desc(LaunchplaneAuthzPolicyRow.revision))
        )
        if not self.database_url.startswith("sqlite"):
            statement = statement.with_for_update()
        return tuple(session.scalars(statement).all())

    def _locked_tenant_technical_human_waiver_event_rows(
        self,
        *,
        session: Any,
        repository_id: str,
        repository_owner_id: str,
        repository: str,
        product: str,
        context_name: str,
        pull_request_number: int,
        head_sha: str,
    ) -> tuple[LaunchplaneTenantTechnicalHumanWaiverEventRow, ...]:
        statement = (
            select(LaunchplaneTenantTechnicalHumanWaiverEventRow)
            .where(
                LaunchplaneTenantTechnicalHumanWaiverEventRow.repository_id == repository_id,
                LaunchplaneTenantTechnicalHumanWaiverEventRow.repository_owner_id
                == repository_owner_id,
                LaunchplaneTenantTechnicalHumanWaiverEventRow.repository == repository,
                LaunchplaneTenantTechnicalHumanWaiverEventRow.product == product,
                LaunchplaneTenantTechnicalHumanWaiverEventRow.context == context_name,
                LaunchplaneTenantTechnicalHumanWaiverEventRow.pull_request_number
                == pull_request_number,
                LaunchplaneTenantTechnicalHumanWaiverEventRow.head_sha == head_sha,
            )
            .order_by(
                LaunchplaneTenantTechnicalHumanWaiverEventRow.occurred_at.asc(),
                LaunchplaneTenantTechnicalHumanWaiverEventRow.event_id.asc(),
            )
        )
        if not self.database_url.startswith("sqlite"):
            statement = statement.with_for_update()
        return tuple(session.scalars(statement).all())

    def compare_and_write_tenant_technical_human_waiver_event(
        self,
        *,
        identity: GitHubHumanIdentity,
        envelope: TenantTechnicalHumanWaiverApplyEnvelope,
        mutation: DbOnlyMutationRequest,
    ) -> TenantTechnicalHumanWaiverCompareWriteResult:
        if not 100 <= mutation.response_status_code <= 599:
            raise ValueError("DB-only mutation response status must be between 100 and 599.")
        if not mutation.response_trace_id.strip():
            raise ValueError("DB-only mutation response trace id is required.")
        with self._session_factory() as session:
            self._begin_serialized_write(session)
            reservation_status, reservation_row, mutation_reservation = (
                self._reserve_db_only_mutation_in_session(
                    session=session,
                    mutation=mutation,
                )
            )
            if reservation_status == "idempotency_conflict":
                return TenantTechnicalHumanWaiverCompareWriteResult(
                    status="idempotency_conflict",
                    idempotency_record=mutation_reservation,
                )
            if reservation_status == "replayed":
                return TenantTechnicalHumanWaiverCompareWriteResult(
                    status="replayed",
                    idempotency_record=mutation_reservation,
                )
            if reservation_status == "reservation_in_progress":
                return TenantTechnicalHumanWaiverCompareWriteResult(
                    status="reservation_in_progress",
                    idempotency_record=mutation_reservation,
                )
            if reservation_status == "reconciliation_required":
                return TenantTechnicalHumanWaiverCompareWriteResult(
                    status="reconciliation_required",
                    idempotency_record=mutation_reservation,
                )
            if reservation_row is None:
                raise RuntimeError(
                    "Tenant technical human waiver mutation reservation missing row."
                )

            return self._compare_and_write_tenant_technical_human_waiver_locked(
                session=session,
                identity=identity,
                envelope=envelope,
                reservation_row=reservation_row,
                mutation_reservation=mutation_reservation,
                mutation=mutation,
            )

    def _compare_and_write_tenant_technical_human_waiver_locked(
        self,
        *,
        session: Any,
        identity: GitHubHumanIdentity,
        envelope: TenantTechnicalHumanWaiverApplyEnvelope,
        reservation_row: LaunchplaneIdempotencyRow,
        mutation_reservation: LaunchplaneIdempotencyRecord,
        mutation: DbOnlyMutationRequest,
    ) -> TenantTechnicalHumanWaiverCompareWriteResult:
        candidate = envelope.candidate
        self._lock_tenant_repository_classification_write(
            session,
            repository_id=candidate.repository_id,
        )
        self._lock_repository_human_role_policy_write(
            session,
            repository_id=candidate.repository_id,
            product=candidate.product,
            context_name=candidate.context,
        )
        self._lock_active_authz_policy(session)
        observed_at = self._database_mutation_timestamp(session)
        classification_rows = self._locked_current_classification_rows(
            session=session,
            repository_id=candidate.repository_id,
        )
        role_policy_rows = self._repository_human_role_policy_stream_rows(
            session=session,
            repository_id=candidate.repository_id,
            product=candidate.product,
            context_name=candidate.context,
            for_update=True,
        )
        authz_policy_rows = self._locked_active_authz_policy_rows(session=session)
        authority_snapshot = _TenantTechnicalHumanWaiverAuthoritySnapshot(
            classifications=tuple(
                self._read_payload(
                    model_type=TenantRepositoryClassificationRecord,
                    payload=row.payload,
                )
                for row in classification_rows
            ),
            role_policies=tuple(
                self._read_payload(
                    model_type=RepositoryHumanRolePolicyRecord,
                    payload=row.payload,
                )
                for row in role_policy_rows
            ),
            authz_policies=tuple(self._read_authz_policy_row(row) for row in authz_policy_rows),
        )
        try:
            current = tenant_technical_human_waiver_current_authority(
                store=authority_snapshot,
                candidate=candidate,
                expected_authority=envelope.expected_authority,
                evaluated_at=observed_at,
            )
            provisional_event = capture_tenant_technical_human_waiver_event(
                identity=identity,
                candidate=candidate,
                classification=current.classification,
                role_policy_record=current.role_policy_record,
                authz_policy_record=current.authz_policy_record,
                action=envelope.action,
                occurred_at=observed_at,
                source_event_kind=envelope.source_event_kind,
                source_event_id=envelope.source_event_id,
                reason=envelope.reason,
                recorded_at=observed_at,
                expires_at=envelope.expires_at,
            )
        except (
            TenantTechnicalHumanWaiverAuthorizationError,
            TenantTechnicalHumanWaiverEventConflictError,
            TenantTechnicalHumanWaiverRevokeCurrentError,
            TenantTechnicalHumanWaiverStaleAuthorityError,
            ValueError,
        ):
            session.delete(reservation_row)
            session.commit()
            raise

        self._lock_tenant_technical_human_waiver_binding(
            session,
            binding_sha256=provisional_event.record.binding.binding_sha256,
        )
        event_rows = self._locked_tenant_technical_human_waiver_event_rows(
            session=session,
            repository_id=candidate.repository_id,
            repository_owner_id=candidate.repository_owner_id,
            repository=candidate.repository,
            product=candidate.product,
            context_name=candidate.context,
            pull_request_number=candidate.pull_request_number,
            head_sha=candidate.head_sha,
        )
        events = tuple(
            self._read_payload(
                model_type=TenantTechnicalHumanWaiverEventRecord,
                payload=row.payload,
            )
            for row in event_rows
        )
        try:
            result = build_tenant_technical_human_waiver_apply_result(
                identity=identity,
                envelope=envelope,
                classification=current.classification,
                role_policy_record=current.role_policy_record,
                authz_policy_record=current.authz_policy_record,
                events=events,
                observed_at=observed_at,
            )
            event_record = capture_tenant_technical_human_waiver_event(
                identity=identity,
                candidate=candidate,
                classification=current.classification,
                role_policy_record=current.role_policy_record,
                authz_policy_record=current.authz_policy_record,
                action=envelope.action,
                occurred_at=observed_at,
                source_event_kind=envelope.source_event_kind,
                source_event_id=envelope.source_event_id,
                reason=envelope.reason,
                recorded_at=observed_at,
                expires_at=envelope.expires_at,
            ).record
            if event_record.event_id != result.event_id:
                raise RuntimeError("Tenant technical human waiver result/event identity mismatch.")
            append_plan = plan_tenant_technical_human_waiver_event_append(
                records=events,
                record=event_record,
            )
        except (
            TenantTechnicalHumanWaiverAuthorizationError,
            TenantTechnicalHumanWaiverEventConflictError,
            TenantTechnicalHumanWaiverRevokeCurrentError,
            TenantTechnicalHumanWaiverStaleAuthorityError,
            ValueError,
        ):
            session.delete(reservation_row)
            session.commit()
            raise

        if append_plan.status != "replayed":
            session.add(self._tenant_technical_human_waiver_event_row(event_record))
            session.flush()
            self._after_tenant_technical_human_waiver_write_step("insert_event")
        response_payload = mutation.response_payload | {
            "result": result.model_dump(mode="json"),
        }
        completed_at = self._database_mutation_timestamp(session)
        completion = complete_launchplane_mutation_reservation(
            mutation_reservation,
            response_status_code=mutation.response_status_code,
            response_trace_id=mutation.response_trace_id,
            completed_at=completed_at,
            response_payload=response_payload,
        )
        self._sync_idempotency_row(reservation_row, completion)
        self._after_tenant_technical_human_waiver_write_step("complete_idempotency")
        session.commit()
        return TenantTechnicalHumanWaiverCompareWriteResult(
            status="exact_replay" if append_plan.status == "replayed" else "written",
            result=result,
            event_record=event_record,
            idempotency_record=completion,
        )

    def _lock_repository_human_role_policy_write(
        self,
        session: Any,
        *,
        repository_id: str,
        product: str,
        context_name: str,
    ) -> None:
        if self.database_url.startswith("sqlite"):
            return
        lock_parts = (
            "launchplane",
            "repository-human-role-policy",
            repository_id,
            product,
            context_name,
        )
        session.execute(
            text("select pg_advisory_xact_lock(hashtextextended(:lock_name, 0))"),
            {"lock_name": "".join(f"{len(lock_part)}:{lock_part}" for lock_part in lock_parts)},
        )

    def _repository_human_role_policy_stream_rows(
        self,
        *,
        session: Any,
        repository_id: str,
        product: str,
        context_name: str,
        for_update: bool = False,
    ) -> tuple[LaunchplaneRepositoryHumanRolePolicyRow, ...]:
        statement = (
            select(LaunchplaneRepositoryHumanRolePolicyRow)
            .where(
                LaunchplaneRepositoryHumanRolePolicyRow.repository_id == repository_id,
                LaunchplaneRepositoryHumanRolePolicyRow.product == product,
                LaunchplaneRepositoryHumanRolePolicyRow.context == context_name,
            )
            .order_by(LaunchplaneRepositoryHumanRolePolicyRow.role_policy_revision.asc())
        )
        if for_update and not self.database_url.startswith("sqlite"):
            statement = statement.with_for_update()
        return tuple(session.scalars(statement).all())

    def _reserve_db_only_mutation_in_session(
        self,
        *,
        session: Any,
        mutation: DbOnlyMutationRequest,
    ) -> tuple[str, LaunchplaneIdempotencyRow | None, LaunchplaneIdempotencyRecord]:
        observed_at = self._database_mutation_timestamp(session)
        reservation = build_launchplane_mutation_reservation(
            scope=mutation.scope,
            route_path=mutation.route_path,
            idempotency_key=mutation.idempotency_key,
            request_fingerprint=mutation.request_fingerprint,
            lease_owner=mutation.lease_owner,
            lease_expires_at=self._mutation_lease_expiry(
                observed_at=observed_at,
                lease_seconds=mutation.lease_seconds,
            ),
            reserved_at=observed_at,
        )
        reservation_row = self._idempotency_row(reservation)
        session.add(reservation_row)
        try:
            session.flush()
            return "acquired", reservation_row, reservation
        except IntegrityError:
            session.rollback()

        self._begin_serialized_write(session)
        reservation_row = session.scalar(
            self._idempotency_statement(
                scope=mutation.scope,
                route_path=mutation.route_path,
                idempotency_key=mutation.idempotency_key,
                for_update=True,
            )
        )
        if reservation_row is None:
            raise RuntimeError("Mutation reservation collision disappeared before replay read.")
        current_reservation = self._read_payload(
            model_type=LaunchplaneIdempotencyRecord,
            payload=reservation_row.payload,
        )
        if current_reservation.request_fingerprint != mutation.request_fingerprint:
            return "idempotency_conflict", reservation_row, current_reservation
        if current_reservation.state == "completed":
            return "replayed", reservation_row, current_reservation
        if current_reservation.state == "reconcile_required":
            return "reconciliation_required", reservation_row, current_reservation
        observed_at = self._database_mutation_timestamp(session)
        if parse_launchplane_mutation_timestamp(
            current_reservation.lease_expires_at,
            field_name="lease_expires_at",
        ) > parse_launchplane_mutation_timestamp(
            observed_at,
            field_name="observed_at",
        ):
            return "reservation_in_progress", reservation_row, current_reservation
        if current_reservation.reconciliation_key:
            reconcile_record = self._updated_idempotency_record(
                current_reservation,
                state="reconcile_required",
                updated_at=observed_at,
            )
            self._sync_idempotency_row(reservation_row, reconcile_record)
            session.commit()
            return "reconciliation_required", reservation_row, reconcile_record
        reclaimed_reservation = self._updated_idempotency_record(
            current_reservation,
            lease_owner=mutation.lease_owner,
            lease_expires_at=self._mutation_lease_expiry(
                observed_at=observed_at,
                lease_seconds=mutation.lease_seconds,
            ),
            attempt=current_reservation.attempt + 1,
            updated_at=observed_at,
            response_status_code=None,
            response_trace_id="",
            recorded_at="",
            response_payload={},
        )
        self._sync_idempotency_row(reservation_row, reclaimed_reservation)
        return "acquired", reservation_row, reclaimed_reservation

    def compare_and_write_repository_human_role_policy_record(
        self,
        *,
        record: RepositoryHumanRolePolicyRecord,
        expected_current_record_id: str,
        expected_current_role_policy_digest: str,
        mutation: DbOnlyMutationRequest,
    ) -> RepositoryHumanRolePolicyCompareWriteResult:
        if not 100 <= mutation.response_status_code <= 599:
            raise ValueError("DB-only mutation response status must be between 100 and 599.")
        if not mutation.response_trace_id.strip():
            raise ValueError("DB-only mutation response trace id is required.")
        normalized_expected_record_id = expected_current_record_id.strip()
        normalized_expected_digest = expected_current_role_policy_digest.strip().lower()

        reservation_insert_error: IntegrityError | None = None
        with self._session_factory() as session:
            self._begin_serialized_write(session)
            observed_at = self._database_mutation_timestamp(session)
            stored_reservation = build_launchplane_mutation_reservation(
                scope=mutation.scope,
                route_path=mutation.route_path,
                idempotency_key=mutation.idempotency_key,
                request_fingerprint=mutation.request_fingerprint,
                lease_owner=mutation.lease_owner,
                lease_expires_at=self._mutation_lease_expiry(
                    observed_at=observed_at,
                    lease_seconds=mutation.lease_seconds,
                ),
                reserved_at=observed_at,
            )
            reservation_row = self._idempotency_row(stored_reservation)
            session.add(reservation_row)
            try:
                session.flush()
            except IntegrityError as error:
                session.rollback()
                reservation_insert_error = error
            if reservation_insert_error is None:
                return self._compare_and_write_repository_human_role_policy_locked(
                    session=session,
                    record=record,
                    expected_current_record_id=normalized_expected_record_id,
                    expected_current_role_policy_digest=normalized_expected_digest,
                    reservation_row=reservation_row,
                    mutation_reservation=stored_reservation,
                    mutation=mutation,
                )

        with self._session_factory() as session:
            self._begin_serialized_write(session)
            reservation_row = session.scalar(
                self._idempotency_statement(
                    scope=mutation.scope,
                    route_path=mutation.route_path,
                    idempotency_key=mutation.idempotency_key,
                    for_update=True,
                )
            )
            if reservation_row is None:
                assert reservation_insert_error is not None
                raise reservation_insert_error
            current_reservation = self._read_payload(
                model_type=LaunchplaneIdempotencyRecord,
                payload=reservation_row.payload,
            )
            if current_reservation.request_fingerprint != mutation.request_fingerprint:
                return RepositoryHumanRolePolicyCompareWriteResult(
                    status="idempotency_conflict",
                    idempotency_record=current_reservation,
                )
            if current_reservation.state == "completed":
                return RepositoryHumanRolePolicyCompareWriteResult(
                    status="replayed",
                    idempotency_record=current_reservation,
                )
            if current_reservation.state == "reconcile_required":
                return RepositoryHumanRolePolicyCompareWriteResult(
                    status="reconciliation_required",
                    idempotency_record=current_reservation,
                )
            observed_at = self._database_mutation_timestamp(session)
            if parse_launchplane_mutation_timestamp(
                current_reservation.lease_expires_at,
                field_name="lease_expires_at",
            ) > parse_launchplane_mutation_timestamp(
                observed_at,
                field_name="observed_at",
            ):
                return RepositoryHumanRolePolicyCompareWriteResult(
                    status="reservation_in_progress",
                    idempotency_record=current_reservation,
                )
            if current_reservation.reconciliation_key:
                reconcile_record = self._updated_idempotency_record(
                    current_reservation,
                    state="reconcile_required",
                    updated_at=observed_at,
                )
                self._sync_idempotency_row(reservation_row, reconcile_record)
                session.commit()
                return RepositoryHumanRolePolicyCompareWriteResult(
                    status="reconciliation_required",
                    idempotency_record=reconcile_record,
                )
            reclaimed_reservation = self._updated_idempotency_record(
                current_reservation,
                lease_owner=mutation.lease_owner,
                lease_expires_at=self._mutation_lease_expiry(
                    observed_at=observed_at,
                    lease_seconds=mutation.lease_seconds,
                ),
                attempt=current_reservation.attempt + 1,
                updated_at=observed_at,
                response_status_code=None,
                response_trace_id="",
                recorded_at="",
                response_payload={},
            )
            self._sync_idempotency_row(reservation_row, reclaimed_reservation)
            return self._compare_and_write_repository_human_role_policy_locked(
                session=session,
                record=record,
                expected_current_record_id=normalized_expected_record_id,
                expected_current_role_policy_digest=normalized_expected_digest,
                reservation_row=reservation_row,
                mutation_reservation=reclaimed_reservation,
                mutation=mutation,
            )

    def _compare_and_write_repository_human_role_policy_locked(
        self,
        *,
        session: Any,
        record: RepositoryHumanRolePolicyRecord,
        expected_current_record_id: str,
        expected_current_role_policy_digest: str,
        reservation_row: LaunchplaneIdempotencyRow,
        mutation_reservation: LaunchplaneIdempotencyRecord,
        mutation: DbOnlyMutationRequest,
    ) -> RepositoryHumanRolePolicyCompareWriteResult:
        self._lock_repository_human_role_policy_write(
            session,
            repository_id=record.repository_id,
            product=record.product,
            context_name=record.context,
        )
        rows = self._repository_human_role_policy_stream_rows(
            session=session,
            repository_id=record.repository_id,
            product=record.product,
            context_name=record.context,
            for_update=True,
        )
        existing_records = tuple(
            self._read_payload(
                model_type=RepositoryHumanRolePolicyRecord,
                payload=row.payload,
            )
            for row in rows
        )
        try:
            plan = plan_repository_human_role_policy_apply(
                records=existing_records,
                record=record,
                expected_current_record_id=expected_current_record_id,
                expected_current_role_policy_digest=expected_current_role_policy_digest,
            )
        except (
            RepositoryHumanRolePolicyConflictError,
            RepositoryHumanRolePolicySequenceError,
            ValueError,
        ):
            session.delete(reservation_row)
            session.commit()
            raise

        exact_replay = plan.status == "replayed"
        if not exact_replay:
            if plan.superseded_current_record is not None:
                current_row = next(
                    row for row in rows if row.record_id == plan.superseded_current_record.record_id
                )
                self._sync_repository_human_role_policy_row(
                    current_row,
                    plan.superseded_current_record,
                )
                session.flush()
            session.add(self._repository_human_role_policy_row(record))
            session.flush()
        completed_at = self._database_mutation_timestamp(session)
        completion = complete_launchplane_mutation_reservation(
            mutation_reservation,
            response_status_code=mutation.response_status_code,
            response_trace_id=mutation.response_trace_id,
            completed_at=completed_at,
            response_payload=(
                mutation.replay_response_payload
                if exact_replay and mutation.replay_response_payload is not None
                else mutation.response_payload
            ),
        )
        self._sync_idempotency_row(reservation_row, completion)
        session.commit()
        return RepositoryHumanRolePolicyCompareWriteResult(
            status="exact_replay" if exact_replay else "written",
            idempotency_record=completion,
        )

    def write_repository_human_role_policy_record(
        self,
        record: RepositoryHumanRolePolicyRecord,
    ) -> Literal["written", "replayed"]:
        insert_error: IntegrityError | None = None
        with self._session_factory() as session:
            self._begin_serialized_write(session)
            self._lock_repository_human_role_policy_write(
                session,
                repository_id=record.repository_id,
                product=record.product,
                context_name=record.context,
            )
            rows = self._repository_human_role_policy_stream_rows(
                session=session,
                repository_id=record.repository_id,
                product=record.product,
                context_name=record.context,
                for_update=True,
            )
            records = tuple(
                self._read_payload(
                    model_type=RepositoryHumanRolePolicyRecord,
                    payload=row.payload,
                )
                for row in rows
            )
            plan = plan_repository_human_role_policy_append(records=records, record=record)
            if plan.status == "replayed":
                session.rollback()
                return "replayed"
            if plan.superseded_current_record is not None:
                current_row = next(
                    row for row in rows if row.record_id == plan.superseded_current_record.record_id
                )
                self._sync_repository_human_role_policy_row(
                    current_row,
                    plan.superseded_current_record,
                )
                session.flush()
            session.add(self._repository_human_role_policy_row(record))
            try:
                session.flush()
                session.commit()
                return "written"
            except IntegrityError as error:
                session.rollback()
                insert_error = error

        current_records = self.list_repository_human_role_policy_records(
            repository_id=record.repository_id,
            product=record.product,
            context=record.context,
        )
        replay_plan = plan_repository_human_role_policy_append(
            records=current_records,
            record=record,
        )
        if replay_plan.status == "replayed":
            return "replayed"
        assert insert_error is not None
        raise insert_error

    def read_repository_human_role_policy_record(
        self,
        record_id: str,
    ) -> RepositoryHumanRolePolicyRecord:
        return self._read_model(
            model_type=RepositoryHumanRolePolicyRecord,
            orm_model=LaunchplaneRepositoryHumanRolePolicyRow,
            filters=(LaunchplaneRepositoryHumanRolePolicyRow.record_id == record_id,),
        )

    def list_repository_human_role_policy_records(
        self,
        *,
        repository_id: str = "",
        repository_owner_id: str = "",
        repository: str = "",
        product: str = "",
        context: str = "",
        status: str = "",
        limit: int | None = None,
    ) -> tuple[RepositoryHumanRolePolicyRecord, ...]:
        filters: list[object] = []
        normalized_repository = repository.strip().lower()
        if repository_id:
            filters.append(LaunchplaneRepositoryHumanRolePolicyRow.repository_id == repository_id)
        if repository_owner_id:
            filters.append(
                LaunchplaneRepositoryHumanRolePolicyRow.repository_owner_id == repository_owner_id
            )
        if normalized_repository:
            filters.append(
                LaunchplaneRepositoryHumanRolePolicyRow.repository == normalized_repository
            )
        if product:
            filters.append(LaunchplaneRepositoryHumanRolePolicyRow.product == product)
        if context:
            filters.append(LaunchplaneRepositoryHumanRolePolicyRow.context == context)
        if status:
            filters.append(LaunchplaneRepositoryHumanRolePolicyRow.status == status)
        return self._list_models(
            model_type=RepositoryHumanRolePolicyRecord,
            orm_model=LaunchplaneRepositoryHumanRolePolicyRow,
            filters=filters,
            order_by=(
                LaunchplaneRepositoryHumanRolePolicyRow.role_policy_revision.desc(),
                LaunchplaneRepositoryHumanRolePolicyRow.repository_id.desc(),
                LaunchplaneRepositoryHumanRolePolicyRow.product.desc(),
                LaunchplaneRepositoryHumanRolePolicyRow.context.desc(),
                LaunchplaneRepositoryHumanRolePolicyRow.record_id.desc(),
            ),
            limit=limit,
        )

    def write_tenant_technical_human_waiver_event_record(
        self,
        record: TenantTechnicalHumanWaiverEventRecord,
    ) -> Literal["written", "replayed"]:
        insert_error: IntegrityError | None = None
        with self._session_factory() as session:
            self._begin_serialized_write(session)
            statement = select(LaunchplaneTenantTechnicalHumanWaiverEventRow).where(
                LaunchplaneTenantTechnicalHumanWaiverEventRow.event_id == record.event_id
            )
            if not self.database_url.startswith("sqlite"):
                statement = statement.with_for_update()
            existing_row = session.scalar(statement)
            if existing_row is not None:
                existing_record = self._read_payload(
                    model_type=TenantTechnicalHumanWaiverEventRecord,
                    payload=existing_row.payload,
                )
                plan = plan_tenant_technical_human_waiver_event_append(
                    records=(existing_record,),
                    record=record,
                )
                session.rollback()
                return plan.status
            session.add(self._tenant_technical_human_waiver_event_row(record))
            try:
                session.flush()
                session.commit()
                return "written"
            except IntegrityError as error:
                session.rollback()
                insert_error = error

        try:
            existing_record = self.read_tenant_technical_human_waiver_event_record(record.event_id)
        except FileNotFoundError as read_error:
            assert insert_error is not None
            raise insert_error from read_error
        replay_plan = plan_tenant_technical_human_waiver_event_append(
            records=(existing_record,),
            record=record,
        )
        if replay_plan.status == "replayed":
            return "replayed"
        assert insert_error is not None
        raise insert_error

    def read_tenant_technical_human_waiver_event_record(
        self,
        event_id: str,
    ) -> TenantTechnicalHumanWaiverEventRecord:
        return self._read_model(
            model_type=TenantTechnicalHumanWaiverEventRecord,
            orm_model=LaunchplaneTenantTechnicalHumanWaiverEventRow,
            filters=(LaunchplaneTenantTechnicalHumanWaiverEventRow.event_id == event_id,),
        )

    def list_tenant_technical_human_waiver_event_records(
        self,
        *,
        repository_id: str = "",
        repository_owner_id: str = "",
        repository: str = "",
        product: str = "",
        context: str = "",
        waiver_id: str = "",
        binding_sha256: str = "",
        pull_request_number: int | None = None,
        head_sha: str = "",
        classification_digest: str = "",
        role_policy_record_id: str = "",
        role_policy_digest: str = "",
        authz_policy_record_id: str = "",
        authz_policy_digest: str = "",
        action: str = "",
        author_github_id: int | None = None,
        limit: int | None = None,
    ) -> tuple[TenantTechnicalHumanWaiverEventRecord, ...]:
        filters: list[object] = []
        normalized_repository = repository.strip().lower()
        if repository_id:
            filters.append(
                LaunchplaneTenantTechnicalHumanWaiverEventRow.repository_id == repository_id
            )
        if repository_owner_id:
            filters.append(
                LaunchplaneTenantTechnicalHumanWaiverEventRow.repository_owner_id
                == repository_owner_id
            )
        if normalized_repository:
            filters.append(
                LaunchplaneTenantTechnicalHumanWaiverEventRow.repository == normalized_repository
            )
        if product:
            filters.append(LaunchplaneTenantTechnicalHumanWaiverEventRow.product == product)
        if context:
            filters.append(LaunchplaneTenantTechnicalHumanWaiverEventRow.context == context)
        if waiver_id:
            filters.append(LaunchplaneTenantTechnicalHumanWaiverEventRow.waiver_id == waiver_id)
        if binding_sha256:
            filters.append(
                LaunchplaneTenantTechnicalHumanWaiverEventRow.binding_sha256 == binding_sha256
            )
        if pull_request_number is not None:
            filters.append(
                LaunchplaneTenantTechnicalHumanWaiverEventRow.pull_request_number
                == pull_request_number
            )
        if head_sha:
            filters.append(LaunchplaneTenantTechnicalHumanWaiverEventRow.head_sha == head_sha)
        if classification_digest:
            filters.append(
                LaunchplaneTenantTechnicalHumanWaiverEventRow.classification_digest
                == classification_digest
            )
        if role_policy_record_id:
            filters.append(
                LaunchplaneTenantTechnicalHumanWaiverEventRow.role_policy_record_id
                == role_policy_record_id
            )
        if role_policy_digest:
            filters.append(
                LaunchplaneTenantTechnicalHumanWaiverEventRow.role_policy_digest
                == role_policy_digest
            )
        if authz_policy_record_id:
            filters.append(
                LaunchplaneTenantTechnicalHumanWaiverEventRow.authz_policy_record_id
                == authz_policy_record_id
            )
        if authz_policy_digest:
            filters.append(
                LaunchplaneTenantTechnicalHumanWaiverEventRow.authz_policy_digest
                == authz_policy_digest
            )
        if action:
            filters.append(LaunchplaneTenantTechnicalHumanWaiverEventRow.action == action)
        if author_github_id is not None:
            filters.append(
                LaunchplaneTenantTechnicalHumanWaiverEventRow.author_github_id == author_github_id
            )
        return self._list_models(
            model_type=TenantTechnicalHumanWaiverEventRecord,
            orm_model=LaunchplaneTenantTechnicalHumanWaiverEventRow,
            filters=filters,
            order_by=(
                LaunchplaneTenantTechnicalHumanWaiverEventRow.occurred_at.desc(),
                LaunchplaneTenantTechnicalHumanWaiverEventRow.event_id.desc(),
            ),
            limit=limit,
        )

    def _trusted_maintenance_policy_row(
        self,
        record: TrustedMaintenancePolicyRecord,
    ) -> LaunchplaneTrustedMaintenancePolicyRow:
        return LaunchplaneTrustedMaintenancePolicyRow(
            record_id=record.record_id,
            repository_id=record.repository_id,
            repository_owner_id=record.repository_owner_id,
            repository=record.repository,
            product=record.product,
            context=record.context,
            status=record.status,
            policy_revision=record.policy_revision,
            effective_at=record.effective_at,
            source=record.source,
            supersedes_record_id=record.supersedes_record_id,
            policy_digest=record.policy_digest,
            payload=self._payload_dict(record),
        )

    def _sync_trusted_maintenance_policy_row(
        self,
        row: LaunchplaneTrustedMaintenancePolicyRow,
        record: TrustedMaintenancePolicyRecord,
    ) -> None:
        row.repository_id = record.repository_id
        row.repository_owner_id = record.repository_owner_id
        row.repository = record.repository
        row.product = record.product
        row.context = record.context
        row.status = record.status
        row.policy_revision = record.policy_revision
        row.effective_at = record.effective_at
        row.source = record.source
        row.supersedes_record_id = record.supersedes_record_id
        row.policy_digest = record.policy_digest
        row.payload = self._payload_dict(record)

    def _trusted_maintenance_evidence_row(
        self,
        record: TrustedMaintenanceEvidenceRecord,
    ) -> LaunchplaneTrustedMaintenanceEvidenceRow:
        binding = record.binding
        return LaunchplaneTrustedMaintenanceEvidenceRow(
            evidence_id=record.evidence_id,
            repository_id=binding.repository_id,
            repository_owner_id=binding.repository_owner_id,
            repository=binding.repository,
            product=binding.product,
            context=binding.context,
            binding_sha256=binding.binding_sha256,
            pull_request_number=binding.pull_request_number,
            head_sha=binding.head_sha,
            classification_record_id=binding.classification_record_id,
            classification_revision=binding.classification_revision,
            classification_digest=binding.classification_digest,
            policy_record_id=binding.policy_record_id,
            policy_revision=binding.policy_revision,
            policy_digest=binding.policy_digest,
            matched_actor_rule_id=binding.matched_actor_rule_id,
            pr_author_github_id=binding.pr_author_github_id,
            pr_author_type=binding.pr_author_type,
            pr_author_login=binding.pr_author_login,
            sender_github_id=binding.sender_github_id,
            sender_type=binding.sender_type,
            sender_login=binding.sender_login,
            head_repository_id=binding.head_repository_id,
            head_repository_owner_id=binding.head_repository_owner_id,
            head_repository=binding.head_repository,
            event_name=binding.event_name,
            event_action=binding.event_action,
            source=binding.source,
            delivery_id=binding.delivery_id,
            occurred_at=record.occurred_at,
            expires_at=record.expires_at,
            evidence_digest=record.evidence_digest,
            payload=self._payload_dict(record),
        )

    def _lock_trusted_maintenance_evidence_identity(
        self,
        session: Any,
        *,
        evidence_id: str,
    ) -> None:
        if self.database_url.startswith("sqlite"):
            return
        lock_parts = (
            "launchplane",
            "trusted-maintenance-evidence",
            evidence_id,
        )
        session.execute(
            text("select pg_advisory_xact_lock(hashtextextended(:lock_name, 0))"),
            {"lock_name": "".join(f"{len(lock_part)}:{lock_part}" for lock_part in lock_parts)},
        )

    def _locked_trusted_maintenance_evidence_identity_rows(
        self,
        *,
        session: Any,
        evidence_id: str,
    ) -> tuple[LaunchplaneTrustedMaintenanceEvidenceRow, ...]:
        statement = (
            select(LaunchplaneTrustedMaintenanceEvidenceRow)
            .where(
                LaunchplaneTrustedMaintenanceEvidenceRow.evidence_id == evidence_id,
            )
            .order_by(
                LaunchplaneTrustedMaintenanceEvidenceRow.occurred_at.asc(),
                LaunchplaneTrustedMaintenanceEvidenceRow.evidence_id.asc(),
            )
        )
        if not self.database_url.startswith("sqlite"):
            statement = statement.with_for_update()
        return tuple(session.scalars(statement).all())

    def _lock_trusted_maintenance_policy_write(
        self,
        session: Any,
        *,
        repository_id: str,
        product: str,
        context_name: str,
    ) -> None:
        if self.database_url.startswith("sqlite"):
            return
        lock_parts = (
            "launchplane",
            "trusted-maintenance-policy",
            repository_id,
            product,
            context_name,
        )
        session.execute(
            text("select pg_advisory_xact_lock(hashtextextended(:lock_name, 0))"),
            {"lock_name": "".join(f"{len(lock_part)}:{lock_part}" for lock_part in lock_parts)},
        )

    def _trusted_maintenance_policy_stream_rows(
        self,
        *,
        session: Any,
        repository_id: str,
        product: str,
        context_name: str,
        for_update: bool,
    ) -> tuple[LaunchplaneTrustedMaintenancePolicyRow, ...]:
        statement = (
            select(LaunchplaneTrustedMaintenancePolicyRow)
            .where(
                LaunchplaneTrustedMaintenancePolicyRow.repository_id == repository_id,
                LaunchplaneTrustedMaintenancePolicyRow.product == product,
                LaunchplaneTrustedMaintenancePolicyRow.context == context_name,
            )
            .order_by(LaunchplaneTrustedMaintenancePolicyRow.policy_revision.asc())
        )
        if for_update and not self.database_url.startswith("sqlite"):
            statement = statement.with_for_update()
        return tuple(session.scalars(statement).all())

    def write_trusted_maintenance_policy_record(
        self,
        record: TrustedMaintenancePolicyRecord,
    ) -> Literal["written", "replayed"]:
        insert_error: IntegrityError | None = None
        with self._session_factory() as session:
            self._begin_serialized_write(session)
            self._lock_trusted_maintenance_policy_write(
                session,
                repository_id=record.repository_id,
                product=record.product,
                context_name=record.context,
            )
            rows = self._trusted_maintenance_policy_stream_rows(
                session=session,
                repository_id=record.repository_id,
                product=record.product,
                context_name=record.context,
                for_update=True,
            )
            records = tuple(
                self._read_payload(
                    model_type=TrustedMaintenancePolicyRecord,
                    payload=row.payload,
                )
                for row in rows
            )
            plan = plan_trusted_maintenance_policy_append(records=records, record=record)
            if plan.status == "replayed":
                session.rollback()
                return "replayed"
            if plan.superseded_current_record is not None:
                current_row = next(
                    row for row in rows if row.record_id == plan.superseded_current_record.record_id
                )
                self._sync_trusted_maintenance_policy_row(
                    current_row,
                    plan.superseded_current_record,
                )
                session.flush()
            session.add(self._trusted_maintenance_policy_row(record))
            try:
                session.flush()
                session.commit()
                return "written"
            except IntegrityError as error:
                session.rollback()
                insert_error = error

        with self._session_factory() as session:
            self._begin_serialized_write(session)
            self._lock_trusted_maintenance_policy_write(
                session,
                repository_id=record.repository_id,
                product=record.product,
                context_name=record.context,
            )
            rows = self._trusted_maintenance_policy_stream_rows(
                session=session,
                repository_id=record.repository_id,
                product=record.product,
                context_name=record.context,
                for_update=True,
            )
            current_records = tuple(
                self._read_payload(
                    model_type=TrustedMaintenancePolicyRecord,
                    payload=row.payload,
                )
                for row in rows
            )
            replay_plan = plan_trusted_maintenance_policy_append(
                records=current_records,
                record=record,
            )
            session.rollback()
            if replay_plan.status == "replayed":
                return "replayed"
        assert insert_error is not None
        raise insert_error

    @overload
    def compare_and_write_trusted_maintenance_policy_record(
        self,
        record: TrustedMaintenancePolicyRecord,
        *,
        expected_current_record_id: str,
        expected_current_policy_digest: str,
    ) -> Literal["written", "replayed"]: ...

    @overload
    def compare_and_write_trusted_maintenance_policy_record(
        self,
        *,
        record: TrustedMaintenancePolicyRecord,
        expected_current_record_id: str,
        expected_current_policy_digest: str,
        mutation: DbOnlyMutationRequest,
    ) -> TrustedMaintenancePolicyCompareWriteResult: ...

    def compare_and_write_trusted_maintenance_policy_record(
        self,
        record: TrustedMaintenancePolicyRecord,
        *,
        expected_current_record_id: str,
        expected_current_policy_digest: str,
        mutation: DbOnlyMutationRequest | None = None,
    ) -> Literal["written", "replayed"] | TrustedMaintenancePolicyCompareWriteResult:
        if mutation is None:
            return self._compare_and_write_trusted_maintenance_policy_without_idempotency(
                record=record,
                expected_current_record_id=expected_current_record_id,
                expected_current_policy_digest=expected_current_policy_digest,
            )
        if not 100 <= mutation.response_status_code <= 599:
            raise ValueError("DB-only mutation response status must be between 100 and 599.")
        if not mutation.response_trace_id.strip():
            raise ValueError("DB-only mutation response trace id is required.")
        normalized_expected_record_id = expected_current_record_id.strip()
        normalized_expected_digest = expected_current_policy_digest.strip().lower()

        reservation_insert_error: IntegrityError | None = None
        with self._session_factory() as session:
            self._begin_serialized_write(session)
            observed_at = self._database_mutation_timestamp(session)
            stored_reservation = build_launchplane_mutation_reservation(
                scope=mutation.scope,
                route_path=mutation.route_path,
                idempotency_key=mutation.idempotency_key,
                request_fingerprint=mutation.request_fingerprint,
                lease_owner=mutation.lease_owner,
                lease_expires_at=self._mutation_lease_expiry(
                    observed_at=observed_at,
                    lease_seconds=mutation.lease_seconds,
                ),
                reserved_at=observed_at,
            )
            reservation_row = self._idempotency_row(stored_reservation)
            session.add(reservation_row)
            try:
                session.flush()
            except IntegrityError as error:
                session.rollback()
                reservation_insert_error = error
            if reservation_insert_error is None:
                return self._compare_and_write_trusted_maintenance_policy_locked(
                    session=session,
                    record=record,
                    expected_current_record_id=normalized_expected_record_id,
                    expected_current_policy_digest=normalized_expected_digest,
                    reservation_row=reservation_row,
                    mutation_reservation=stored_reservation,
                    mutation=mutation,
                )

        with self._session_factory() as session:
            self._begin_serialized_write(session)
            reservation_row = session.scalar(
                self._idempotency_statement(
                    scope=mutation.scope,
                    route_path=mutation.route_path,
                    idempotency_key=mutation.idempotency_key,
                    for_update=True,
                )
            )
            if reservation_row is None:
                assert reservation_insert_error is not None
                raise reservation_insert_error
            current_reservation = self._read_payload(
                model_type=LaunchplaneIdempotencyRecord,
                payload=reservation_row.payload,
            )
            if current_reservation.request_fingerprint != mutation.request_fingerprint:
                return TrustedMaintenancePolicyCompareWriteResult(
                    status="idempotency_conflict",
                    idempotency_record=current_reservation,
                )
            if current_reservation.state == "completed":
                return TrustedMaintenancePolicyCompareWriteResult(
                    status="replayed",
                    idempotency_record=current_reservation,
                )
            if current_reservation.state == "reconcile_required":
                return TrustedMaintenancePolicyCompareWriteResult(
                    status="reconciliation_required",
                    idempotency_record=current_reservation,
                )
            observed_at = self._database_mutation_timestamp(session)
            if parse_launchplane_mutation_timestamp(
                current_reservation.lease_expires_at,
                field_name="lease_expires_at",
            ) > parse_launchplane_mutation_timestamp(
                observed_at,
                field_name="observed_at",
            ):
                return TrustedMaintenancePolicyCompareWriteResult(
                    status="reservation_in_progress",
                    idempotency_record=current_reservation,
                )
            if current_reservation.reconciliation_key:
                reconcile_record = self._updated_idempotency_record(
                    current_reservation,
                    state="reconcile_required",
                    updated_at=observed_at,
                )
                self._sync_idempotency_row(reservation_row, reconcile_record)
                session.commit()
                return TrustedMaintenancePolicyCompareWriteResult(
                    status="reconciliation_required",
                    idempotency_record=reconcile_record,
                )
            reclaimed_reservation = self._updated_idempotency_record(
                current_reservation,
                lease_owner=mutation.lease_owner,
                lease_expires_at=self._mutation_lease_expiry(
                    observed_at=observed_at,
                    lease_seconds=mutation.lease_seconds,
                ),
                attempt=current_reservation.attempt + 1,
                updated_at=observed_at,
                response_status_code=None,
                response_trace_id="",
                recorded_at="",
                response_payload={},
            )
            self._sync_idempotency_row(reservation_row, reclaimed_reservation)
            return self._compare_and_write_trusted_maintenance_policy_locked(
                session=session,
                record=record,
                expected_current_record_id=normalized_expected_record_id,
                expected_current_policy_digest=normalized_expected_digest,
                reservation_row=reservation_row,
                mutation_reservation=reclaimed_reservation,
                mutation=mutation,
            )

    def _compare_and_write_trusted_maintenance_policy_without_idempotency(
        self,
        *,
        record: TrustedMaintenancePolicyRecord,
        expected_current_record_id: str,
        expected_current_policy_digest: str,
    ) -> Literal["written", "replayed"]:
        with self._session_factory() as session:
            self._begin_serialized_write(session)
            self._lock_trusted_maintenance_policy_write(
                session,
                repository_id=record.repository_id,
                product=record.product,
                context_name=record.context,
            )
            rows = self._trusted_maintenance_policy_stream_rows(
                session=session,
                repository_id=record.repository_id,
                product=record.product,
                context_name=record.context,
                for_update=True,
            )
            records = tuple(
                self._read_payload(
                    model_type=TrustedMaintenancePolicyRecord,
                    payload=row.payload,
                )
                for row in rows
            )
            plan = plan_trusted_maintenance_policy_apply(
                records=records,
                record=record,
                expected_current_record_id=expected_current_record_id,
                expected_current_policy_digest=expected_current_policy_digest,
            )
            if plan.status == "replayed":
                session.rollback()
                return "replayed"
            if plan.superseded_current_record is not None:
                current_row = next(
                    row for row in rows if row.record_id == plan.superseded_current_record.record_id
                )
                self._sync_trusted_maintenance_policy_row(
                    current_row,
                    plan.superseded_current_record,
                )
                session.flush()
            session.add(self._trusted_maintenance_policy_row(record))
            session.flush()
            session.commit()
            return "written"

    def _compare_and_write_trusted_maintenance_policy_locked(
        self,
        *,
        session: Any,
        record: TrustedMaintenancePolicyRecord,
        expected_current_record_id: str,
        expected_current_policy_digest: str,
        reservation_row: LaunchplaneIdempotencyRow,
        mutation_reservation: LaunchplaneIdempotencyRecord,
        mutation: DbOnlyMutationRequest,
    ) -> TrustedMaintenancePolicyCompareWriteResult:
        self._lock_trusted_maintenance_policy_write(
            session,
            repository_id=record.repository_id,
            product=record.product,
            context_name=record.context,
        )
        rows = self._trusted_maintenance_policy_stream_rows(
            session=session,
            repository_id=record.repository_id,
            product=record.product,
            context_name=record.context,
            for_update=True,
        )
        records = tuple(
            self._read_payload(
                model_type=TrustedMaintenancePolicyRecord,
                payload=row.payload,
            )
            for row in rows
        )
        try:
            plan = plan_trusted_maintenance_policy_apply(
                records=records,
                record=record,
                expected_current_record_id=expected_current_record_id,
                expected_current_policy_digest=expected_current_policy_digest,
            )
        except (
            TrustedMaintenancePolicyConflictError,
            TrustedMaintenancePolicySequenceError,
            ValueError,
        ):
            session.delete(reservation_row)
            session.commit()
            raise

        exact_replay = plan.status == "replayed"
        if not exact_replay:
            if plan.superseded_current_record is not None:
                current_row = next(
                    row for row in rows if row.record_id == plan.superseded_current_record.record_id
                )
                self._sync_trusted_maintenance_policy_row(
                    current_row,
                    plan.superseded_current_record,
                )
                session.flush()
            session.add(self._trusted_maintenance_policy_row(record))
            session.flush()
        completed_at = self._database_mutation_timestamp(session)
        completion = complete_launchplane_mutation_reservation(
            mutation_reservation,
            response_status_code=mutation.response_status_code,
            response_trace_id=mutation.response_trace_id,
            completed_at=completed_at,
            response_payload=(
                mutation.replay_response_payload
                if exact_replay and mutation.replay_response_payload is not None
                else mutation.response_payload
            ),
        )
        self._sync_idempotency_row(reservation_row, completion)
        session.flush()
        session.commit()
        return TrustedMaintenancePolicyCompareWriteResult(
            status="exact_replay" if exact_replay else "written",
            idempotency_record=completion,
        )

    def read_trusted_maintenance_policy_record(
        self,
        record_id: str,
    ) -> TrustedMaintenancePolicyRecord:
        return self._read_model(
            model_type=TrustedMaintenancePolicyRecord,
            orm_model=LaunchplaneTrustedMaintenancePolicyRow,
            filters=(LaunchplaneTrustedMaintenancePolicyRow.record_id == record_id,),
        )

    def list_trusted_maintenance_policy_records(
        self,
        *,
        repository_id: str = "",
        repository_owner_id: str = "",
        repository: str = "",
        product: str = "",
        context: str = "",
        status: str = "",
        limit: int | None = None,
    ) -> tuple[TrustedMaintenancePolicyRecord, ...]:
        filters: list[object] = []
        normalized_repository = repository.strip().lower()
        if repository_id:
            filters.append(LaunchplaneTrustedMaintenancePolicyRow.repository_id == repository_id)
        if repository_owner_id:
            filters.append(
                LaunchplaneTrustedMaintenancePolicyRow.repository_owner_id == repository_owner_id
            )
        if normalized_repository:
            filters.append(
                LaunchplaneTrustedMaintenancePolicyRow.repository == normalized_repository
            )
        if product:
            filters.append(LaunchplaneTrustedMaintenancePolicyRow.product == product)
        if context:
            filters.append(LaunchplaneTrustedMaintenancePolicyRow.context == context)
        if status:
            filters.append(LaunchplaneTrustedMaintenancePolicyRow.status == status)
        return self._list_models(
            model_type=TrustedMaintenancePolicyRecord,
            orm_model=LaunchplaneTrustedMaintenancePolicyRow,
            filters=filters,
            order_by=(
                LaunchplaneTrustedMaintenancePolicyRow.policy_revision.desc(),
                LaunchplaneTrustedMaintenancePolicyRow.repository_id.desc(),
                LaunchplaneTrustedMaintenancePolicyRow.product.desc(),
                LaunchplaneTrustedMaintenancePolicyRow.context.desc(),
                LaunchplaneTrustedMaintenancePolicyRow.record_id.desc(),
            ),
            limit=limit,
        )

    def capture_trusted_maintenance_evidence_transactionally(
        self,
        *,
        candidate: TenantMergeCandidate,
        expected_authority: TrustedMaintenanceExpectedAuthority,
        event_facts: TrustedMaintenanceGitHubEventFacts,
    ) -> Literal["written", "replayed"]:
        insert_error: IntegrityError | None = None
        with self._session_factory() as session:
            self._begin_serialized_write(session)
            self._lock_tenant_repository_classification_write(
                session,
                repository_id=candidate.repository_id,
            )
            self._lock_trusted_maintenance_policy_write(
                session,
                repository_id=candidate.repository_id,
                product=candidate.product,
                context_name=candidate.context,
            )
            observed_at = self._database_mutation_timestamp(session)
            classification_rows = self._locked_current_classification_rows(
                session=session,
                repository_id=candidate.repository_id,
            )
            policy_rows = self._trusted_maintenance_policy_stream_rows(
                session=session,
                repository_id=candidate.repository_id,
                product=candidate.product,
                context_name=candidate.context,
                for_update=True,
            )
            authority_snapshot = _TrustedMaintenanceAuthoritySnapshot(
                classifications=tuple(
                    self._read_payload(
                        model_type=TenantRepositoryClassificationRecord,
                        payload=row.payload,
                    )
                    for row in classification_rows
                ),
                policies=tuple(
                    self._read_payload(
                        model_type=TrustedMaintenancePolicyRecord,
                        payload=row.payload,
                    )
                    for row in policy_rows
                ),
            )
            try:
                current = trusted_maintenance_current_authority(
                    store=authority_snapshot,
                    candidate=candidate,
                    expected_authority=expected_authority,
                    evaluated_at=observed_at,
                )
                provisional = capture_trusted_maintenance_evidence(
                    candidate=candidate,
                    classification=current.classification,
                    policy_record=current.policy_record,
                    event_facts=event_facts,
                    occurred_at=observed_at,
                    recorded_at=observed_at,
                )
            except (
                TrustedMaintenanceAuthorityError,
                TrustedMaintenanceRuleMatchError,
                ValueError,
            ):
                session.rollback()
                raise

            self._lock_trusted_maintenance_evidence_identity(
                session,
                evidence_id=provisional.record.evidence_id,
            )
            evidence_rows = self._locked_trusted_maintenance_evidence_identity_rows(
                session=session,
                evidence_id=provisional.record.evidence_id,
            )
            evidence_records = tuple(
                self._read_payload(
                    model_type=TrustedMaintenanceEvidenceRecord,
                    payload=row.payload,
                )
                for row in evidence_rows
            )
            append_plan = plan_trusted_maintenance_evidence_append(
                records=evidence_records,
                record=provisional.record,
            )
            if append_plan.status != "replayed":
                session.add(self._trusted_maintenance_evidence_row(provisional.record))
                try:
                    session.flush()
                    session.commit()
                    return append_plan.status
                except IntegrityError as error:
                    session.rollback()
                    insert_error = error
            else:
                session.commit()
                return append_plan.status

        with self._session_factory() as session:
            self._begin_serialized_write(session)
            self._lock_trusted_maintenance_evidence_identity(
                session,
                evidence_id=provisional.record.evidence_id,
            )
            evidence_rows = self._locked_trusted_maintenance_evidence_identity_rows(
                session=session,
                evidence_id=provisional.record.evidence_id,
            )
            evidence_records = tuple(
                self._read_payload(
                    model_type=TrustedMaintenanceEvidenceRecord,
                    payload=row.payload,
                )
                for row in evidence_rows
            )
            replay_plan = plan_trusted_maintenance_evidence_append(
                records=evidence_records,
                record=provisional.record,
            )
            session.rollback()
            if replay_plan.status == "replayed":
                return "replayed"
        assert insert_error is not None
        raise insert_error

    def write_trusted_maintenance_evidence_record(
        self,
        record: TrustedMaintenanceEvidenceRecord,
    ) -> Literal["written", "replayed"]:
        insert_error: IntegrityError | None = None
        with self._session_factory() as session:
            self._begin_serialized_write(session)
            statement = select(LaunchplaneTrustedMaintenanceEvidenceRow).where(
                LaunchplaneTrustedMaintenanceEvidenceRow.evidence_id == record.evidence_id
            )
            if not self.database_url.startswith("sqlite"):
                statement = statement.with_for_update()
            existing_row = session.scalar(statement)
            if existing_row is not None:
                existing_record = self._read_payload(
                    model_type=TrustedMaintenanceEvidenceRecord,
                    payload=existing_row.payload,
                )
                plan = plan_trusted_maintenance_evidence_append(
                    records=(existing_record,),
                    record=record,
                )
                session.rollback()
                return plan.status
            session.add(self._trusted_maintenance_evidence_row(record))
            try:
                session.flush()
                session.commit()
                return "written"
            except IntegrityError as error:
                session.rollback()
                insert_error = error

        try:
            existing_record = self.read_trusted_maintenance_evidence_record(record.evidence_id)
        except FileNotFoundError as read_error:
            assert insert_error is not None
            raise insert_error from read_error
        replay_plan = plan_trusted_maintenance_evidence_append(
            records=(existing_record,),
            record=record,
        )
        if replay_plan.status == "replayed":
            return "replayed"
        assert insert_error is not None
        raise insert_error

    def read_trusted_maintenance_evidence_record(
        self,
        evidence_id: str,
    ) -> TrustedMaintenanceEvidenceRecord:
        return self._read_model(
            model_type=TrustedMaintenanceEvidenceRecord,
            orm_model=LaunchplaneTrustedMaintenanceEvidenceRow,
            filters=(LaunchplaneTrustedMaintenanceEvidenceRow.evidence_id == evidence_id,),
        )

    def list_trusted_maintenance_evidence_records(
        self,
        *,
        repository_id: str = "",
        repository_owner_id: str = "",
        repository: str = "",
        product: str = "",
        context: str = "",
        evidence_id: str = "",
        binding_sha256: str = "",
        pull_request_number: int | None = None,
        head_sha: str = "",
        classification_digest: str = "",
        policy_record_id: str = "",
        policy_digest: str = "",
        matched_actor_rule_id: str = "",
        pr_author_github_id: int | None = None,
        sender_github_id: int | None = None,
        event_name: str = "",
        event_action: str = "",
        delivery_id: str = "",
        limit: int | None = None,
    ) -> tuple[TrustedMaintenanceEvidenceRecord, ...]:
        filters: list[object] = []
        normalized_repository = repository.strip().lower()
        if repository_id:
            filters.append(LaunchplaneTrustedMaintenanceEvidenceRow.repository_id == repository_id)
        if repository_owner_id:
            filters.append(
                LaunchplaneTrustedMaintenanceEvidenceRow.repository_owner_id == repository_owner_id
            )
        if normalized_repository:
            filters.append(
                LaunchplaneTrustedMaintenanceEvidenceRow.repository == normalized_repository
            )
        if product:
            filters.append(LaunchplaneTrustedMaintenanceEvidenceRow.product == product)
        if context:
            filters.append(LaunchplaneTrustedMaintenanceEvidenceRow.context == context)
        if evidence_id:
            filters.append(LaunchplaneTrustedMaintenanceEvidenceRow.evidence_id == evidence_id)
        if binding_sha256:
            filters.append(
                LaunchplaneTrustedMaintenanceEvidenceRow.binding_sha256 == binding_sha256
            )
        if pull_request_number is not None:
            filters.append(
                LaunchplaneTrustedMaintenanceEvidenceRow.pull_request_number == pull_request_number
            )
        if head_sha:
            filters.append(LaunchplaneTrustedMaintenanceEvidenceRow.head_sha == head_sha)
        if classification_digest:
            filters.append(
                LaunchplaneTrustedMaintenanceEvidenceRow.classification_digest
                == classification_digest
            )
        if policy_record_id:
            filters.append(
                LaunchplaneTrustedMaintenanceEvidenceRow.policy_record_id == policy_record_id
            )
        if policy_digest:
            filters.append(LaunchplaneTrustedMaintenanceEvidenceRow.policy_digest == policy_digest)
        if matched_actor_rule_id:
            filters.append(
                LaunchplaneTrustedMaintenanceEvidenceRow.matched_actor_rule_id
                == matched_actor_rule_id
            )
        if pr_author_github_id is not None:
            filters.append(
                LaunchplaneTrustedMaintenanceEvidenceRow.pr_author_github_id == pr_author_github_id
            )
        if sender_github_id is not None:
            filters.append(
                LaunchplaneTrustedMaintenanceEvidenceRow.sender_github_id == sender_github_id
            )
        if event_name:
            filters.append(LaunchplaneTrustedMaintenanceEvidenceRow.event_name == event_name)
        if event_action:
            filters.append(LaunchplaneTrustedMaintenanceEvidenceRow.event_action == event_action)
        if delivery_id:
            filters.append(LaunchplaneTrustedMaintenanceEvidenceRow.delivery_id == delivery_id)
        return self._list_models(
            model_type=TrustedMaintenanceEvidenceRecord,
            orm_model=LaunchplaneTrustedMaintenanceEvidenceRow,
            filters=filters,
            order_by=(
                LaunchplaneTrustedMaintenanceEvidenceRow.occurred_at.desc(),
                LaunchplaneTrustedMaintenanceEvidenceRow.evidence_id.desc(),
            ),
            limit=limit,
        )

    def _tenant_repository_classification_row(
        self, record: TenantRepositoryClassificationRecord
    ) -> LaunchplaneTenantRepositoryClassificationRow:
        return LaunchplaneTenantRepositoryClassificationRow(
            record_id=record.record_id,
            repository_id=record.repository_id,
            repository_owner_id=record.repository_owner_id,
            repository=record.repository,
            product=record.product,
            context=record.context,
            classification_kind=record.classification_kind,
            classification_revision=record.classification_revision,
            classified_at=record.classified_at,
            classification_digest=record.classification_digest,
            payload=self._payload_dict(record),
        )

    def _lock_tenant_repository_classification_write(
        self,
        session: Any,
        *,
        repository_id: str,
    ) -> None:
        if self.database_url.startswith("sqlite"):
            return
        session.execute(
            text("select pg_advisory_xact_lock(hashtextextended(:lock_name, 0))"),
            {"lock_name": f"launchplane:tenant-repository-classification:{repository_id}"},
        )

    def compare_and_write_tenant_repository_classification_record(
        self,
        *,
        record: TenantRepositoryClassificationRecord,
        expected_current_record_id: str,
        mutation: DbOnlyMutationRequest,
    ) -> TenantRepositoryClassificationCompareWriteResult:
        if not 100 <= mutation.response_status_code <= 599:
            raise ValueError("DB-only mutation response status must be between 100 and 599.")
        if not mutation.response_trace_id.strip():
            raise ValueError("DB-only mutation response trace id is required.")
        normalized_expected = expected_current_record_id.strip()

        reservation_insert_error: IntegrityError | None = None
        with self._session_factory() as session:
            self._begin_serialized_write(session)
            observed_at = self._database_mutation_timestamp(session)
            stored_reservation = build_launchplane_mutation_reservation(
                scope=mutation.scope,
                route_path=mutation.route_path,
                idempotency_key=mutation.idempotency_key,
                request_fingerprint=mutation.request_fingerprint,
                lease_owner=mutation.lease_owner,
                lease_expires_at=self._mutation_lease_expiry(
                    observed_at=observed_at,
                    lease_seconds=mutation.lease_seconds,
                ),
                reserved_at=observed_at,
            )
            reservation_row = self._idempotency_row(stored_reservation)
            session.add(reservation_row)
            try:
                session.flush()
            except IntegrityError as error:
                session.rollback()
                reservation_insert_error = error
            if reservation_insert_error is None:
                return self._compare_and_write_tenant_repository_classification_locked(
                    session=session,
                    record=record,
                    expected_current_record_id=normalized_expected,
                    reservation_row=reservation_row,
                    mutation_reservation=stored_reservation,
                    mutation=mutation,
                )

        with self._session_factory() as session:
            self._begin_serialized_write(session)
            reservation_row = session.scalar(
                self._idempotency_statement(
                    scope=mutation.scope,
                    route_path=mutation.route_path,
                    idempotency_key=mutation.idempotency_key,
                    for_update=True,
                )
            )
            if reservation_row is None:
                assert reservation_insert_error is not None
                raise reservation_insert_error
            current_reservation = self._read_payload(
                model_type=LaunchplaneIdempotencyRecord,
                payload=reservation_row.payload,
            )
            if current_reservation.request_fingerprint != mutation.request_fingerprint:
                return TenantRepositoryClassificationCompareWriteResult(
                    status="idempotency_conflict",
                    idempotency_record=current_reservation,
                )
            if current_reservation.state == "completed":
                return TenantRepositoryClassificationCompareWriteResult(
                    status="replayed",
                    idempotency_record=current_reservation,
                )
            if current_reservation.state == "reconcile_required":
                return TenantRepositoryClassificationCompareWriteResult(
                    status="reconciliation_required",
                    idempotency_record=current_reservation,
                )
            observed_at = self._database_mutation_timestamp(session)
            if parse_launchplane_mutation_timestamp(
                current_reservation.lease_expires_at,
                field_name="lease_expires_at",
            ) > parse_launchplane_mutation_timestamp(
                observed_at,
                field_name="observed_at",
            ):
                return TenantRepositoryClassificationCompareWriteResult(
                    status="reservation_in_progress",
                    idempotency_record=current_reservation,
                )
            if current_reservation.reconciliation_key:
                reconcile_record = self._updated_idempotency_record(
                    current_reservation,
                    state="reconcile_required",
                    updated_at=observed_at,
                )
                self._sync_idempotency_row(reservation_row, reconcile_record)
                session.commit()
                return TenantRepositoryClassificationCompareWriteResult(
                    status="reconciliation_required",
                    idempotency_record=reconcile_record,
                )
            reclaimed_reservation = self._updated_idempotency_record(
                current_reservation,
                lease_owner=mutation.lease_owner,
                lease_expires_at=self._mutation_lease_expiry(
                    observed_at=observed_at,
                    lease_seconds=mutation.lease_seconds,
                ),
                attempt=current_reservation.attempt + 1,
                updated_at=observed_at,
                response_status_code=None,
                response_trace_id="",
                recorded_at="",
                response_payload={},
            )
            self._sync_idempotency_row(reservation_row, reclaimed_reservation)
            return self._compare_and_write_tenant_repository_classification_locked(
                session=session,
                record=record,
                expected_current_record_id=normalized_expected,
                reservation_row=reservation_row,
                mutation_reservation=reclaimed_reservation,
                mutation=mutation,
            )

    def _compare_and_write_tenant_repository_classification_locked(
        self,
        *,
        session: Any,
        record: TenantRepositoryClassificationRecord,
        expected_current_record_id: str,
        reservation_row: LaunchplaneIdempotencyRow,
        mutation_reservation: LaunchplaneIdempotencyRecord,
        mutation: DbOnlyMutationRequest,
    ) -> TenantRepositoryClassificationCompareWriteResult:
        self._lock_tenant_repository_classification_write(
            session,
            repository_id=record.repository_id,
        )
        statement = select(LaunchplaneTenantRepositoryClassificationRow).where(
            LaunchplaneTenantRepositoryClassificationRow.repository_id == record.repository_id
        )
        if not self.database_url.startswith("sqlite"):
            statement = statement.with_for_update()
        existing_records = tuple(
            self._read_payload(
                model_type=TenantRepositoryClassificationRecord,
                payload=row.payload,
            )
            for row in session.scalars(statement).all()
        )
        try:
            plan = plan_tenant_repository_classification_append(
                records=existing_records,
                record=record,
            )
            if plan.status == "replayed":
                raise TenantRepositoryClassificationConflictError(
                    "Tenant repository classification record already exists; retry the "
                    "original request with the same Idempotency-Key."
                )
            if plan.current_record is None and expected_current_record_id:
                raise TenantRepositoryClassificationConflictError(
                    f"Expected current classification record ID '{expected_current_record_id}' "
                    "does not match current state: repository has no existing "
                    "classification record."
                )
            if (
                plan.current_record is not None
                and expected_current_record_id != plan.current_record.record_id
            ):
                raise TenantRepositoryClassificationConflictError(
                    f"Expected current classification record ID '{expected_current_record_id}' "
                    "does not match active current record ID "
                    f"'{plan.current_record.record_id}'."
                )
        except (
            TenantRepositoryClassificationConflictError,
            TenantRepositoryClassificationSequenceError,
        ):
            session.delete(reservation_row)
            session.commit()
            raise

        session.add(self._tenant_repository_classification_row(record))
        session.flush()
        completed_at = self._database_mutation_timestamp(session)
        completion = complete_launchplane_mutation_reservation(
            mutation_reservation,
            response_status_code=mutation.response_status_code,
            response_trace_id=mutation.response_trace_id,
            completed_at=completed_at,
            response_payload=mutation.response_payload,
        )
        self._sync_idempotency_row(reservation_row, completion)
        session.commit()
        return TenantRepositoryClassificationCompareWriteResult(
            status="written",
            idempotency_record=completion,
        )

    def write_tenant_repository_classification_record(
        self, record: TenantRepositoryClassificationRecord
    ) -> Literal["written", "replayed"]:
        insert_error: IntegrityError | None = None
        with self._session_factory() as session:
            self._begin_serialized_write(session)
            self._lock_tenant_repository_classification_write(
                session,
                repository_id=record.repository_id,
            )
            statement = select(LaunchplaneTenantRepositoryClassificationRow).where(
                LaunchplaneTenantRepositoryClassificationRow.repository_id == record.repository_id
            )
            if not self.database_url.startswith("sqlite"):
                statement = statement.with_for_update()
            existing_records = tuple(
                self._read_payload(
                    model_type=TenantRepositoryClassificationRecord,
                    payload=row.payload,
                )
                for row in session.scalars(statement).all()
            )
            plan = plan_tenant_repository_classification_append(
                records=existing_records,
                record=record,
            )
            if plan.status == "replayed":
                session.rollback()
                return "replayed"
            session.add(self._tenant_repository_classification_row(record))
            try:
                session.commit()
                return "written"
            except IntegrityError as error:
                session.rollback()
                insert_error = error

        current_records = self.list_tenant_repository_classification_records(
            repository_id=record.repository_id
        )
        replay_plan = plan_tenant_repository_classification_append(
            records=current_records,
            record=record,
        )
        if replay_plan.status == "replayed":
            return "replayed"
        assert insert_error is not None
        raise insert_error

    def read_tenant_repository_classification_record(
        self, record_id: str
    ) -> TenantRepositoryClassificationRecord:
        return self._read_model(
            model_type=TenantRepositoryClassificationRecord,
            orm_model=LaunchplaneTenantRepositoryClassificationRow,
            filters=(LaunchplaneTenantRepositoryClassificationRow.record_id == record_id,),
        )

    def list_tenant_repository_classification_records(
        self,
        *,
        repository_id: str = "",
        limit: int | None = None,
    ) -> tuple[TenantRepositoryClassificationRecord, ...]:
        filters: list[object] = []
        if repository_id:
            filters.append(
                LaunchplaneTenantRepositoryClassificationRow.repository_id == repository_id
            )
        return self._list_models(
            model_type=TenantRepositoryClassificationRecord,
            orm_model=LaunchplaneTenantRepositoryClassificationRow,
            filters=filters,
            order_by=(
                LaunchplaneTenantRepositoryClassificationRow.classification_revision.desc(),
                LaunchplaneTenantRepositoryClassificationRow.repository_id.desc(),
                LaunchplaneTenantRepositoryClassificationRow.record_id.desc(),
            ),
            limit=limit,
        )

    def latest_tenant_repository_classification_lookup(
        self, *, repository_id: str
    ) -> TenantRepositoryClassificationLookup:
        records = self.list_tenant_repository_classification_records(
            repository_id=repository_id,
        )
        if not records:
            return TenantRepositoryClassificationLookup(status="missing")
        latest_revision = records[0].classification_revision
        return TenantRepositoryClassificationLookup(
            records=tuple(
                record for record in records if record.classification_revision == latest_revision
            )
        )

    def _repository_inventory_row(
        self, record: RepositoryInventoryRecord
    ) -> LaunchplaneRepositoryInventoryRow:
        return LaunchplaneRepositoryInventoryRow(
            record_id=record.record_id,
            repository_id=record.repository_id,
            repository_owner_id=record.repository_owner_id,
            repository=record.repository,
            inventory_state=record.inventory_state,
            inventory_revision=record.inventory_revision,
            recorded_at=record.recorded_at,
            inventory_digest=record.inventory_digest,
            payload=self._payload_dict(record),
        )

    def _lock_repository_inventory_write(self, session: Any, *, repository_id: str) -> None:
        if self.database_url.startswith("sqlite"):
            return
        session.execute(
            text("select pg_advisory_xact_lock(hashtextextended(:lock_name, 0))"),
            {"lock_name": f"launchplane:repository-inventory:{repository_id}"},
        )

    def compare_and_write_repository_inventory_record(
        self,
        *,
        record: RepositoryInventoryRecord,
        expected_current_record_id: str,
        mutation: DbOnlyMutationRequest,
    ) -> RepositoryInventoryCompareWriteResult:
        if not 100 <= mutation.response_status_code <= 599:
            raise ValueError("DB-only mutation response status must be between 100 and 599.")
        if not mutation.response_trace_id.strip():
            raise ValueError("DB-only mutation response trace id is required.")
        normalized_expected = expected_current_record_id.strip()

        reservation_insert_error: IntegrityError | None = None
        with self._session_factory() as session:
            self._begin_serialized_write(session)
            observed_at = self._database_mutation_timestamp(session)
            stored_reservation = build_launchplane_mutation_reservation(
                scope=mutation.scope,
                route_path=mutation.route_path,
                idempotency_key=mutation.idempotency_key,
                request_fingerprint=mutation.request_fingerprint,
                lease_owner=mutation.lease_owner,
                lease_expires_at=self._mutation_lease_expiry(
                    observed_at=observed_at,
                    lease_seconds=mutation.lease_seconds,
                ),
                reserved_at=observed_at,
            )
            reservation_row = self._idempotency_row(stored_reservation)
            session.add(reservation_row)
            try:
                session.flush()
            except IntegrityError as error:
                session.rollback()
                reservation_insert_error = error
            if reservation_insert_error is None:
                return self._compare_and_write_repository_inventory_locked(
                    session=session,
                    record=record,
                    expected_current_record_id=normalized_expected,
                    reservation_row=reservation_row,
                    mutation_reservation=stored_reservation,
                    mutation=mutation,
                )

        with self._session_factory() as session:
            self._begin_serialized_write(session)
            reservation_row = session.scalar(
                self._idempotency_statement(
                    scope=mutation.scope,
                    route_path=mutation.route_path,
                    idempotency_key=mutation.idempotency_key,
                    for_update=True,
                )
            )
            if reservation_row is None:
                assert reservation_insert_error is not None
                raise reservation_insert_error
            current_reservation = self._read_payload(
                model_type=LaunchplaneIdempotencyRecord,
                payload=reservation_row.payload,
            )
            if current_reservation.request_fingerprint != mutation.request_fingerprint:
                return RepositoryInventoryCompareWriteResult(
                    status="idempotency_conflict",
                    idempotency_record=current_reservation,
                )
            if current_reservation.state == "completed":
                return RepositoryInventoryCompareWriteResult(
                    status="replayed",
                    idempotency_record=current_reservation,
                )
            if current_reservation.state == "reconcile_required":
                return RepositoryInventoryCompareWriteResult(
                    status="reconciliation_required",
                    idempotency_record=current_reservation,
                )
            observed_at = self._database_mutation_timestamp(session)
            if parse_launchplane_mutation_timestamp(
                current_reservation.lease_expires_at,
                field_name="lease_expires_at",
            ) > parse_launchplane_mutation_timestamp(
                observed_at,
                field_name="observed_at",
            ):
                return RepositoryInventoryCompareWriteResult(
                    status="reservation_in_progress",
                    idempotency_record=current_reservation,
                )
            if current_reservation.reconciliation_key:
                reconcile_record = self._updated_idempotency_record(
                    current_reservation,
                    state="reconcile_required",
                    updated_at=observed_at,
                )
                self._sync_idempotency_row(reservation_row, reconcile_record)
                session.commit()
                return RepositoryInventoryCompareWriteResult(
                    status="reconciliation_required",
                    idempotency_record=reconcile_record,
                )
            reclaimed_reservation = self._updated_idempotency_record(
                current_reservation,
                lease_owner=mutation.lease_owner,
                lease_expires_at=self._mutation_lease_expiry(
                    observed_at=observed_at,
                    lease_seconds=mutation.lease_seconds,
                ),
                attempt=current_reservation.attempt + 1,
                updated_at=observed_at,
                response_status_code=None,
                response_trace_id="",
                recorded_at="",
                response_payload={},
            )
            self._sync_idempotency_row(reservation_row, reclaimed_reservation)
            return self._compare_and_write_repository_inventory_locked(
                session=session,
                record=record,
                expected_current_record_id=normalized_expected,
                reservation_row=reservation_row,
                mutation_reservation=reclaimed_reservation,
                mutation=mutation,
            )

    def _compare_and_write_repository_inventory_locked(
        self,
        *,
        session: Any,
        record: RepositoryInventoryRecord,
        expected_current_record_id: str,
        reservation_row: LaunchplaneIdempotencyRow,
        mutation_reservation: LaunchplaneIdempotencyRecord,
        mutation: DbOnlyMutationRequest,
    ) -> RepositoryInventoryCompareWriteResult:
        self._lock_repository_inventory_write(session, repository_id=record.repository_id)
        statement = select(LaunchplaneRepositoryInventoryRow).where(
            LaunchplaneRepositoryInventoryRow.repository_id == record.repository_id
        )
        if not self.database_url.startswith("sqlite"):
            statement = statement.with_for_update()
        existing_records = tuple(
            self._read_payload(model_type=RepositoryInventoryRecord, payload=row.payload)
            for row in session.scalars(statement).all()
        )
        try:
            plan = plan_repository_inventory_append(records=existing_records, record=record)
            if plan.status == "replayed":
                raise RepositoryInventoryConflictError(
                    "Repository inventory record already exists; retry the original request "
                    "with the same Idempotency-Key."
                )
            if plan.current_record is None and expected_current_record_id:
                raise RepositoryInventoryConflictError(
                    f"Expected current repository inventory record ID "
                    f"'{expected_current_record_id}' does not match current state: repository "
                    "has no existing inventory record."
                )
            if (
                plan.current_record is not None
                and expected_current_record_id != plan.current_record.record_id
            ):
                raise RepositoryInventoryConflictError(
                    f"Expected current repository inventory record ID "
                    f"'{expected_current_record_id}' does not match active current record ID "
                    f"'{plan.current_record.record_id}'."
                )
        except (RepositoryInventoryConflictError, RepositoryInventorySequenceError):
            session.delete(reservation_row)
            session.commit()
            raise

        session.add(self._repository_inventory_row(record))
        session.flush()
        completed_at = self._database_mutation_timestamp(session)
        completion = complete_launchplane_mutation_reservation(
            mutation_reservation,
            response_status_code=mutation.response_status_code,
            response_trace_id=mutation.response_trace_id,
            completed_at=completed_at,
            response_payload=mutation.response_payload,
        )
        self._sync_idempotency_row(reservation_row, completion)
        session.commit()
        return RepositoryInventoryCompareWriteResult(
            status="written",
            idempotency_record=completion,
        )

    def write_repository_inventory_record(
        self, record: RepositoryInventoryRecord
    ) -> Literal["written", "replayed"]:
        insert_error: IntegrityError | None = None
        with self._session_factory() as session:
            self._begin_serialized_write(session)
            self._lock_repository_inventory_write(session, repository_id=record.repository_id)
            statement = select(LaunchplaneRepositoryInventoryRow).where(
                LaunchplaneRepositoryInventoryRow.repository_id == record.repository_id
            )
            if not self.database_url.startswith("sqlite"):
                statement = statement.with_for_update()
            existing_records = tuple(
                self._read_payload(model_type=RepositoryInventoryRecord, payload=row.payload)
                for row in session.scalars(statement).all()
            )
            plan = plan_repository_inventory_append(records=existing_records, record=record)
            if plan.status == "replayed":
                session.rollback()
                return "replayed"
            session.add(self._repository_inventory_row(record))
            try:
                session.commit()
                return "written"
            except IntegrityError as error:
                session.rollback()
                insert_error = error
        replay_plan = plan_repository_inventory_append(
            records=self.list_repository_inventory_records(repository_id=record.repository_id),
            record=record,
        )
        if replay_plan.status == "replayed":
            return "replayed"
        assert insert_error is not None
        raise insert_error

    def list_repository_inventory_records(
        self,
        *,
        repository_id: str = "",
        limit: int | None = None,
    ) -> tuple[RepositoryInventoryRecord, ...]:
        filters: list[object] = []
        if repository_id:
            filters.append(LaunchplaneRepositoryInventoryRow.repository_id == repository_id)
        return self._list_models(
            model_type=RepositoryInventoryRecord,
            orm_model=LaunchplaneRepositoryInventoryRow,
            filters=filters,
            order_by=(
                LaunchplaneRepositoryInventoryRow.inventory_revision.desc(),
                LaunchplaneRepositoryInventoryRow.repository_id.desc(),
                LaunchplaneRepositoryInventoryRow.record_id.desc(),
            ),
            limit=limit,
        )

    def list_manager_preview_approval_event_records(
        self,
        *,
        product: str = "",
        context: str = "",
        repository: str = "",
        pr_number: int | None = None,
        preview_id: str = "",
        action: str = "",
        limit: int | None = None,
    ) -> tuple[ManagerPreviewApprovalEventRecord, ...]:
        filters: list[object] = []
        if product:
            filters.append(LaunchplaneManagerPreviewApprovalEventRow.product == product.lower())
        if context:
            filters.append(LaunchplaneManagerPreviewApprovalEventRow.context == context.lower())
        if repository:
            filters.append(
                LaunchplaneManagerPreviewApprovalEventRow.repository == repository.lower()
            )
        if pr_number is not None:
            filters.append(LaunchplaneManagerPreviewApprovalEventRow.pr_number == pr_number)
        if preview_id:
            filters.append(LaunchplaneManagerPreviewApprovalEventRow.preview_id == preview_id)
        if action:
            filters.append(LaunchplaneManagerPreviewApprovalEventRow.action == action)
        return self._list_models(
            model_type=ManagerPreviewApprovalEventRecord,
            orm_model=LaunchplaneManagerPreviewApprovalEventRow,
            filters=filters,
            order_by=(
                LaunchplaneManagerPreviewApprovalEventRow.occurred_at.desc(),
                LaunchplaneManagerPreviewApprovalEventRow.event_id.desc(),
            ),
            limit=limit,
        )

    def list_owner_acceptance_event_records(
        self,
        *,
        repository_id: str = "",
        repository: str = "",
        pull_request_number: int | None = None,
        product: str = "",
        system: str = "",
        action: str = "",
        acceptance_action: str = "",
        limit: int | None = None,
    ) -> tuple[OwnerAcceptanceEventRecord, ...]:
        filters: list[Any] = []
        if repository_id:
            filters.append(
                LaunchplaneOwnerAcceptanceEventRow.repository_id == repository_id.strip()
            )
        if repository:
            filters.append(LaunchplaneOwnerAcceptanceEventRow.repository == repository.lower())
        if pull_request_number is not None:
            filters.append(LaunchplaneOwnerAcceptanceEventRow.pr_number == pull_request_number)
        if product:
            filters.append(LaunchplaneOwnerAcceptanceEventRow.product == product.strip())
        if system:
            filters.append(LaunchplaneOwnerAcceptanceEventRow.system == system.strip())
        if action:
            filters.append(LaunchplaneOwnerAcceptanceEventRow.owner_action == action.strip())
        if acceptance_action:
            filters.append(LaunchplaneOwnerAcceptanceEventRow.action == acceptance_action.strip())
        statement = select(LaunchplaneOwnerAcceptanceEventRow)
        if filters:
            statement = statement.where(*filters)
        statement = statement.order_by(
            LaunchplaneOwnerAcceptanceEventRow.occurred_at.desc(),
            LaunchplaneOwnerAcceptanceEventRow.event_id.desc(),
        )
        if limit is not None:
            statement = statement.limit(limit)
        with self._session_factory() as session:
            rows = session.scalars(statement).all()
            return tuple(self._owner_acceptance_record_from_row(row) for row in rows)

    def write_preview_enablement_record(self, record: PreviewEnablementRecord) -> None:
        self._write_row(
            LaunchplanePreviewEnablementRow(
                record_id=record.record_id,
                context=record.context,
                anchor_repo=record.anchor_repo,
                anchor_pr_number=record.anchor_pr_number,
                pr_state=record.pr_state,
                updated_at=record.updated_at,
                payload=self._payload_dict(record),
            )
        )

    def read_preview_enablement_record(self, record_id: str) -> PreviewEnablementRecord:
        return self._read_model(
            model_type=PreviewEnablementRecord,
            orm_model=LaunchplanePreviewEnablementRow,
            filters=(LaunchplanePreviewEnablementRow.record_id == record_id,),
        )

    def list_preview_enablement_records(
        self,
        *,
        context_name: str = "",
        anchor_repo: str = "",
        pr_state: str = "",
        limit: int | None = None,
    ) -> tuple[PreviewEnablementRecord, ...]:
        filters: list[object] = []
        if context_name:
            filters.append(LaunchplanePreviewEnablementRow.context == context_name)
        if anchor_repo:
            filters.append(LaunchplanePreviewEnablementRow.anchor_repo == anchor_repo)
        if pr_state:
            filters.append(LaunchplanePreviewEnablementRow.pr_state == pr_state)
        return self._list_models(
            model_type=PreviewEnablementRecord,
            orm_model=LaunchplanePreviewEnablementRow,
            filters=filters,
            order_by=(
                LaunchplanePreviewEnablementRow.updated_at.desc(),
                LaunchplanePreviewEnablementRow.record_id.desc(),
            ),
            limit=limit,
        )

    def write_preview_inventory_scan_record(self, record: PreviewInventoryScanRecord) -> None:
        self._write_row(
            LaunchplanePreviewInventoryScanRow(
                scan_id=record.scan_id,
                context=record.context,
                scanned_at=record.scanned_at,
                source=record.source,
                status=record.status,
                preview_count=record.preview_count,
                payload=self._payload_dict(record),
            )
        )

    def list_preview_inventory_scan_records(
        self,
        *,
        context_name: str = "",
        limit: int | None = None,
    ) -> tuple[PreviewInventoryScanRecord, ...]:
        filters: list[object] = []
        if context_name:
            filters.append(LaunchplanePreviewInventoryScanRow.context == context_name)
        return self._list_models(
            model_type=PreviewInventoryScanRecord,
            orm_model=LaunchplanePreviewInventoryScanRow,
            filters=filters,
            order_by=(
                LaunchplanePreviewInventoryScanRow.scanned_at.desc(),
                LaunchplanePreviewInventoryScanRow.scan_id.desc(),
            ),
            limit=limit,
        )

    def write_preview_desired_state_record(self, record: PreviewDesiredStateRecord) -> None:
        self._write_row(
            LaunchplanePreviewDesiredStateRow(
                desired_state_id=record.desired_state_id,
                product=record.product,
                context=record.context,
                discovered_at=record.discovered_at,
                repository=record.repository,
                label=record.label,
                status=record.status,
                desired_count=record.desired_count,
                payload=self._payload_dict(record),
            )
        )

    def list_preview_desired_state_records(
        self,
        *,
        context_name: str = "",
        limit: int | None = None,
    ) -> tuple[PreviewDesiredStateRecord, ...]:
        filters: list[object] = []
        if context_name:
            filters.append(LaunchplanePreviewDesiredStateRow.context == context_name)
        return self._list_models(
            model_type=PreviewDesiredStateRecord,
            orm_model=LaunchplanePreviewDesiredStateRow,
            filters=filters,
            order_by=(
                LaunchplanePreviewDesiredStateRow.discovered_at.desc(),
                LaunchplanePreviewDesiredStateRow.desired_state_id.desc(),
            ),
            limit=limit,
        )

    def write_every_code_work_request_record(self, record: EveryCodeWorkRequestRecord) -> None:
        self._write_row(
            LaunchplaneEveryCodeWorkRequestRow(
                request_id=record.request_id,
                source=record.source,
                state=record.state,
                repository=record.repository,
                issue_number=record.issue_number,
                trigger_label=record.trigger_label,
                updated_at=record.updated_at,
                claimed_by_host=record.claimed_by_host,
                lease_expires_at=record.lease_expires_at,
                fencing_token=record.fencing_token,
                attempt=record.attempt,
                payload=self._payload_dict(record),
            )
        )

    def create_every_code_work_request_record_if_absent(
        self, record: EveryCodeWorkRequestRecord
    ) -> tuple[EveryCodeWorkRequestRecord, bool]:
        with self._session_factory() as session:
            session.add(
                LaunchplaneEveryCodeWorkRequestRow(
                    request_id=record.request_id,
                    source=record.source,
                    state=record.state,
                    repository=record.repository,
                    issue_number=record.issue_number,
                    trigger_label=record.trigger_label,
                    updated_at=record.updated_at,
                    claimed_by_host=record.claimed_by_host,
                    lease_expires_at=record.lease_expires_at,
                    fencing_token=record.fencing_token,
                    attempt=record.attempt,
                    payload=self._payload_dict(record),
                )
            )
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                return self.read_every_code_work_request_record(record.request_id), False
        return record, True

    def read_every_code_work_request_record(self, request_id: str) -> EveryCodeWorkRequestRecord:
        return self._read_model(
            model_type=EveryCodeWorkRequestRecord,
            orm_model=LaunchplaneEveryCodeWorkRequestRow,
            filters=(LaunchplaneEveryCodeWorkRequestRow.request_id == request_id,),
        )

    def list_every_code_work_request_records(
        self,
        *,
        state: str = "",
        repository: str = "",
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[EveryCodeWorkRequestRecord, ...]:
        filters: list[Any] = []
        if state:
            filters.append(LaunchplaneEveryCodeWorkRequestRow.state == state)
        if repository:
            filters.append(LaunchplaneEveryCodeWorkRequestRow.repository == repository)
        return self._list_models(
            model_type=EveryCodeWorkRequestRecord,
            orm_model=LaunchplaneEveryCodeWorkRequestRow,
            filters=filters,
            order_by=(
                LaunchplaneEveryCodeWorkRequestRow.updated_at.desc(),
                LaunchplaneEveryCodeWorkRequestRow.request_id.desc(),
            ),
            limit=limit,
            offset=offset,
        )

    def close_every_code_work_request_for_pull_request_record(
        self,
        *,
        request_id: str,
        expected_lifecycle_id: str,
        pr_url: str,
        merged: bool,
        closed_at: str,
    ) -> EveryCodeWorkRequestRecord | None:
        with self._session_factory() as session:
            statement = (
                select(LaunchplaneEveryCodeWorkRequestRow)
                .where(LaunchplaneEveryCodeWorkRequestRow.request_id == request_id)
                .limit(1)
            )
            if not self.database_url.startswith("sqlite"):
                statement = statement.with_for_update()
            row = session.scalar(statement)
            if row is None:
                raise FileNotFoundError(request_id)
            record = self._read_payload(model_type=EveryCodeWorkRequestRecord, payload=row.payload)
            if record.lifecycle_id != expected_lifecycle_id:
                return None
            closed_record = close_every_code_work_request_for_pull_request(
                record,
                pr_url=pr_url,
                merged=merged,
                closed_at=closed_at,
            )
            if closed_record is None:
                return None
            self._sync_every_code_work_request_row(row, closed_record)
            session.commit()
            return closed_record

    def claim_every_code_work_request_record(
        self,
        *,
        request_id: str,
        host: str,
        claimed_at: str,
        lease_seconds: int = 1800,
        idempotency_record_factory: (
            Callable[[EveryCodeWorkRequestRecord], LaunchplaneIdempotencyRecord] | None
        ) = None,
    ) -> EveryCodeWorkRequestRecord | None:
        with self._session_factory() as session:
            statement = (
                select(LaunchplaneEveryCodeWorkRequestRow)
                .where(LaunchplaneEveryCodeWorkRequestRow.request_id == request_id)
                .limit(1)
            )
            if not self.database_url.startswith("sqlite"):
                statement = statement.with_for_update()
            row = session.scalar(statement)
            if row is None:
                raise FileNotFoundError(request_id)
            record = self._read_payload(model_type=EveryCodeWorkRequestRecord, payload=row.payload)
            claimed_record = claim_every_code_work_request(
                record,
                host=host,
                claimed_at=claimed_at,
                lease_seconds=lease_seconds,
            )
            if claimed_record is None:
                return None
            self._sync_every_code_work_request_row(row, claimed_record)
            if idempotency_record_factory is not None:
                session.merge(self._idempotency_row(idempotency_record_factory(claimed_record)))
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                if idempotency_record_factory is not None:
                    return None
                raise
            return claimed_record

    def heartbeat_every_code_work_request_record(
        self,
        *,
        request_id: str,
        host: str,
        fencing_token: int,
        heartbeat_at: str,
        lease_expires_at: str,
        lease_seconds: int = 1800,
    ) -> bool:
        del lease_seconds
        with self._session_factory() as session:
            statement = (
                select(LaunchplaneEveryCodeWorkRequestRow)
                .where(LaunchplaneEveryCodeWorkRequestRow.request_id == request_id)
                .limit(1)
            )
            if not self.database_url.startswith("sqlite"):
                statement = statement.with_for_update()
            row = session.scalar(statement)
            if row is None:
                return False
            record = self._read_payload(model_type=EveryCodeWorkRequestRecord, payload=row.payload)
            updated = heartbeat_every_code_work_request(
                record,
                EveryCodeWorkRequestHeartbeat(
                    host=host,
                    fencing_token=fencing_token,
                    heartbeat_at=heartbeat_at,
                    lease_expires_at=lease_expires_at,
                ),
            )
            if updated is None:
                return False
            self._sync_every_code_work_request_row(row, updated)
            session.commit()
            return True

    def update_every_code_work_request_status_record(
        self,
        *,
        request_id: str,
        update: EveryCodeWorkRequestStatusUpdate,
    ) -> EveryCodeWorkRequestRecord:
        with self._session_factory() as session:
            statement = (
                select(LaunchplaneEveryCodeWorkRequestRow)
                .where(LaunchplaneEveryCodeWorkRequestRow.request_id == request_id)
                .limit(1)
            )
            if not self.database_url.startswith("sqlite"):
                statement = statement.with_for_update()
            row = session.scalar(statement)
            if row is None:
                raise FileNotFoundError(request_id)
            record = self._read_payload(model_type=EveryCodeWorkRequestRecord, payload=row.payload)
            if 0 < record.fencing_token != update.fencing_token:
                raise ValueError(
                    f"Every Code status update fencing token {update.fencing_token} "
                    f"does not match record fencing token {record.fencing_token}"
                )
            updated = apply_every_code_work_request_status(record, update)
            self._sync_every_code_work_request_row(row, updated)
            session.commit()
            return updated

    def compare_and_write_every_code_work_request_record(
        self,
        *,
        expected_record: EveryCodeWorkRequestRecord,
        record: EveryCodeWorkRequestRecord,
        idempotency_record: LaunchplaneIdempotencyRecord | None = None,
    ) -> Literal["updated", "changed", "missing"]:
        if expected_record.request_id != record.request_id:
            raise ValueError("Every Code compare-and-write requires matching request IDs")
        with self._session_factory() as session:
            statement = (
                select(LaunchplaneEveryCodeWorkRequestRow)
                .where(LaunchplaneEveryCodeWorkRequestRow.request_id == expected_record.request_id)
                .limit(1)
            )
            if not self.database_url.startswith("sqlite"):
                statement = statement.with_for_update()
            row = session.scalar(statement)
            if row is None:
                return "missing"
            current = self._read_payload(model_type=EveryCodeWorkRequestRecord, payload=row.payload)
            if self._payload_dict(current) != self._payload_dict(expected_record):
                return "changed"
            self._sync_every_code_work_request_row(row, record)
            if idempotency_record is not None:
                session.merge(self._idempotency_row(idempotency_record))
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                if idempotency_record is not None:
                    return "changed"
                raise
            return "updated"

    def list_stale_every_code_work_request_records(
        self,
        *,
        as_of: str,
        limit: int = 50,
    ) -> tuple[EveryCodeWorkRequestRecord, ...]:
        stale_states = ("claimed", "running")
        filters: list[Any] = [
            LaunchplaneEveryCodeWorkRequestRow.state.in_(stale_states),
            LaunchplaneEveryCodeWorkRequestRow.lease_expires_at != "",
            LaunchplaneEveryCodeWorkRequestRow.lease_expires_at < as_of,
        ]
        return self._list_models(
            model_type=EveryCodeWorkRequestRecord,
            orm_model=LaunchplaneEveryCodeWorkRequestRow,
            filters=filters,
            order_by=(
                LaunchplaneEveryCodeWorkRequestRow.lease_expires_at.asc(),
                LaunchplaneEveryCodeWorkRequestRow.request_id.asc(),
            ),
            limit=limit,
        )

    def recover_stale_every_code_work_request_record(
        self,
        *,
        expected_record: EveryCodeWorkRequestRecord,
        recovered_at: str,
    ) -> EveryCodeWorkRequestRecord | None:
        with self._session_factory() as session:
            statement = (
                select(LaunchplaneEveryCodeWorkRequestRow)
                .where(LaunchplaneEveryCodeWorkRequestRow.request_id == expected_record.request_id)
                .limit(1)
            )
            if not self.database_url.startswith("sqlite"):
                statement = statement.with_for_update()
            row = session.scalar(statement)
            if row is None:
                return None
            current = self._read_payload(model_type=EveryCodeWorkRequestRecord, payload=row.payload)
            if self._payload_dict(current) != self._payload_dict(expected_record):
                return None
            if not current.lease_expires_at or current.lease_expires_at >= recovered_at:
                return None
            recovered = recover_stale_every_code_work_request(
                current,
                recovered_at=recovered_at,
            )
            self._sync_every_code_work_request_row(row, recovered)
            session.commit()
            return recovered

    def _sync_every_code_work_request_row(
        self,
        row: LaunchplaneEveryCodeWorkRequestRow,
        record: EveryCodeWorkRequestRecord,
    ) -> None:
        row.state = record.state
        row.updated_at = record.updated_at
        row.claimed_by_host = record.claimed_by_host
        row.lease_expires_at = record.lease_expires_at
        row.fencing_token = record.fencing_token
        row.attempt = record.attempt
        row.payload = self._payload_dict(record)

    def write_every_code_pr_feedback_record(self, record: EveryCodePrFeedbackRecord) -> None:
        self._write_row(
            LaunchplaneEveryCodePrFeedbackRow(
                feedback_id=record.feedback_id,
                request_id=record.request_id,
                repository=record.repository,
                pr_number=record.pr_number,
                feedback_kind=record.feedback_kind,
                github_delivery_id=record.github_delivery_id,
                actor=record.actor,
                received_at=record.received_at,
                status=record.status,
                payload=self._payload_dict(record),
            )
        )

    def write_every_code_notification_policy_record(
        self, record: EveryCodeNotificationPolicyRecord
    ) -> None:
        self._write_row(
            LaunchplaneEveryCodeNotificationPolicyRow(
                policy_id=record.policy_id,
                repository=record.repository,
                status=record.status,
                updated_at=record.updated_at,
                payload=self._payload_dict(record),
            )
        )

    def list_every_code_notification_policy_records(
        self,
        *,
        repository: str = "",
        status: str = "",
        limit: int | None = None,
    ) -> tuple[EveryCodeNotificationPolicyRecord, ...]:
        filters: list[object] = []
        if repository:
            filters.append(
                LaunchplaneEveryCodeNotificationPolicyRow.repository.in_(("", repository))
            )
        if status:
            filters.append(LaunchplaneEveryCodeNotificationPolicyRow.status == status)
        return self._list_models(
            model_type=EveryCodeNotificationPolicyRecord,
            orm_model=LaunchplaneEveryCodeNotificationPolicyRow,
            filters=filters,
            order_by=(
                LaunchplaneEveryCodeNotificationPolicyRow.updated_at.desc(),
                LaunchplaneEveryCodeNotificationPolicyRow.policy_id.desc(),
            ),
            limit=limit,
        )

    def write_every_code_notification_attempt_record(
        self, record: EveryCodeNotificationAttemptRecord
    ) -> None:
        self._write_row(
            LaunchplaneEveryCodeNotificationAttemptRow(
                attempt_id=record.attempt_id,
                request_id=record.request_id,
                event=record.event,
                destination_kind=record.destination_kind,
                delivery_status=record.delivery_status,
                attempted_at=record.attempted_at,
                payload=self._payload_dict(record),
            )
        )

    def list_every_code_notification_attempt_records(
        self,
        *,
        request_id: str = "",
        event: str = "",
        destination_kind: str = "",
        limit: int | None = None,
    ) -> tuple[EveryCodeNotificationAttemptRecord, ...]:
        filters: list[object] = []
        if request_id:
            filters.append(LaunchplaneEveryCodeNotificationAttemptRow.request_id == request_id)
        if event:
            filters.append(LaunchplaneEveryCodeNotificationAttemptRow.event == event)
        if destination_kind:
            filters.append(
                LaunchplaneEveryCodeNotificationAttemptRow.destination_kind == destination_kind
            )
        return self._list_models(
            model_type=EveryCodeNotificationAttemptRecord,
            orm_model=LaunchplaneEveryCodeNotificationAttemptRow,
            filters=filters,
            order_by=(
                LaunchplaneEveryCodeNotificationAttemptRow.attempted_at.desc(),
                LaunchplaneEveryCodeNotificationAttemptRow.attempt_id.desc(),
            ),
            limit=limit,
        )

    def list_engineering_review_authority_records(
        self,
        *,
        repository: str = "",
        status: str = "",
        limit: int | None = None,
    ) -> tuple[EngineeringReviewAuthorityRecord, ...]:
        filters: list[Any] = []
        if repository:
            filters.append(LaunchplaneEngineeringReviewAuthorityRow.repository == repository)
        if status:
            filters.append(LaunchplaneEngineeringReviewAuthorityRow.status == status)
        return self._list_models(
            model_type=EngineeringReviewAuthorityRecord,
            orm_model=LaunchplaneEngineeringReviewAuthorityRow,
            filters=filters,
            order_by=(
                LaunchplaneEngineeringReviewAuthorityRow.policy_revision.desc(),
                LaunchplaneEngineeringReviewAuthorityRow.authority_id.desc(),
            ),
            limit=limit,
        )

    def compare_and_write_engineering_review_authority_record(
        self,
        record: EngineeringReviewAuthorityRecord,
        *,
        expected_current_authority_id: str,
        expected_current_authority_digest: str,
    ) -> Literal["written", "replayed"]:
        with self._session_factory() as session:
            self._begin_serialized_write(session)
            if not self.database_url.startswith("sqlite"):
                session.execute(
                    text("select pg_advisory_xact_lock(hashtextextended(:lock_name, 0))"),
                    {"lock_name": f"engineering-review-authority:{record.repository}"},
                )
            statement = select(LaunchplaneEngineeringReviewAuthorityRow).where(
                LaunchplaneEngineeringReviewAuthorityRow.repository == record.repository,
                LaunchplaneEngineeringReviewAuthorityRow.status == "active",
            )
            if not self.database_url.startswith("sqlite"):
                statement = statement.with_for_update()
            current_row = session.scalar(statement)
            current = (
                self._read_payload(
                    model_type=EngineeringReviewAuthorityRecord,
                    payload=current_row.payload,
                )
                if current_row is not None
                else None
            )
            existing_row = session.get(
                LaunchplaneEngineeringReviewAuthorityRow,
                record.authority_id,
            )
            if existing_row is not None:
                existing = self._read_payload(
                    model_type=EngineeringReviewAuthorityRecord,
                    payload=existing_row.payload,
                )
                if (
                    existing.authority_digest == record.authority_digest
                    and existing.status == record.status == "active"
                    and current is not None
                    and current.authority_id == existing.authority_id
                ):
                    session.rollback()
                    return "replayed"
                raise EngineeringReviewConflictError(
                    "Engineering review authority id is stale or has different content."
                )
            if current is None:
                if expected_current_authority_id or expected_current_authority_digest:
                    raise EngineeringReviewConflictError(
                        "Engineering review authority expected-current values are stale."
                    )
                if record.policy_revision != 1 or record.supersedes_authority_id is not None:
                    raise EngineeringReviewSequenceError(
                        "Initial engineering review authority must use revision 1."
                    )
            else:
                if (
                    current.authority_id != expected_current_authority_id
                    or current.authority_digest != expected_current_authority_digest
                ):
                    raise EngineeringReviewConflictError(
                        "Engineering review authority expected-current values are stale."
                    )
                if (
                    record.policy_revision != current.policy_revision + 1
                    or record.supersedes_authority_id != current.authority_id
                ):
                    raise EngineeringReviewSequenceError(
                        "Engineering review authority successor is non-contiguous."
                    )
                assert current_row is not None
                retired = current.model_copy(update={"status": "retired"})
                current_row.status = retired.status
                current_row.payload = self._payload_dict(retired)
            session.add(
                LaunchplaneEngineeringReviewAuthorityRow(
                    authority_id=record.authority_id,
                    repository=record.repository,
                    status=record.status,
                    policy_revision=record.policy_revision,
                    authority_digest=record.authority_digest,
                    recorded_at=record.recorded_at,
                    payload=self._payload_dict(record),
                )
            )
            session.commit()
        return "written"

    def create_engineering_review_run_record_if_absent(
        self, record: EngineeringReviewRunRecord
    ) -> tuple[EngineeringReviewRunRecord, bool]:
        records, created = self.create_engineering_review_run_records_if_absent(
            (record,),
            expected_authority_id=record.authority_id,
            expected_authority_digest=record.authority_digest,
            expected_work_request_lifecycle_id=record.work_request_lifecycle_id,
        )
        return records[0], created

    def create_engineering_review_run_records_if_absent(
        self,
        records: tuple[EngineeringReviewRunRecord, ...],
        *,
        expected_authority_id: str,
        expected_authority_digest: str,
        expected_work_request_lifecycle_id: str,
    ) -> tuple[tuple[EngineeringReviewRunRecord, ...], bool]:
        if not records:
            raise ValueError("Engineering review scheduling requires at least one run.")
        first = records[0]
        if any(record.work_request_id != first.work_request_id for record in records):
            raise ValueError("Engineering review run batch must share one work request.")
        with self._session_factory() as session:
            self._begin_serialized_write(session)
            authority_statement = select(LaunchplaneEngineeringReviewAuthorityRow).where(
                LaunchplaneEngineeringReviewAuthorityRow.repository == first.repository,
                LaunchplaneEngineeringReviewAuthorityRow.status == "active",
            )
            work_request_statement = select(LaunchplaneEveryCodeWorkRequestRow).where(
                LaunchplaneEveryCodeWorkRequestRow.request_id == first.work_request_id
            )
            if not self.database_url.startswith("sqlite"):
                authority_statement = authority_statement.with_for_update()
                work_request_statement = work_request_statement.with_for_update()
            authority_row = session.scalar(authority_statement)
            work_request_row = session.scalar(work_request_statement)
            if authority_row is None or work_request_row is None:
                raise EngineeringReviewConflictError(
                    "Engineering review scheduling authority or work request disappeared."
                )
            authority = self._read_payload(
                model_type=EngineeringReviewAuthorityRecord,
                payload=authority_row.payload,
            )
            work_request = self._read_payload(
                model_type=EveryCodeWorkRequestRecord,
                payload=work_request_row.payload,
            )
            if (
                authority.authority_id != expected_authority_id
                or authority.authority_digest != expected_authority_digest
                or work_request.lifecycle_id != expected_work_request_lifecycle_id
                or work_request.state != "done"
            ):
                raise EngineeringReviewConflictError(
                    "Engineering review scheduling evidence changed before persistence."
                )
            stored: list[EngineeringReviewRunRecord] = []
            created_all = True
            for record in records:
                existing_row = session.get(LaunchplaneEngineeringReviewRunRow, record.run_id)
                if existing_row is not None:
                    existing = self._read_payload(
                        model_type=EngineeringReviewRunRecord,
                        payload=existing_row.payload,
                    )
                    if existing.assignment_fingerprint != record.assignment_fingerprint:
                        raise EngineeringReviewConflictError(
                            "Engineering review run id replay conflicts with stored assignment."
                        )
                    stored.append(existing)
                    created_all = False
                    continue
                session.add(self._engineering_review_run_row(record))
                stored.append(record)
            session.commit()
        return tuple(stored), created_all

    def read_engineering_review_run_record(self, run_id: str) -> EngineeringReviewRunRecord:
        return self._read_model(
            model_type=EngineeringReviewRunRecord,
            orm_model=LaunchplaneEngineeringReviewRunRow,
            filters=(LaunchplaneEngineeringReviewRunRow.run_id == run_id,),
        )

    def list_engineering_review_run_records(
        self,
        *,
        repository: str = "",
        pr_number: int | None = None,
        head_sha: str = "",
        work_request_id: str = "",
        worker_runtime_id: str = "",
        worker_host: str = "",
        state: str = "",
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[EngineeringReviewRunRecord, ...]:
        filters: list[Any] = []
        if repository:
            filters.append(LaunchplaneEngineeringReviewRunRow.repository == repository)
        if pr_number is not None:
            filters.append(LaunchplaneEngineeringReviewRunRow.pr_number == pr_number)
        if head_sha:
            filters.append(LaunchplaneEngineeringReviewRunRow.head_sha == head_sha)
        if work_request_id:
            filters.append(LaunchplaneEngineeringReviewRunRow.work_request_id == work_request_id)
        if worker_runtime_id:
            filters.append(
                LaunchplaneEngineeringReviewRunRow.worker_runtime_id == worker_runtime_id
            )
        if worker_host:
            filters.append(LaunchplaneEngineeringReviewRunRow.worker_host == worker_host)
        if state:
            filters.append(LaunchplaneEngineeringReviewRunRow.state == state)
        return self._list_models(
            model_type=EngineeringReviewRunRecord,
            orm_model=LaunchplaneEngineeringReviewRunRow,
            filters=filters,
            order_by=(
                LaunchplaneEngineeringReviewRunRow.created_at.desc(),
                LaunchplaneEngineeringReviewRunRow.run_id.desc(),
            ),
            limit=limit,
            offset=offset,
        )

    def claim_engineering_review_run_record(
        self,
        *,
        run_id: str,
        worker_runtime_id: str,
        worker_host: str,
    ) -> EngineeringReviewRunRecord:
        with self._session_factory() as session:
            self._begin_serialized_write(session)
            row = self._locked_engineering_review_run_row(session, run_id)
            record = self._read_payload(
                model_type=EngineeringReviewRunRecord,
                payload=row.payload,
            )
            self._require_engineering_review_worker(record, worker_runtime_id, worker_host)
            now = self._database_mutation_timestamp(session)
            if record.state in {"claimed", "running"}:
                if record.lease_expires_at <= now:
                    expired = expire_engineering_review_run(record, expired_at=now)
                    self._sync_engineering_review_run_row(row, expired)
                    session.commit()
                    raise EngineeringReviewConflictError("Engineering review run lease expired.")
                session.rollback()
                return record
            if record.state != "pending":
                raise EngineeringReviewConflictError(
                    f"Engineering review run cannot be claimed from state {record.state!r}."
                )
            claimed = claim_engineering_review_run(record, claimed_at=now)
            self._sync_engineering_review_run_row(row, claimed)
            session.commit()
            return claimed

    def start_engineering_review_run_record(
        self,
        *,
        run_id: str,
        worker_runtime_id: str,
        worker_host: str,
        fencing_token: int,
    ) -> EngineeringReviewRunRecord:
        with self._session_factory() as session:
            self._begin_serialized_write(session)
            row = self._locked_engineering_review_run_row(session, run_id)
            record = self._read_payload(
                model_type=EngineeringReviewRunRecord,
                payload=row.payload,
            )
            self._require_engineering_review_worker(
                record,
                worker_runtime_id,
                worker_host,
                fencing_token=fencing_token,
            )
            now = self._database_mutation_timestamp(session)
            if record.state == "running":
                session.rollback()
                return record
            if record.state != "claimed" or record.lease_expires_at <= now:
                if record.state == "claimed" and record.lease_expires_at <= now:
                    expired = expire_engineering_review_run(record, expired_at=now)
                    self._sync_engineering_review_run_row(row, expired)
                    session.commit()
                raise EngineeringReviewConflictError("Engineering review run cannot start.")
            running = start_engineering_review_run(record, started_at=now)
            self._sync_engineering_review_run_row(row, running)
            session.commit()
            return running

    def submit_engineering_review_run_record(
        self,
        submission: EngineeringReviewRunSubmission,
    ) -> EngineeringReviewRunRecord:
        credential_hash = hashlib.sha256(submission.credential.encode()).hexdigest()
        with self._session_factory() as session:
            self._begin_serialized_write(session)
            statement = select(LaunchplaneEngineeringReviewRunRow).where(
                LaunchplaneEngineeringReviewRunRow.credential_hash == credential_hash
            )
            if not self.database_url.startswith("sqlite"):
                statement = statement.with_for_update()
            row = session.scalar(statement)
            if row is None:
                raise EngineeringReviewConflictError("Engineering review submission rejected.")
            record = self._read_payload(
                model_type=EngineeringReviewRunRecord,
                payload=row.payload,
            )
            now = self._database_mutation_timestamp(session)
            if record.state in {"claimed", "running"} and record.lease_expires_at <= now:
                expired = expire_engineering_review_run(record, expired_at=now)
                self._sync_engineering_review_run_row(row, expired)
                session.commit()
                raise EngineeringReviewConflictError("Engineering review run lease expired.")
            try:
                completed = submit_engineering_review_run(
                    record,
                    submission,
                    completed_at=now,
                )
            except ValueError as error:
                raise EngineeringReviewConflictError(str(error)) from error
            if completed == record:
                session.rollback()
                return record
            self._sync_engineering_review_run_row(row, completed)
            session.commit()
            return completed

    def fail_engineering_review_run_record(
        self,
        *,
        run_id: str,
        worker_runtime_id: str,
        worker_host: str,
        fencing_token: int,
        failure: EngineeringReviewRunFailure,
    ) -> EngineeringReviewRunRecord:
        with self._session_factory() as session:
            self._begin_serialized_write(session)
            row = self._locked_engineering_review_run_row(session, run_id)
            record = self._read_payload(
                model_type=EngineeringReviewRunRecord,
                payload=row.payload,
            )
            self._require_engineering_review_worker(
                record,
                worker_runtime_id,
                worker_host,
                fencing_token=fencing_token,
            )
            expected_error = f"{failure.error_code}: {failure.summary}"
            if record.state == "failed":
                if record.error_message == expected_error:
                    session.rollback()
                    return record
                raise EngineeringReviewConflictError(
                    "Engineering review failure replay conflicts with stored failure."
                )
            now = self._database_mutation_timestamp(session)
            if record.state in {"claimed", "running"} and record.lease_expires_at <= now:
                expired = expire_engineering_review_run(record, expired_at=now)
                self._sync_engineering_review_run_row(row, expired)
                session.commit()
                raise EngineeringReviewConflictError("Engineering review run lease expired.")
            failed = fail_engineering_review_run(record, failure, failed_at=now)
            self._sync_engineering_review_run_row(row, failed)
            session.commit()
            return failed

    def expire_stale_engineering_review_run_records(
        self,
        *,
        limit: int = 50,
    ) -> tuple[EngineeringReviewRunRecord, ...]:
        with self._session_factory() as session:
            self._begin_serialized_write(session)
            now = self._database_mutation_timestamp(session)
            statement = (
                select(LaunchplaneEngineeringReviewRunRow)
                .where(
                    LaunchplaneEngineeringReviewRunRow.state.in_(("claimed", "running")),
                    LaunchplaneEngineeringReviewRunRow.lease_expires_at <= now,
                )
                .order_by(LaunchplaneEngineeringReviewRunRow.lease_expires_at.asc())
                .limit(max(1, min(limit, 200)))
            )
            if not self.database_url.startswith("sqlite"):
                statement = statement.with_for_update(skip_locked=True)
            expired_records: list[EngineeringReviewRunRecord] = []
            for row in session.scalars(statement):
                record = self._read_payload(
                    model_type=EngineeringReviewRunRecord,
                    payload=row.payload,
                )
                expired = expire_engineering_review_run(record, expired_at=now)
                self._sync_engineering_review_run_row(row, expired)
                expired_records.append(expired)
            session.commit()
        return tuple(expired_records)

    def _engineering_review_run_row(
        self, record: EngineeringReviewRunRecord
    ) -> LaunchplaneEngineeringReviewRunRow:
        return LaunchplaneEngineeringReviewRunRow(
            run_id=record.run_id,
            assignment_fingerprint=record.assignment_fingerprint,
            review_slot=record.review_slot,
            state=record.state,
            repository=record.repository,
            pr_number=record.pr_number,
            head_sha=record.head_sha,
            authority_id=record.authority_id,
            authority_digest=record.authority_digest,
            work_request_id=record.work_request_id,
            work_request_lifecycle_id=record.work_request_lifecycle_id,
            policy_revision=record.policy_revision,
            worker_runtime_id=record.worker_runtime_id,
            worker_host=record.worker_host,
            credential_hash=record.credential_hash,
            lease_expires_at=record.lease_expires_at,
            fencing_token=record.fencing_token,
            created_at=record.created_at,
            updated_at=record.updated_at,
            payload=self._payload_dict(record),
        )

    def _locked_engineering_review_run_row(
        self, session: Any, run_id: str
    ) -> LaunchplaneEngineeringReviewRunRow:
        statement = select(LaunchplaneEngineeringReviewRunRow).where(
            LaunchplaneEngineeringReviewRunRow.run_id == run_id
        )
        if not self.database_url.startswith("sqlite"):
            statement = statement.with_for_update()
        row = cast(LaunchplaneEngineeringReviewRunRow | None, session.scalar(statement))
        if row is None:
            raise FileNotFoundError(f"Engineering review run not found: {run_id}")
        return row

    @staticmethod
    def _require_engineering_review_worker(
        record: EngineeringReviewRunRecord,
        worker_runtime_id: str,
        worker_host: str,
        *,
        fencing_token: int | None = None,
    ) -> None:
        if (
            record.worker_runtime_id != worker_runtime_id.strip()
            or record.worker_host != worker_host.strip()
        ):
            raise EngineeringReviewConflictError(
                "Engineering review worker identity does not match the assignment."
            )
        if fencing_token is not None and record.fencing_token != fencing_token:
            raise EngineeringReviewConflictError(
                "Engineering review worker fencing token is stale."
            )

    def _sync_engineering_review_run_row(
        self,
        row: LaunchplaneEngineeringReviewRunRow,
        record: EngineeringReviewRunRecord,
    ) -> None:
        row.state = record.state
        row.lease_expires_at = record.lease_expires_at
        row.fencing_token = record.fencing_token
        row.updated_at = record.updated_at
        row.payload = self._payload_dict(record)

    def write_engineering_review_decision_record_if_absent(
        self,
        record: EngineeringReviewDecisionRecord,
    ) -> tuple[EngineeringReviewDecisionRecord, bool]:
        with self._session_factory() as session:
            self._begin_serialized_write(session)
            existing_row = session.get(
                LaunchplaneEngineeringReviewDecisionRow,
                record.decision_id,
            )
            if existing_row is not None:
                existing = self._read_payload(
                    model_type=EngineeringReviewDecisionRecord,
                    payload=existing_row.payload,
                )
                if (existing.binding_hash_version, existing.decision_binding_sha256) != (
                    record.binding_hash_version,
                    record.decision_binding_sha256,
                ):
                    raise EngineeringReviewConflictError(
                        "Engineering review decision id conflicts with stored evidence."
                    )
                session.rollback()
                return existing, False
            session.add(
                LaunchplaneEngineeringReviewDecisionRow(
                    decision_id=record.decision_id,
                    decision_binding_sha256=record.decision_binding_sha256,
                    repository=record.target.repository,
                    pull_request_number=record.target.pull_request_number,
                    head_sha=record.target.head_sha,
                    tree_sha=record.target.tree_sha,
                    work_request_id=record.work_request_id,
                    status=record.status,
                    evaluated_at=record.evaluated_at,
                    payload=self._payload_dict(record),
                )
            )
            session.commit()
        return record, True

    def read_engineering_review_decision_record(
        self,
        decision_id: str,
    ) -> EngineeringReviewDecisionRecord:
        return self._read_model(
            model_type=EngineeringReviewDecisionRecord,
            orm_model=LaunchplaneEngineeringReviewDecisionRow,
            filters=(LaunchplaneEngineeringReviewDecisionRow.decision_id == decision_id,),
        )

    def list_engineering_review_decision_records(
        self,
        *,
        repository: str = "",
        pull_request_number: int | None = None,
        head_sha: str = "",
        work_request_id: str = "",
        limit: int | None = None,
    ) -> tuple[EngineeringReviewDecisionRecord, ...]:
        filters: list[object] = []
        if repository:
            filters.append(LaunchplaneEngineeringReviewDecisionRow.repository == repository)
        if pull_request_number is not None:
            filters.append(
                LaunchplaneEngineeringReviewDecisionRow.pull_request_number == pull_request_number
            )
        if head_sha:
            filters.append(LaunchplaneEngineeringReviewDecisionRow.head_sha == head_sha)
        if work_request_id:
            filters.append(
                LaunchplaneEngineeringReviewDecisionRow.work_request_id == work_request_id
            )
        return self._list_models(
            model_type=EngineeringReviewDecisionRecord,
            orm_model=LaunchplaneEngineeringReviewDecisionRow,
            filters=filters,
            order_by=(
                LaunchplaneEngineeringReviewDecisionRow.evaluated_at.desc(),
                LaunchplaneEngineeringReviewDecisionRow.decision_id.desc(),
            ),
            limit=limit,
        )

    def write_agent_write_intent_record(self, record: AgentWriteIntentRecord) -> None:
        self._write_row(
            LaunchplaneAgentWriteIntentRow(
                record_id=record.record_id,
                recorded_at=record.recorded_at,
                trace_id=record.trace_id,
                intent=record.evaluation.intent,
                mode=record.evaluation.mode,
                status=record.evaluation.status,
                authz_action=record.evaluation.authz_action,
                product=record.evaluation.product,
                context=record.evaluation.context,
                payload=self._payload_dict(record),
            )
        )

    def read_agent_write_intent_record(self, record_id: str) -> AgentWriteIntentRecord:
        return self._read_model(
            model_type=AgentWriteIntentRecord,
            orm_model=LaunchplaneAgentWriteIntentRow,
            filters=(LaunchplaneAgentWriteIntentRow.record_id == record_id,),
        )

    def list_agent_write_intent_records(
        self,
        *,
        status: str = "",
        product: str = "",
        context_name: str = "",
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[AgentWriteIntentRecord, ...]:
        filters: list[object] = []
        if status:
            filters.append(LaunchplaneAgentWriteIntentRow.status == status)
        if product:
            filters.append(LaunchplaneAgentWriteIntentRow.product == product)
        if context_name:
            filters.append(LaunchplaneAgentWriteIntentRow.context == context_name)
        return self._list_models(
            model_type=AgentWriteIntentRecord,
            orm_model=LaunchplaneAgentWriteIntentRow,
            filters=filters,
            order_by=(
                LaunchplaneAgentWriteIntentRow.recorded_at.desc(),
                LaunchplaneAgentWriteIntentRow.record_id.desc(),
            ),
            limit=limit,
            offset=offset,
        )

    def write_merge_train_run_record(self, record: MergeTrainRunRecord) -> None:
        self._write_row(
            LaunchplaneMergeTrainRunRow(
                run_id=record.run_id,
                recorded_at=record.recorded_at,
                trace_id=record.trace_id,
                repository=record.repository,
                base_branch=record.base_branch,
                mode=record.mode,
                status=record.status,
                intended_next_action=record.intended_next_action,
                selected_pr_number=record.selected_pr_number,
                policy_key=record.policy_key,
                policy_sha256=record.policy_sha256,
                payload=self._payload_dict(record),
            )
        )

    def write_merge_train_policy_record(self, record: MergeTrainPolicyRecord) -> None:
        with self._session_factory() as session:
            self._begin_serialized_write(session)
            self._lock_merge_train_policy_write(session)
            existing_row = session.get(LaunchplaneMergeTrainPolicyRow, record.record_id)
            if existing_row is not None and existing_row.policy_sha256 != record.policy_sha256:
                raise ValueError(
                    "Merge-train policy record ID cannot be reused for different policy content."
                )
            if record.status == "active":
                active_rows = tuple(
                    session.scalars(
                        select(LaunchplaneMergeTrainPolicyRow).where(
                            LaunchplaneMergeTrainPolicyRow.status == "active",
                            LaunchplaneMergeTrainPolicyRow.record_id != record.record_id,
                        )
                    ).all()
                )
                for active_row in active_rows:
                    active_record = self._read_payload(
                        model_type=MergeTrainPolicyRecord,
                        payload=active_row.payload,
                    )
                    superseded_record = active_record.model_copy(update={"status": "superseded"})
                    active_row.status = "superseded"
                    active_row.payload = self._payload_dict(superseded_record)
            session.merge(
                LaunchplaneMergeTrainPolicyRow(
                    record_id=record.record_id,
                    status=record.status,
                    source=record.source,
                    updated_at=record.updated_at,
                    policy_sha256=record.policy_sha256,
                    payload=self._payload_dict(record),
                )
            )
            session.commit()

    def read_merge_train_policy_record(self, record_id: str) -> MergeTrainPolicyRecord:
        return self._read_model(
            model_type=MergeTrainPolicyRecord,
            orm_model=LaunchplaneMergeTrainPolicyRow,
            filters=(LaunchplaneMergeTrainPolicyRow.record_id == record_id,),
        )

    def compare_and_write_merge_train_policy_record(
        self,
        *,
        expected_record: MergeTrainPolicyRecord,
        replacement_record: MergeTrainPolicyRecord,
        mutation: DbOnlyMutationRequest | None = None,
    ) -> MergeTrainPolicyCompareWriteResult:
        if expected_record.status != "active":
            raise ValueError("Merge-train policy compare-and-write expected record must be active.")
        if replacement_record.status != "active":
            raise ValueError("Merge-train policy compare-and-write replacement must be active.")
        if mutation is not None:
            if not 100 <= mutation.response_status_code <= 599:
                raise ValueError("DB-only mutation response status must be between 100 and 599.")
            if not mutation.response_trace_id.strip():
                raise ValueError("DB-only mutation response trace id is required.")
        statement = (
            select(LaunchplaneMergeTrainPolicyRow)
            .where(LaunchplaneMergeTrainPolicyRow.status == "active")
            .order_by(desc(LaunchplaneMergeTrainPolicyRow.updated_at))
        )
        if not self.database_url.startswith("sqlite"):
            statement = statement.with_for_update()
        if mutation is None:
            with self._session_factory() as session:
                self._begin_serialized_write(session)
                return self._compare_and_write_merge_train_policy_locked(
                    session=session,
                    statement=statement,
                    expected_record=expected_record,
                    replacement_record=replacement_record,
                )

        reservation_insert_error: IntegrityError | None = None
        with self._session_factory() as session:
            self._begin_serialized_write(session)
            observed_at = self._database_mutation_timestamp(session)
            stored_reservation = build_launchplane_mutation_reservation(
                scope=mutation.scope,
                route_path=mutation.route_path,
                idempotency_key=mutation.idempotency_key,
                request_fingerprint=mutation.request_fingerprint,
                lease_owner=mutation.lease_owner,
                lease_expires_at=self._mutation_lease_expiry(
                    observed_at=observed_at,
                    lease_seconds=mutation.lease_seconds,
                ),
                reserved_at=observed_at,
            )
            reservation_row = self._idempotency_row(stored_reservation)
            session.add(reservation_row)
            try:
                session.flush()
            except IntegrityError as error:
                session.rollback()
                reservation_insert_error = error
            if reservation_insert_error is None:
                return self._compare_and_write_merge_train_policy_locked(
                    session=session,
                    statement=statement,
                    expected_record=expected_record,
                    replacement_record=replacement_record,
                    reservation_row=reservation_row,
                    mutation_reservation=stored_reservation,
                    mutation=mutation,
                )

        with self._session_factory() as session:
            self._begin_serialized_write(session)
            reservation_row = session.scalar(
                self._idempotency_statement(
                    scope=mutation.scope,
                    route_path=mutation.route_path,
                    idempotency_key=mutation.idempotency_key,
                    for_update=True,
                )
            )
            if reservation_row is None:
                assert reservation_insert_error is not None
                raise reservation_insert_error
            current_reservation = self._read_payload(
                model_type=LaunchplaneIdempotencyRecord,
                payload=reservation_row.payload,
            )
            if current_reservation.request_fingerprint != mutation.request_fingerprint:
                return MergeTrainPolicyCompareWriteResult(
                    status="idempotency_conflict",
                    idempotency_record=current_reservation,
                )
            if current_reservation.state == "completed":
                active_records = self.list_merge_train_policy_records(status="active", limit=2)
                return MergeTrainPolicyCompareWriteResult(
                    status="replayed",
                    current_record=active_records[0] if len(active_records) == 1 else None,
                    idempotency_record=current_reservation,
                )
            if current_reservation.state == "reconcile_required":
                return MergeTrainPolicyCompareWriteResult(
                    status="reconciliation_required",
                    idempotency_record=current_reservation,
                )
            observed_at = self._database_mutation_timestamp(session)
            if parse_launchplane_mutation_timestamp(
                current_reservation.lease_expires_at,
                field_name="lease_expires_at",
            ) > parse_launchplane_mutation_timestamp(
                observed_at,
                field_name="observed_at",
            ):
                return MergeTrainPolicyCompareWriteResult(
                    status="reservation_in_progress",
                    idempotency_record=current_reservation,
                )
            if current_reservation.reconciliation_key:
                reconcile_record = self._updated_idempotency_record(
                    current_reservation,
                    state="reconcile_required",
                    updated_at=observed_at,
                )
                self._sync_idempotency_row(reservation_row, reconcile_record)
                session.commit()
                return MergeTrainPolicyCompareWriteResult(
                    status="reconciliation_required",
                    idempotency_record=reconcile_record,
                )
            reclaimed_reservation = self._updated_idempotency_record(
                current_reservation,
                lease_owner=mutation.lease_owner,
                lease_expires_at=self._mutation_lease_expiry(
                    observed_at=observed_at,
                    lease_seconds=mutation.lease_seconds,
                ),
                attempt=current_reservation.attempt + 1,
                updated_at=observed_at,
                response_status_code=None,
                response_trace_id="",
                recorded_at="",
                response_payload={},
            )
            self._sync_idempotency_row(reservation_row, reclaimed_reservation)
            return self._compare_and_write_merge_train_policy_locked(
                session=session,
                statement=statement,
                expected_record=expected_record,
                replacement_record=replacement_record,
                reservation_row=reservation_row,
                mutation_reservation=reclaimed_reservation,
                mutation=mutation,
            )

    def _compare_and_write_merge_train_policy_locked(
        self,
        *,
        session: Any,
        statement: Any,
        expected_record: MergeTrainPolicyRecord,
        replacement_record: MergeTrainPolicyRecord,
        reservation_row: LaunchplaneIdempotencyRow | None = None,
        mutation_reservation: LaunchplaneIdempotencyRecord | None = None,
        mutation: DbOnlyMutationRequest | None = None,
    ) -> MergeTrainPolicyCompareWriteResult:
        self._lock_merge_train_policy_write(session)
        active_rows = tuple(session.scalars(statement).all())
        if not active_rows:
            if reservation_row is not None:
                session.delete(reservation_row)
                session.commit()
            return MergeTrainPolicyCompareWriteResult(status="missing")
        if len(active_rows) > 1:
            if reservation_row is not None:
                session.delete(reservation_row)
                session.commit()
            return MergeTrainPolicyCompareWriteResult(status="ambiguous_active")
        active_row = active_rows[0]
        current_record = self._read_payload(
            model_type=MergeTrainPolicyRecord,
            payload=active_row.payload,
        )
        if (
            current_record.record_id != expected_record.record_id
            or current_record.policy_sha256 != expected_record.policy_sha256
            or current_record.updated_at != expected_record.updated_at
        ):
            if reservation_row is not None:
                session.delete(reservation_row)
                session.commit()
            return MergeTrainPolicyCompareWriteResult(status="stale", current_record=current_record)
        if (
            current_record.record_id == replacement_record.record_id
            and current_record.policy_sha256 != replacement_record.policy_sha256
        ):
            if reservation_row is not None:
                session.delete(reservation_row)
                session.commit()
            return MergeTrainPolicyCompareWriteResult(
                status="record_id_conflict", current_record=current_record
            )
        if (
            replacement_record.record_id != current_record.record_id
            and session.get(
                LaunchplaneMergeTrainPolicyRow,
                replacement_record.record_id,
            )
            is not None
        ):
            if reservation_row is not None:
                session.delete(reservation_row)
                session.commit()
            return MergeTrainPolicyCompareWriteResult(
                status="record_id_conflict", current_record=current_record
            )

        result_record = current_record
        status: Literal["written", "unchanged"] = "unchanged"
        if current_record.policy_sha256 != replacement_record.policy_sha256:
            superseded_record = current_record.model_copy(update={"status": "superseded"})
            active_row.status = "superseded"
            active_row.payload = self._payload_dict(superseded_record)
            session.flush()
            session.add(
                LaunchplaneMergeTrainPolicyRow(
                    record_id=replacement_record.record_id,
                    status=replacement_record.status,
                    source=replacement_record.source,
                    updated_at=replacement_record.updated_at,
                    policy_sha256=replacement_record.policy_sha256,
                    payload=self._payload_dict(replacement_record),
                )
            )
            session.flush()
            result_record = replacement_record
            status = "written"

        stored_completion: LaunchplaneIdempotencyRecord | None = None
        if reservation_row is not None:
            if mutation_reservation is None or mutation is None:
                raise RuntimeError("Merge-train policy mutation completion evidence is incomplete.")
            stored_completion = complete_launchplane_mutation_reservation(
                mutation_reservation,
                response_status_code=mutation.response_status_code,
                response_trace_id=mutation.response_trace_id,
                completed_at=self._database_mutation_timestamp(session),
                response_payload=mutation.response_payload,
            )
            self._sync_idempotency_row(reservation_row, stored_completion)
        session.commit()
        return MergeTrainPolicyCompareWriteResult(
            status=status,
            current_record=result_record,
            idempotency_record=stored_completion,
        )

    def write_merge_train_pr_feedback_record(self, record: MergeTrainPrFeedbackRecord) -> None:
        self._write_row(
            LaunchplaneMergeTrainPrFeedbackRow(
                feedback_id=record.feedback_id,
                repository=record.repository,
                base_branch=record.base_branch,
                pull_request_number=record.pull_request_number,
                event=record.event,
                delivery_status=record.delivery_status,
                recorded_at=record.recorded_at,
                payload=self._payload_dict(record),
            )
        )

    def list_merge_train_pr_feedback_records(
        self,
        *,
        repository: str = "",
        base_branch: str = "",
        pr_number: int | None = None,
        limit: int | None = None,
    ) -> tuple[MergeTrainPrFeedbackRecord, ...]:
        filters: list[object] = []
        if repository:
            filters.append(LaunchplaneMergeTrainPrFeedbackRow.repository == repository)
        if base_branch:
            filters.append(LaunchplaneMergeTrainPrFeedbackRow.base_branch == base_branch)
        if pr_number is not None:
            filters.append(LaunchplaneMergeTrainPrFeedbackRow.pull_request_number == pr_number)
        return self._list_models(
            model_type=MergeTrainPrFeedbackRecord,
            orm_model=LaunchplaneMergeTrainPrFeedbackRow,
            filters=filters,
            order_by=(
                LaunchplaneMergeTrainPrFeedbackRow.recorded_at.desc(),
                LaunchplaneMergeTrainPrFeedbackRow.feedback_id.desc(),
            ),
            limit=limit,
        )

    def write_merge_train_batch_candidate_record(
        self, record: MergeTrainBatchCandidateRecord
    ) -> None:
        self._write_row(
            LaunchplaneMergeTrainBatchCandidateRow(
                record_id=record.record_id,
                status=record.status,
                source=record.source,
                updated_at=record.updated_at,
                repository=record.candidate.repository,
                base_branch=record.candidate.base_branch,
                batch_id=record.candidate.batch_id,
                candidate_status=record.candidate.status,
                payload=self._payload_dict(record),
            )
        )

    def write_merge_train_controller_state_record(
        self, record: MergeTrainControllerStateRecord
    ) -> None:
        self._write_row(
            LaunchplaneMergeTrainControllerStateRow(
                controller_key=record.controller_key,
                repository=record.repository,
                base_branch=record.base_branch,
                status=record.status,
                policy_key=record.policy_key,
                policy_sha256=record.policy_sha256,
                updated_at=record.updated_at,
                lease_owner=record.lease_owner,
                lease_expires_at=record.lease_expires_at,
                active_action=record.active_action,
                active_phase=record.active_phase,
                payload=self._payload_dict(record),
            )
        )

    def read_merge_train_controller_state_record(
        self, controller_key: str
    ) -> MergeTrainControllerStateRecord:
        return self._read_model(
            model_type=MergeTrainControllerStateRecord,
            orm_model=LaunchplaneMergeTrainControllerStateRow,
            filters=(LaunchplaneMergeTrainControllerStateRow.controller_key == controller_key,),
        )

    def list_merge_train_controller_state_records(
        self,
        *,
        repository: str = "",
        base_branch: str = "",
        status: str = "",
        limit: int | None = None,
    ) -> tuple[MergeTrainControllerStateRecord, ...]:
        filters: list[object] = []
        if repository:
            filters.append(LaunchplaneMergeTrainControllerStateRow.repository == repository)
        if base_branch:
            filters.append(LaunchplaneMergeTrainControllerStateRow.base_branch == base_branch)
        if status:
            filters.append(LaunchplaneMergeTrainControllerStateRow.status == status)
        return self._list_models(
            model_type=MergeTrainControllerStateRecord,
            orm_model=LaunchplaneMergeTrainControllerStateRow,
            filters=filters,
            order_by=(
                LaunchplaneMergeTrainControllerStateRow.updated_at.desc(),
                LaunchplaneMergeTrainControllerStateRow.controller_key.desc(),
            ),
            limit=limit,
        )

    def acquire_merge_train_controller_state_record(
        self,
        *,
        repository: str,
        base_branch: str,
        policy_key: str,
        policy_sha256: str,
        lease_owner: str,
        lease_seconds: int,
        initial_active_action: str,
        initial_active_phase: str,
        adoptable_active_actions: tuple[str, ...],
    ) -> MergeTrainControllerStateRecord:
        normalized_initial_action = initial_active_action.strip()
        normalized_initial_phase = initial_active_phase.strip()
        normalized_adoptable_actions = tuple(
            dict.fromkeys(action.strip() for action in adoptable_active_actions if action.strip())
        )
        if not normalized_initial_action or not normalized_initial_phase:
            raise ValueError("merge train controller acquisition requires initial action and phase")
        if normalized_initial_action not in normalized_adoptable_actions:
            raise ValueError(
                "initial controller action must be adoptable by the acquiring controller"
            )
        controller_key = build_merge_train_controller_key(
            repository=repository,
            base_branch=base_branch,
        )
        with self._session_factory() as session:
            self._begin_serialized_write(session)
            self._advisory_lock_merge_train_controller(session, controller_key)
            observed_at = self._database_mutation_timestamp(session)
            lease_expires_at = self._mutation_lease_expiry(
                observed_at=observed_at,
                lease_seconds=lease_seconds,
            )
            row = session.scalar(
                select(LaunchplaneMergeTrainControllerStateRow)
                .where(LaunchplaneMergeTrainControllerStateRow.controller_key == controller_key)
                .with_for_update()
            )
            if row is None:
                record = build_merge_train_controller_state_record(
                    repository=repository,
                    base_branch=base_branch,
                    policy_key=policy_key,
                    policy_sha256=policy_sha256,
                    updated_at=observed_at,
                )
                leased_record = record.model_copy(
                    update={
                        "status": "running",
                        "lease_owner": lease_owner,
                        "lease_acquired_at": observed_at,
                        "lease_expires_at": lease_expires_at,
                        "heartbeat_at": observed_at,
                        "active_action": normalized_initial_action,
                        "active_phase": normalized_initial_phase,
                    }
                )
                leased_record = MergeTrainControllerStateRecord.model_validate(
                    leased_record.model_dump(mode="json")
                )
                session.add(
                    LaunchplaneMergeTrainControllerStateRow(
                        controller_key=leased_record.controller_key,
                        repository=leased_record.repository,
                        base_branch=leased_record.base_branch,
                        status=leased_record.status,
                        policy_key=leased_record.policy_key,
                        policy_sha256=leased_record.policy_sha256,
                        updated_at=leased_record.updated_at,
                        lease_owner=leased_record.lease_owner,
                        lease_expires_at=leased_record.lease_expires_at,
                        active_action=leased_record.active_action,
                        active_phase=leased_record.active_phase,
                        payload=self._payload_dict(leased_record),
                    )
                )
                session.commit()
                return leased_record
            current_record = self._read_payload(
                model_type=MergeTrainControllerStateRecord,
                payload=row.payload,
            )
            if (
                current_record.status == "running"
                and current_record.lease_expires_at
                and current_record.lease_expires_at > observed_at
            ):
                raise MergeTrainControllerLeaseHeldError(
                    "merge train controller lease is held by another owner"
                )
            adopting = current_record.status == "reconcile_required" or bool(
                current_record.active_action and current_record.active_phase
            )
            if adopting and current_record.active_action not in normalized_adoptable_actions:
                raise MergeTrainControllerAdoptionRejectedError(
                    "merge train controller state belongs to a different active action"
                )
            leased_record = current_record.model_copy(
                update={
                    "policy_key": policy_key,
                    "policy_sha256": policy_sha256,
                    "status": "running",
                    "updated_at": observed_at,
                    "lease_owner": lease_owner,
                    "lease_acquired_at": observed_at,
                    "lease_expires_at": lease_expires_at,
                    "heartbeat_at": observed_at,
                    "active_action": current_record.active_action or normalized_initial_action,
                    "active_phase": current_record.active_phase or normalized_initial_phase,
                    "reconciliation_status": "adopted" if adopting else "clean",
                    "reconciliation_detail": (
                        build_merge_train_controller_resume_detail(current_record)
                        if adopting
                        else ""
                    ),
                }
            )
            leased_record = MergeTrainControllerStateRecord.model_validate(
                leased_record.model_dump(mode="json")
            )
            self._sync_merge_train_controller_state_row(row, leased_record)
            session.commit()
            return leased_record

    def compare_and_set_merge_train_controller_state_record(
        self,
        *,
        record: MergeTrainControllerStateRecord,
        expected_lease_owner: str,
        expected_lease_acquired_at: str,
        lease_seconds: int,
    ) -> MergeTrainControllerStateRecord:
        with self._session_factory() as session:
            self._begin_serialized_write(session)
            self._advisory_lock_merge_train_controller(session, record.controller_key)
            observed_at = self._database_mutation_timestamp(session)
            row = session.scalar(
                select(LaunchplaneMergeTrainControllerStateRow)
                .where(
                    LaunchplaneMergeTrainControllerStateRow.controller_key == record.controller_key
                )
                .with_for_update()
            )
            if row is None:
                raise MergeTrainControllerLeaseLostError("merge train controller state is missing")
            current_record = self._read_payload(
                model_type=MergeTrainControllerStateRecord,
                payload=row.payload,
            )
            if current_record.lease_owner != expected_lease_owner:
                raise MergeTrainControllerLeaseLostError(
                    "merge train controller lease owner changed"
                )
            if current_record.lease_acquired_at != expected_lease_acquired_at:
                raise MergeTrainControllerLeaseLostError(
                    "merge train controller lease token changed"
                )
            if (
                not current_record.lease_expires_at
                or current_record.lease_expires_at <= observed_at
            ):
                raise MergeTrainControllerLeaseLostError("merge train controller lease expired")
            persisted_record = record.model_copy(
                update={
                    "updated_at": observed_at,
                    **(
                        {
                            "heartbeat_at": observed_at,
                            "lease_expires_at": self._mutation_lease_expiry(
                                observed_at=observed_at,
                                lease_seconds=lease_seconds,
                            ),
                        }
                        if record.status == "running"
                        else {"last_transition_at": observed_at}
                    ),
                }
            )
            persisted_record = MergeTrainControllerStateRecord.model_validate(
                persisted_record.model_dump(mode="json")
            )
            self._sync_merge_train_controller_state_row(row, persisted_record)
            session.commit()
            return persisted_record

    def list_merge_train_batch_candidate_records(
        self,
        *,
        repository: str = "",
        base_branch: str = "",
        status: str = "",
        limit: int | None = None,
    ) -> tuple[MergeTrainBatchCandidateRecord, ...]:
        filters: list[object] = []
        if repository:
            filters.append(LaunchplaneMergeTrainBatchCandidateRow.repository == repository)
        if base_branch:
            filters.append(LaunchplaneMergeTrainBatchCandidateRow.base_branch == base_branch)
        if status:
            filters.append(LaunchplaneMergeTrainBatchCandidateRow.status == status)
        return self._list_models(
            model_type=MergeTrainBatchCandidateRecord,
            orm_model=LaunchplaneMergeTrainBatchCandidateRow,
            filters=filters,
            order_by=(
                LaunchplaneMergeTrainBatchCandidateRow.updated_at.desc(),
                LaunchplaneMergeTrainBatchCandidateRow.record_id.desc(),
            ),
            limit=limit,
        )

    def write_merge_train_batch_landing_plan_record(
        self, record: MergeTrainBatchLandingPlanRecord
    ) -> None:
        self._write_row(
            LaunchplaneMergeTrainBatchLandingPlanRow(
                record_id=record.record_id,
                status=record.status,
                source=record.source,
                updated_at=record.updated_at,
                repository=record.landing_plan.repository,
                base_branch=record.landing_plan.base_branch,
                batch_id=record.landing_plan.batch_id,
                plan_id=record.landing_plan.plan_id,
                payload=self._payload_dict(record),
            )
        )

    def list_merge_train_batch_landing_plan_records(
        self,
        *,
        repository: str = "",
        base_branch: str = "",
        status: str = "",
        limit: int | None = None,
    ) -> tuple[MergeTrainBatchLandingPlanRecord, ...]:
        filters: list[object] = []
        if repository:
            filters.append(LaunchplaneMergeTrainBatchLandingPlanRow.repository == repository)
        if base_branch:
            filters.append(LaunchplaneMergeTrainBatchLandingPlanRow.base_branch == base_branch)
        if status:
            filters.append(LaunchplaneMergeTrainBatchLandingPlanRow.status == status)
        return self._list_models(
            model_type=MergeTrainBatchLandingPlanRecord,
            orm_model=LaunchplaneMergeTrainBatchLandingPlanRow,
            filters=filters,
            order_by=(
                LaunchplaneMergeTrainBatchLandingPlanRow.updated_at.desc(),
                LaunchplaneMergeTrainBatchLandingPlanRow.record_id.desc(),
            ),
            limit=limit,
        )

    def create_merge_admission_record_if_absent(
        self, record: MergeAdmissionRecord
    ) -> tuple[MergeAdmissionRecord, bool]:
        with self._session_factory() as session:
            existing_row = session.scalar(
                select(LaunchplaneMergeAdmissionRow).where(
                    LaunchplaneMergeAdmissionRow.attempt_id == record.attempt_id
                )
            )
            if existing_row is not None:
                existing = MergeAdmissionRecord.model_validate(existing_row.payload)
                if existing != record:
                    raise ValueError("Merge admission attempts are append-only.")
                return existing, False
            session.add(
                LaunchplaneMergeAdmissionRow(
                    admission_id=record.admission_id,
                    admission_binding_sha256=record.admission_binding_sha256,
                    attempt_id=record.attempt_id,
                    attempt_sequence=record.attempt_sequence,
                    decision=record.decision,
                    repository=record.repository,
                    base_branch=record.base_branch,
                    pull_request_number=record.pull_request_number,
                    queue_position=record.queue_position,
                    landing_plan_record_id=record.landing_plan_record_id,
                    landing_plan_id=record.landing_plan_id,
                    created_at=record.created_at,
                    payload=self._payload_dict(record),
                )
            )
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                existing_row = session.scalar(
                    select(LaunchplaneMergeAdmissionRow).where(
                        or_(
                            LaunchplaneMergeAdmissionRow.attempt_id == record.attempt_id,
                            LaunchplaneMergeAdmissionRow.admission_id == record.admission_id,
                            LaunchplaneMergeAdmissionRow.admission_binding_sha256
                            == record.admission_binding_sha256,
                        )
                    )
                )
                if existing_row is None:
                    raise
                existing = MergeAdmissionRecord.model_validate(existing_row.payload)
                if existing != record:
                    raise ValueError("Merge admission records are append-only.") from None
                return existing, False
        return record, True

    def create_guarded_merge_admission_record_if_absent(
        self,
        record: MergeAdmissionRecord,
        *,
        admitted_at: str,
    ) -> tuple[MergeAdmissionRecord, bool]:
        with self._session_factory() as session:
            self._advisory_lock_merge_train_controller(session, record.controller_key)
            controller_row = session.scalar(
                select(LaunchplaneMergeTrainControllerStateRow)
                .where(
                    LaunchplaneMergeTrainControllerStateRow.controller_key == record.controller_key
                )
                .with_for_update()
            )
            if controller_row is None:
                raise MergeAdmissionFenceRejectedError(
                    "Persisted merge controller state is missing at admission creation."
                )
            controller_state = MergeTrainControllerStateRecord.model_validate(
                controller_row.payload
            )
            validate_merge_admission_controller_fence(
                admission=record,
                controller_state=controller_state,
                admitted_at=admitted_at,
            )
            existing_row = session.scalar(
                select(LaunchplaneMergeAdmissionRow).where(
                    LaunchplaneMergeAdmissionRow.attempt_id == record.attempt_id
                )
            )
            if existing_row is not None:
                existing = MergeAdmissionRecord.model_validate(existing_row.payload)
                if existing != record:
                    raise ValueError("Merge admission attempts are append-only.")
                return existing, False
            session.add(
                LaunchplaneMergeAdmissionRow(
                    admission_id=record.admission_id,
                    admission_binding_sha256=record.admission_binding_sha256,
                    attempt_id=record.attempt_id,
                    attempt_sequence=record.attempt_sequence,
                    decision=record.decision,
                    repository=record.repository,
                    base_branch=record.base_branch,
                    pull_request_number=record.pull_request_number,
                    queue_position=record.queue_position,
                    landing_plan_record_id=record.landing_plan_record_id,
                    landing_plan_id=record.landing_plan_id,
                    created_at=record.created_at,
                    payload=self._payload_dict(record),
                )
            )
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                existing_row = session.scalar(
                    select(LaunchplaneMergeAdmissionRow).where(
                        or_(
                            LaunchplaneMergeAdmissionRow.attempt_id == record.attempt_id,
                            LaunchplaneMergeAdmissionRow.admission_id == record.admission_id,
                            LaunchplaneMergeAdmissionRow.admission_binding_sha256
                            == record.admission_binding_sha256,
                        )
                    )
                )
                if existing_row is None:
                    raise
                existing = MergeAdmissionRecord.model_validate(existing_row.payload)
                if existing != record:
                    raise ValueError("Merge admission records are append-only.") from None
                return existing, False
        return record, True

    def read_merge_admission_record(self, admission_id: str) -> MergeAdmissionRecord:
        return self._read_model(
            model_type=MergeAdmissionRecord,
            orm_model=LaunchplaneMergeAdmissionRow,
            filters=(LaunchplaneMergeAdmissionRow.admission_id == admission_id,),
        )

    def list_merge_admission_records(
        self,
        *,
        repository: str = "",
        base_branch: str = "",
        pull_request_number: int | None = None,
        landing_plan_record_id: str = "",
        landing_plan_id: str = "",
        attempt_id: str = "",
        limit: int | None = None,
    ) -> tuple[MergeAdmissionRecord, ...]:
        filters: list[object] = []
        if repository:
            filters.append(LaunchplaneMergeAdmissionRow.repository == repository.strip().lower())
        if base_branch:
            filters.append(LaunchplaneMergeAdmissionRow.base_branch == base_branch)
        if pull_request_number is not None:
            filters.append(LaunchplaneMergeAdmissionRow.pull_request_number == pull_request_number)
        if landing_plan_record_id:
            filters.append(
                LaunchplaneMergeAdmissionRow.landing_plan_record_id == landing_plan_record_id
            )
        if landing_plan_id:
            filters.append(LaunchplaneMergeAdmissionRow.landing_plan_id == landing_plan_id)
        if attempt_id:
            filters.append(LaunchplaneMergeAdmissionRow.attempt_id == attempt_id)
        return self._list_models(
            model_type=MergeAdmissionRecord,
            orm_model=LaunchplaneMergeAdmissionRow,
            filters=filters,
            order_by=(
                LaunchplaneMergeAdmissionRow.created_at.desc(),
                LaunchplaneMergeAdmissionRow.admission_id.desc(),
            ),
            limit=limit,
        )

    def create_merge_landing_outcome_record_if_absent(
        self, record: MergeLandingOutcomeRecord
    ) -> tuple[MergeLandingOutcomeRecord, bool]:
        admission = self.read_merge_admission_record(record.admission_id)
        validate_merge_landing_outcome_for_admission(admission=admission, outcome=record)
        prior_records = self.list_merge_landing_outcome_records(
            admission_id=record.admission_id,
            limit=1,
        )
        if prior_records:
            prior = prior_records[0]
            if prior.observation_sequence == record.observation_sequence:
                if prior != record:
                    raise ValueError("Merge landing outcome observations are append-only.")
                return prior, False
            validate_merge_landing_outcome_successor(prior=prior, successor=record)
        elif record.observation_sequence != 1:
            raise ValueError("Merge landing outcome reconciliation is missing its predecessor.")
        with self._session_factory() as session:
            session.add(
                LaunchplaneMergeLandingOutcomeRow(
                    outcome_id=record.outcome_id,
                    outcome_binding_sha256=record.outcome_binding_sha256,
                    admission_id=record.admission_id,
                    attempt_id=record.attempt_id,
                    observation_sequence=record.observation_sequence,
                    status=record.status,
                    repository=record.repository,
                    base_branch=record.base_branch,
                    pull_request_number=record.pull_request_number,
                    observed_at=record.observed_at,
                    payload=self._payload_dict(record),
                )
            )
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                existing_row = session.scalar(
                    select(LaunchplaneMergeLandingOutcomeRow).where(
                        or_(
                            LaunchplaneMergeLandingOutcomeRow.outcome_id == record.outcome_id,
                            (LaunchplaneMergeLandingOutcomeRow.admission_id == record.admission_id)
                            & (
                                LaunchplaneMergeLandingOutcomeRow.observation_sequence
                                == record.observation_sequence
                            ),
                        )
                    )
                )
                if existing_row is None:
                    raise
                existing = MergeLandingOutcomeRecord.model_validate(existing_row.payload)
                if existing != record:
                    raise ValueError("Merge landing outcome records are append-only.") from None
                return existing, False
        return record, True

    def read_merge_landing_outcome_record(self, outcome_id: str) -> MergeLandingOutcomeRecord:
        return self._read_model(
            model_type=MergeLandingOutcomeRecord,
            orm_model=LaunchplaneMergeLandingOutcomeRow,
            filters=(LaunchplaneMergeLandingOutcomeRow.outcome_id == outcome_id,),
        )

    def list_merge_landing_outcome_records(
        self,
        *,
        repository: str = "",
        base_branch: str = "",
        pull_request_number: int | None = None,
        admission_id: str = "",
        status: str = "",
        observation_sequence: int | None = None,
        limit: int | None = None,
    ) -> tuple[MergeLandingOutcomeRecord, ...]:
        filters: list[object] = []
        if repository:
            filters.append(
                LaunchplaneMergeLandingOutcomeRow.repository == repository.strip().lower()
            )
        if base_branch:
            filters.append(LaunchplaneMergeLandingOutcomeRow.base_branch == base_branch)
        if pull_request_number is not None:
            filters.append(
                LaunchplaneMergeLandingOutcomeRow.pull_request_number == pull_request_number
            )
        if admission_id:
            filters.append(LaunchplaneMergeLandingOutcomeRow.admission_id == admission_id)
        if status:
            filters.append(LaunchplaneMergeLandingOutcomeRow.status == status)
        if observation_sequence is not None:
            filters.append(
                LaunchplaneMergeLandingOutcomeRow.observation_sequence == observation_sequence
            )
        return self._list_models(
            model_type=MergeLandingOutcomeRecord,
            orm_model=LaunchplaneMergeLandingOutcomeRow,
            filters=filters,
            order_by=(
                LaunchplaneMergeLandingOutcomeRow.admission_id.desc(),
                LaunchplaneMergeLandingOutcomeRow.observation_sequence.desc(),
                LaunchplaneMergeLandingOutcomeRow.observed_at.desc(),
                LaunchplaneMergeLandingOutcomeRow.outcome_id.desc(),
            ),
            limit=limit,
        )

    def list_unresolved_merge_admission_records(
        self,
        *,
        repository: str = "",
        base_branch: str = "",
        limit: int | None = None,
    ) -> tuple[MergeAdmissionRecord, ...]:
        admissions = self.list_merge_admission_records(
            repository=repository,
            base_branch=base_branch,
        )
        unresolved = tuple(
            admission
            for admission in admissions
            if (
                not (
                    outcomes := self.list_merge_landing_outcome_records(
                        admission_id=admission.admission_id,
                        limit=1,
                    )
                )
                or outcomes[0].status == "reconcile_required"
            )
        )
        return unresolved[:limit] if limit is not None else unresolved

    def write_merge_train_stack_collapse_plan_record(
        self, record: MergeTrainStackCollapsePlanRecord
    ) -> None:
        self._write_row(
            LaunchplaneMergeTrainStackCollapsePlanRow(
                record_id=record.record_id,
                status=record.status,
                source=record.source,
                updated_at=record.updated_at,
                repository=record.plan.repository,
                base_branch=record.plan.base_branch,
                collapse_id=record.plan.collapse_id,
                root_pull_request_number=record.plan.root_pull_request_number,
                plan_status=record.plan.status,
                payload=self._payload_dict(record),
            )
        )

    def list_merge_train_stack_collapse_plan_records(
        self,
        *,
        repository: str = "",
        base_branch: str = "",
        status: str = "",
        limit: int | None = None,
    ) -> tuple[MergeTrainStackCollapsePlanRecord, ...]:
        filters: list[object] = []
        if repository:
            filters.append(LaunchplaneMergeTrainStackCollapsePlanRow.repository == repository)
        if base_branch:
            filters.append(LaunchplaneMergeTrainStackCollapsePlanRow.base_branch == base_branch)
        if status:
            filters.append(LaunchplaneMergeTrainStackCollapsePlanRow.status == status)
        return self._list_models(
            model_type=MergeTrainStackCollapsePlanRecord,
            orm_model=LaunchplaneMergeTrainStackCollapsePlanRow,
            filters=filters,
            order_by=(
                LaunchplaneMergeTrainStackCollapsePlanRow.updated_at.desc(),
                LaunchplaneMergeTrainStackCollapsePlanRow.record_id.desc(),
            ),
            limit=limit,
        )

    def list_merge_train_policy_records(
        self,
        *,
        status: str = "",
        limit: int | None = None,
    ) -> tuple[MergeTrainPolicyRecord, ...]:
        filters: list[object] = []
        if status:
            filters.append(LaunchplaneMergeTrainPolicyRow.status == status)
        records = self._list_models(
            model_type=MergeTrainPolicyRecord,
            orm_model=LaunchplaneMergeTrainPolicyRow,
            filters=filters,
            order_by=(
                LaunchplaneMergeTrainPolicyRow.updated_at.desc(),
                LaunchplaneMergeTrainPolicyRow.record_id.desc(),
            ),
        )
        if limit is not None:
            return records[:limit]
        return records

    def read_merge_train_run_record(self, run_id: str) -> MergeTrainRunRecord:
        return self._read_model(
            model_type=MergeTrainRunRecord,
            orm_model=LaunchplaneMergeTrainRunRow,
            filters=(LaunchplaneMergeTrainRunRow.run_id == run_id,),
        )

    def latest_merge_train_run_record(
        self, *, repository: str, base_branch: str
    ) -> MergeTrainRunRecord | None:
        records = self.list_merge_train_run_records(
            repository=repository,
            base_branch=base_branch,
            limit=1,
        )
        return records[0] if records else None

    def list_merge_train_run_records(
        self,
        *,
        repository: str = "",
        base_branch: str = "",
        mode: str = "",
        status: str = "",
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[MergeTrainRunRecord, ...]:
        filters: list[object] = []
        if repository:
            filters.append(LaunchplaneMergeTrainRunRow.repository == repository)
        if base_branch:
            filters.append(LaunchplaneMergeTrainRunRow.base_branch == base_branch)
        if mode:
            filters.append(LaunchplaneMergeTrainRunRow.mode == mode)
        if status:
            filters.append(LaunchplaneMergeTrainRunRow.status == status)
        return self._list_models(
            model_type=MergeTrainRunRecord,
            orm_model=LaunchplaneMergeTrainRunRow,
            filters=filters,
            order_by=(
                LaunchplaneMergeTrainRunRow.recorded_at.desc(),
                LaunchplaneMergeTrainRunRow.run_id.desc(),
            ),
            limit=limit,
            offset=offset,
        )

    def write_every_code_preview_gate_record(self, record: EveryCodePreviewGateRecord) -> None:
        self._write_row(
            LaunchplaneEveryCodePreviewGateRow(
                gate_id=record.gate_id,
                request_id=record.request_id,
                repository=record.repository,
                issue_number=record.issue_number,
                pr_number=record.pr_number,
                head_sha=record.head_sha,
                status=record.status,
                updated_at=record.updated_at,
                payload=self._payload_dict(record),
            )
        )

    def list_every_code_preview_gate_records(
        self,
        *,
        request_id: str = "",
        repository: str = "",
        pr_number: int | None = None,
        status: str = "",
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[EveryCodePreviewGateRecord, ...]:
        filters: list[object] = []
        if request_id:
            filters.append(LaunchplaneEveryCodePreviewGateRow.request_id == request_id)
        if repository:
            filters.append(LaunchplaneEveryCodePreviewGateRow.repository == repository)
        if pr_number is not None:
            filters.append(LaunchplaneEveryCodePreviewGateRow.pr_number == pr_number)
        if status:
            filters.append(LaunchplaneEveryCodePreviewGateRow.status == status)
        return self._list_models(
            model_type=EveryCodePreviewGateRecord,
            orm_model=LaunchplaneEveryCodePreviewGateRow,
            filters=filters,
            order_by=(
                LaunchplaneEveryCodePreviewGateRow.updated_at.desc(),
                LaunchplaneEveryCodePreviewGateRow.gate_id.desc(),
            ),
            limit=limit,
            offset=offset,
        )

    def list_every_code_pr_feedback_records(
        self,
        *,
        request_id: str = "",
        repository: str = "",
        pr_number: int | None = None,
        status: str = "",
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[EveryCodePrFeedbackRecord, ...]:
        filters: list[object] = []
        if request_id:
            filters.append(LaunchplaneEveryCodePrFeedbackRow.request_id == request_id)
        if repository:
            filters.append(LaunchplaneEveryCodePrFeedbackRow.repository == repository)
        if pr_number is not None:
            filters.append(LaunchplaneEveryCodePrFeedbackRow.pr_number == pr_number)
        if status:
            filters.append(LaunchplaneEveryCodePrFeedbackRow.status == status)
        return self._list_models(
            model_type=EveryCodePrFeedbackRecord,
            orm_model=LaunchplaneEveryCodePrFeedbackRow,
            filters=filters,
            order_by=(
                LaunchplaneEveryCodePrFeedbackRow.received_at.desc(),
                LaunchplaneEveryCodePrFeedbackRow.feedback_id.desc(),
            ),
            limit=limit,
            offset=offset,
        )

    def write_preview_lifecycle_plan_record(self, record: PreviewLifecyclePlanRecord) -> None:
        self._write_row(
            LaunchplanePreviewLifecyclePlanRow(
                plan_id=record.plan_id,
                product=record.product,
                context=record.context,
                planned_at=record.planned_at,
                status=record.status,
                inventory_scan_id=record.inventory_scan_id,
                payload=self._payload_dict(record),
            )
        )

    def list_preview_lifecycle_plan_records(
        self,
        *,
        context_name: str = "",
        limit: int | None = None,
    ) -> tuple[PreviewLifecyclePlanRecord, ...]:
        filters: list[object] = []
        if context_name:
            filters.append(LaunchplanePreviewLifecyclePlanRow.context == context_name)
        return self._list_models(
            model_type=PreviewLifecyclePlanRecord,
            orm_model=LaunchplanePreviewLifecyclePlanRow,
            filters=filters,
            order_by=(
                LaunchplanePreviewLifecyclePlanRow.planned_at.desc(),
                LaunchplanePreviewLifecyclePlanRow.plan_id.desc(),
            ),
            limit=limit,
        )

    def write_preview_lifecycle_cleanup_record(self, record: PreviewLifecycleCleanupRecord) -> None:
        self._write_row(
            LaunchplanePreviewLifecycleCleanupRow(
                cleanup_id=record.cleanup_id,
                product=record.product,
                context=record.context,
                plan_id=record.plan_id,
                requested_at=record.requested_at,
                status=record.status,
                payload=self._payload_dict(record),
            )
        )

    def list_preview_lifecycle_cleanup_records(
        self,
        *,
        context_name: str = "",
        limit: int | None = None,
    ) -> tuple[PreviewLifecycleCleanupRecord, ...]:
        filters: list[object] = []
        if context_name:
            filters.append(LaunchplanePreviewLifecycleCleanupRow.context == context_name)
        return self._list_models(
            model_type=PreviewLifecycleCleanupRecord,
            orm_model=LaunchplanePreviewLifecycleCleanupRow,
            filters=filters,
            order_by=(
                LaunchplanePreviewLifecycleCleanupRow.requested_at.desc(),
                LaunchplanePreviewLifecycleCleanupRow.cleanup_id.desc(),
            ),
            limit=limit,
        )

    def write_preview_pr_feedback_record(self, record: PreviewPrFeedbackRecord) -> None:
        self._write_row(
            LaunchplanePreviewPrFeedbackRow(
                feedback_id=record.feedback_id,
                product=record.product,
                context=record.context,
                anchor_repo=record.anchor_repo,
                anchor_pr_number=record.anchor_pr_number,
                requested_at=record.requested_at,
                status=record.status,
                delivery_status=record.delivery_status,
                payload=self._payload_dict(record),
            )
        )

    def list_preview_pr_feedback_records(
        self,
        *,
        context_name: str = "",
        limit: int | None = None,
    ) -> tuple[PreviewPrFeedbackRecord, ...]:
        filters: list[object] = []
        if context_name:
            filters.append(LaunchplanePreviewPrFeedbackRow.context == context_name)
        return self._list_models(
            model_type=PreviewPrFeedbackRecord,
            orm_model=LaunchplanePreviewPrFeedbackRow,
            filters=filters,
            order_by=(
                LaunchplanePreviewPrFeedbackRow.requested_at.desc(),
                LaunchplanePreviewPrFeedbackRow.feedback_id.desc(),
            ),
            limit=limit,
        )

    def write_preview_pr_feedback_remediation_record(
        self, record: PreviewPrFeedbackRemediationRecord
    ) -> None:
        self._write_row(
            LaunchplanePreviewPrFeedbackRemediationRow(
                remediation_id=record.remediation_id,
                product=record.product,
                context=record.context,
                repository=record.repository,
                pull_request_number=record.pull_request_number,
                actor=record.actor,
                idempotency_key=record.idempotency_key,
                requested_at=record.requested_at,
                mode=record.mode,
                outcome=record.outcome,
                payload=self._payload_dict(record),
            )
        )

    def list_preview_pr_feedback_remediation_records(
        self,
        *,
        actor: str = "",
        idempotency_key: str = "",
        mode: str = "",
        limit: int | None = None,
    ) -> tuple[PreviewPrFeedbackRemediationRecord, ...]:
        filters: list[object] = []
        if actor:
            filters.append(LaunchplanePreviewPrFeedbackRemediationRow.actor == actor)
        if idempotency_key:
            filters.append(
                LaunchplanePreviewPrFeedbackRemediationRow.idempotency_key == idempotency_key
            )
        if mode:
            filters.append(LaunchplanePreviewPrFeedbackRemediationRow.mode == mode)
        return self._list_models(
            model_type=PreviewPrFeedbackRemediationRecord,
            orm_model=LaunchplanePreviewPrFeedbackRemediationRow,
            filters=filters,
            order_by=(
                LaunchplanePreviewPrFeedbackRemediationRow.requested_at.desc(),
                LaunchplanePreviewPrFeedbackRemediationRow.remediation_id.desc(),
            ),
            limit=limit,
        )

    def write_preview_pr_feedback_remediation_bundle(
        self,
        *,
        remediation_record: PreviewPrFeedbackRemediationRecord,
        feedback_record: PreviewPrFeedbackRecord,
        idempotency_record: LaunchplaneIdempotencyRecord,
    ) -> None:
        with self._session_factory() as session:
            session.merge(
                LaunchplanePreviewPrFeedbackRemediationRow(
                    remediation_id=remediation_record.remediation_id,
                    product=remediation_record.product,
                    context=remediation_record.context,
                    repository=remediation_record.repository,
                    pull_request_number=remediation_record.pull_request_number,
                    actor=remediation_record.actor,
                    idempotency_key=remediation_record.idempotency_key,
                    requested_at=remediation_record.requested_at,
                    mode=remediation_record.mode,
                    outcome=remediation_record.outcome,
                    payload=self._payload_dict(remediation_record),
                )
            )
            session.merge(
                LaunchplanePreviewPrFeedbackRow(
                    feedback_id=feedback_record.feedback_id,
                    product=feedback_record.product,
                    context=feedback_record.context,
                    anchor_repo=feedback_record.anchor_repo,
                    anchor_pr_number=feedback_record.anchor_pr_number,
                    requested_at=feedback_record.requested_at,
                    status=feedback_record.status,
                    delivery_status=feedback_record.delivery_status,
                    payload=self._payload_dict(feedback_record),
                )
            )
            session.merge(self._idempotency_row(idempotency_record))
            session.commit()

    def write_preview_pr_feedback_notification_policy_record(
        self, record: PreviewPrFeedbackNotificationPolicyRecord
    ) -> None:
        self._write_row(
            LaunchplanePreviewPrFeedbackNotificationPolicyRow(
                policy_id=record.policy_id,
                product=record.product,
                context=record.context,
                repository=record.repository,
                status=record.status,
                updated_at=record.updated_at,
                payload=self._payload_dict(record),
            )
        )

    def list_preview_pr_feedback_notification_policy_records(
        self,
        *,
        product: str = "",
        context_name: str = "",
        repository: str = "",
        status: str = "",
        limit: int | None = None,
    ) -> tuple[PreviewPrFeedbackNotificationPolicyRecord, ...]:
        filters: list[object] = []
        if product:
            filters.append(
                LaunchplanePreviewPrFeedbackNotificationPolicyRow.product.in_(("", product))
            )
        if context_name:
            filters.append(
                LaunchplanePreviewPrFeedbackNotificationPolicyRow.context.in_(("", context_name))
            )
        if repository:
            filters.append(
                LaunchplanePreviewPrFeedbackNotificationPolicyRow.repository.in_(("", repository))
            )
        if status:
            filters.append(LaunchplanePreviewPrFeedbackNotificationPolicyRow.status == status)
        return self._list_models(
            model_type=PreviewPrFeedbackNotificationPolicyRecord,
            orm_model=LaunchplanePreviewPrFeedbackNotificationPolicyRow,
            filters=filters,
            order_by=(
                LaunchplanePreviewPrFeedbackNotificationPolicyRow.updated_at.desc(),
                LaunchplanePreviewPrFeedbackNotificationPolicyRow.policy_id.desc(),
            ),
            limit=limit,
        )

    def write_preview_pr_feedback_notification_attempt_record(
        self, record: PreviewPrFeedbackNotificationAttemptRecord
    ) -> None:
        self._write_row(
            LaunchplanePreviewPrFeedbackNotificationAttemptRow(
                attempt_id=record.attempt_id,
                feedback_id=record.feedback_id,
                event=record.event,
                destination_kind=record.destination_kind,
                delivery_status=record.delivery_status,
                attempted_at=record.attempted_at,
                payload=self._payload_dict(record),
            )
        )

    def list_preview_pr_feedback_notification_attempt_records(
        self,
        *,
        feedback_id: str = "",
        event: str = "",
        destination_kind: str = "",
        limit: int | None = None,
    ) -> tuple[PreviewPrFeedbackNotificationAttemptRecord, ...]:
        filters: list[object] = []
        if feedback_id:
            filters.append(
                LaunchplanePreviewPrFeedbackNotificationAttemptRow.feedback_id == feedback_id
            )
        if event:
            filters.append(LaunchplanePreviewPrFeedbackNotificationAttemptRow.event == event)
        if destination_kind:
            filters.append(
                LaunchplanePreviewPrFeedbackNotificationAttemptRow.destination_kind
                == destination_kind
            )
        return self._list_models(
            model_type=PreviewPrFeedbackNotificationAttemptRecord,
            orm_model=LaunchplanePreviewPrFeedbackNotificationAttemptRow,
            filters=filters,
            order_by=(
                LaunchplanePreviewPrFeedbackNotificationAttemptRow.attempted_at.desc(),
                LaunchplanePreviewPrFeedbackNotificationAttemptRow.attempt_id.desc(),
            ),
            limit=limit,
        )

    def write_runner_host_hygiene_audit_record(
        self, record: RunnerHostHygieneApplyAuditRecord
    ) -> None:
        persisted_record = sanitize_runner_host_hygiene_audit_record_for_persistence(record)
        self._write_row(
            LaunchplaneRunnerHostHygieneAuditRow(
                audit_record_key=persisted_record.audit_record_key,
                host_name=persisted_record.request.host_name,
                action=persisted_record.request.action,
                status=persisted_record.status,
                mutate=int(persisted_record.request.mutate),
                payload=self._payload_dict(persisted_record),
            )
        )

    def list_runner_host_hygiene_audit_records(
        self,
        *,
        host_name: str = "",
        action: str = "",
        status: str = "",
        limit: int | None = None,
    ) -> tuple[RunnerHostHygieneApplyAuditRecord, ...]:
        filters: list[object] = []
        normalized_host_name = host_name.strip().lower()
        if normalized_host_name:
            filters.append(LaunchplaneRunnerHostHygieneAuditRow.host_name == normalized_host_name)
        if action:
            filters.append(LaunchplaneRunnerHostHygieneAuditRow.action == action)
        if status:
            filters.append(LaunchplaneRunnerHostHygieneAuditRow.status == status)
        return self._list_models(
            model_type=RunnerHostHygieneApplyAuditRecord,
            orm_model=LaunchplaneRunnerHostHygieneAuditRow,
            filters=filters,
            order_by=(LaunchplaneRunnerHostHygieneAuditRow.audit_record_key.desc(),),
            limit=limit,
        )

    def read_runner_host_hygiene_audit_record(
        self, audit_record_key: str
    ) -> RunnerHostHygieneApplyAuditRecord:
        return self._read_model(
            model_type=RunnerHostHygieneApplyAuditRecord,
            orm_model=LaunchplaneRunnerHostHygieneAuditRow,
            filters=(LaunchplaneRunnerHostHygieneAuditRow.audit_record_key == audit_record_key,),
        )

    def write_runner_lane_registration_audit_record(
        self, record: RunnerLaneRegistrationAuditRecord
    ) -> None:
        self._write_row(
            LaunchplaneRunnerLaneRegistrationAuditRow(
                audit_record_key=record.audit_record_key,
                repository=record.request.repository.strip().lower(),
                host_name=record.request.host_name.strip().lower(),
                lane_name=record.request.lane_name.strip().lower(),
                status=record.status,
                mutate=int(record.request.mutate),
                payload=self._payload_dict(record),
            )
        )

    def list_runner_lane_registration_audit_records(
        self,
        *,
        repository: str = "",
        host_name: str = "",
        status: str = "",
        limit: int | None = None,
    ) -> tuple[RunnerLaneRegistrationAuditRecord, ...]:
        filters: list[object] = []
        normalized_repository = repository.strip().lower()
        normalized_host_name = host_name.strip().lower()
        if normalized_repository:
            filters.append(
                LaunchplaneRunnerLaneRegistrationAuditRow.repository == normalized_repository
            )
        if normalized_host_name:
            filters.append(
                LaunchplaneRunnerLaneRegistrationAuditRow.host_name == normalized_host_name
            )
        if status:
            filters.append(LaunchplaneRunnerLaneRegistrationAuditRow.status == status)
        return self._list_models(
            model_type=RunnerLaneRegistrationAuditRecord,
            orm_model=LaunchplaneRunnerLaneRegistrationAuditRow,
            filters=filters,
            order_by=(LaunchplaneRunnerLaneRegistrationAuditRow.audit_record_key.desc(),),
            limit=limit,
        )

    def read_preview_summary(
        self,
        *,
        preview_id: str,
        generation_limit: int | None = 10,
    ) -> LaunchplanePreviewSummary:
        preview = self.read_preview_record(preview_id)
        recent_generations = self.list_preview_generation_records(
            preview_id=preview_id,
            limit=generation_limit,
        )
        return LaunchplanePreviewSummary(
            preview=preview,
            latest_generation=next(iter(recent_generations), None),
            recent_generations=recent_generations,
        )

    def list_preview_summaries(
        self,
        *,
        context_name: str = "",
        anchor_repo: str = "",
        anchor_pr_number: int | None = None,
        preview_limit: int | None = None,
        generation_limit: int | None = 1,
    ) -> tuple[LaunchplanePreviewSummary, ...]:
        previews = self.list_preview_records(
            context_name=context_name,
            anchor_repo=anchor_repo,
            anchor_pr_number=anchor_pr_number,
            limit=preview_limit,
        )
        return tuple(
            self.read_preview_summary(
                preview_id=preview.preview_id,
                generation_limit=generation_limit,
            )
            for preview in previews
        )

    def write_release_tuple_record(self, record: ReleaseTupleRecord) -> None:
        self._write_row(self._release_tuple_row(record))

    def read_release_tuple_record(
        self, *, context_name: str, channel_name: str
    ) -> ReleaseTupleRecord:
        return self._read_model(
            model_type=ReleaseTupleRecord,
            orm_model=LaunchplaneReleaseTupleRow,
            filters=(
                LaunchplaneReleaseTupleRow.context == context_name,
                LaunchplaneReleaseTupleRow.channel == channel_name,
            ),
        )

    def list_release_tuple_records(self) -> tuple[ReleaseTupleRecord, ...]:
        return self._list_models(
            model_type=ReleaseTupleRecord,
            orm_model=LaunchplaneReleaseTupleRow,
            order_by=(
                LaunchplaneReleaseTupleRow.context.asc(),
                LaunchplaneReleaseTupleRow.channel.asc(),
            ),
        )

    def _lock_active_authz_policy(self, session: Any) -> None:
        if self.database_url.startswith("sqlite"):
            return
        session.execute(
            text("select pg_advisory_xact_lock(hashtextextended(:lock_name, 0))"),
            {"lock_name": "launchplane:active-authz-policy"},
        )

    @staticmethod
    def _authz_policy_payload(record: LaunchplaneAuthzPolicyRecord) -> PayloadDict:
        payload = record.model_dump(mode="json")
        payload.pop("revision", None)
        return payload

    @classmethod
    def _authz_policy_row(cls, record: LaunchplaneAuthzPolicyRecord) -> LaunchplaneAuthzPolicyRow:
        return LaunchplaneAuthzPolicyRow(
            record_id=record.record_id,
            revision=record.revision,
            status=record.status,
            source=record.source,
            updated_at=record.updated_at,
            policy_sha256=record.policy_sha256,
            payload=cls._authz_policy_payload(record),
        )

    @staticmethod
    def _read_authz_policy_row(row: LaunchplaneAuthzPolicyRow) -> LaunchplaneAuthzPolicyRecord:
        return LaunchplaneAuthzPolicyRecord.model_validate(
            {**row.payload, "revision": row.revision}
        )

    def seed_authz_policy_if_absent(
        self, record: LaunchplaneAuthzPolicyRecord
    ) -> LaunchplaneAuthzPolicyRecord:
        if record.status != "active":
            raise ValueError("Authz policy seed record must be active.")
        with self._session_factory() as session:
            self._begin_serialized_write(session)
            self._lock_active_authz_policy(session)
            active_rows = tuple(
                session.scalars(
                    select(LaunchplaneAuthzPolicyRow)
                    .where(LaunchplaneAuthzPolicyRow.status == "active")
                    .order_by(desc(LaunchplaneAuthzPolicyRow.revision))
                ).all()
            )
            if len(active_rows) > 1:
                session.rollback()
                raise ValueError("Multiple active Launchplane authz policy records found.")
            if active_rows:
                session.rollback()
                return self._read_authz_policy_row(active_rows[0])
            latest_revision = session.scalar(select(func.max(LaunchplaneAuthzPolicyRow.revision)))
            revision = int(latest_revision or 0) + 1
            seeded_record = record.model_copy(
                update={
                    "revision": revision,
                    "record_id": build_authz_policy_record_id(
                        revision=revision,
                        policy_sha256=record.policy_sha256,
                    ),
                }
            )
            session.add(self._authz_policy_row(seeded_record))
            session.commit()
            return seeded_record

    def compare_and_write_authz_policy_record(
        self,
        *,
        expected_record: LaunchplaneAuthzPolicyRecord,
        replacement_record: LaunchplaneAuthzPolicyRecord | None,
        mutation: DbOnlyMutationRequest | None = None,
        confirmation_consumption: SoloAdministrationConfirmationConsumptionBinding | None = None,
    ) -> AuthzPolicyCompareWriteResult:
        if confirmation_consumption is None and mutation is not None:
            confirmation_consumption = mutation.confirmation_consumption
        if expected_record.status != "active":
            raise ValueError("Authz policy compare-and-write expected record must be active.")
        if replacement_record is not None and replacement_record.status != "active":
            raise ValueError("Authz policy compare-and-write replacement must be active.")
        if (
            replacement_record is not None
            and replacement_record.revision != expected_record.revision + 1
        ):
            raise ValueError(
                "Authz policy compare-and-write replacement revision must follow the expected record."
            )
        if mutation is not None:
            if not 100 <= mutation.response_status_code <= 599:
                raise ValueError("DB-only mutation response status must be between 100 and 599.")
            if not mutation.response_trace_id.strip():
                raise ValueError("DB-only mutation response trace id is required.")
        if confirmation_consumption is not None and mutation is None:
            raise ValueError("Confirmation consumption requires idempotency completion evidence.")
        statement = (
            select(LaunchplaneAuthzPolicyRow)
            .where(LaunchplaneAuthzPolicyRow.status == "active")
            .order_by(desc(LaunchplaneAuthzPolicyRow.revision))
        )
        if not self.database_url.startswith("sqlite"):
            statement = statement.with_for_update()
        if mutation is None:
            with self._session_factory() as session:
                self._begin_serialized_write(session)
                return self._compare_and_write_authz_policy_locked(
                    session=session,
                    statement=statement,
                    expected_record=expected_record,
                    replacement_record=replacement_record,
                    confirmation_consumption=confirmation_consumption,
                )

        reservation_insert_error: IntegrityError | None = None
        with self._session_factory() as session:
            self._begin_serialized_write(session)
            observed_at = self._database_mutation_timestamp(session)
            stored_reservation = build_launchplane_mutation_reservation(
                scope=mutation.scope,
                route_path=mutation.route_path,
                idempotency_key=mutation.idempotency_key,
                request_fingerprint=mutation.request_fingerprint,
                lease_owner=mutation.lease_owner,
                lease_expires_at=self._mutation_lease_expiry(
                    observed_at=observed_at,
                    lease_seconds=mutation.lease_seconds,
                ),
                reserved_at=observed_at,
            )
            reservation_row = self._idempotency_row(stored_reservation)
            session.add(reservation_row)
            try:
                session.flush()
            except IntegrityError as error:
                session.rollback()
                reservation_insert_error = error
            if reservation_insert_error is None:
                return self._compare_and_write_authz_policy_locked(
                    session=session,
                    statement=statement,
                    expected_record=expected_record,
                    replacement_record=replacement_record,
                    reservation_row=reservation_row,
                    mutation_reservation=stored_reservation,
                    mutation=mutation,
                    confirmation_consumption=confirmation_consumption,
                )

        with self._session_factory() as session:
            self._begin_serialized_write(session)
            reservation_row = session.scalar(
                self._idempotency_statement(
                    scope=mutation.scope,
                    route_path=mutation.route_path,
                    idempotency_key=mutation.idempotency_key,
                    for_update=True,
                )
            )
            if reservation_row is None:
                assert reservation_insert_error is not None
                raise reservation_insert_error
            current_reservation = self._read_payload(
                model_type=LaunchplaneIdempotencyRecord,
                payload=reservation_row.payload,
            )
            if current_reservation.request_fingerprint != mutation.request_fingerprint:
                return AuthzPolicyCompareWriteResult(
                    status="idempotency_conflict",
                    idempotency_record=current_reservation,
                )
            if current_reservation.state == "completed":
                return AuthzPolicyCompareWriteResult(
                    status="replayed",
                    idempotency_record=current_reservation,
                )
            if current_reservation.state == "reconcile_required":
                return AuthzPolicyCompareWriteResult(
                    status="reconciliation_required",
                    idempotency_record=current_reservation,
                )
            observed_at = self._database_mutation_timestamp(session)
            if parse_launchplane_mutation_timestamp(
                current_reservation.lease_expires_at,
                field_name="lease_expires_at",
            ) > parse_launchplane_mutation_timestamp(
                observed_at,
                field_name="observed_at",
            ):
                return AuthzPolicyCompareWriteResult(
                    status="reservation_in_progress",
                    idempotency_record=current_reservation,
                )
            if current_reservation.reconciliation_key:
                reconcile_record = self._updated_idempotency_record(
                    current_reservation,
                    state="reconcile_required",
                    updated_at=observed_at,
                )
                self._sync_idempotency_row(reservation_row, reconcile_record)
                session.commit()
                return AuthzPolicyCompareWriteResult(
                    status="reconciliation_required",
                    idempotency_record=reconcile_record,
                )
            reclaimed_reservation = self._updated_idempotency_record(
                current_reservation,
                lease_owner=mutation.lease_owner,
                lease_expires_at=self._mutation_lease_expiry(
                    observed_at=observed_at,
                    lease_seconds=mutation.lease_seconds,
                ),
                attempt=current_reservation.attempt + 1,
                updated_at=observed_at,
                response_status_code=None,
                response_trace_id="",
                recorded_at="",
                response_payload={},
            )
            self._sync_idempotency_row(reservation_row, reclaimed_reservation)
            return self._compare_and_write_authz_policy_locked(
                session=session,
                statement=statement,
                expected_record=expected_record,
                replacement_record=replacement_record,
                confirmation_consumption=confirmation_consumption,
                reservation_row=reservation_row,
                mutation_reservation=reclaimed_reservation,
                mutation=mutation,
            )

    def _compare_and_write_authz_policy_locked(
        self,
        *,
        session: Any,
        statement: Any,
        expected_record: LaunchplaneAuthzPolicyRecord,
        replacement_record: LaunchplaneAuthzPolicyRecord | None,
        reservation_row: LaunchplaneIdempotencyRow | None = None,
        mutation_reservation: LaunchplaneIdempotencyRecord | None = None,
        mutation: DbOnlyMutationRequest | None = None,
        confirmation_consumption: SoloAdministrationConfirmationConsumptionBinding | None = None,
    ) -> AuthzPolicyCompareWriteResult:
        self._lock_active_authz_policy(session)
        active_rows = tuple(session.scalars(statement).all())
        if not active_rows:
            if reservation_row is not None:
                session.delete(reservation_row)
                session.commit()
            return AuthzPolicyCompareWriteResult(status="missing")
        if len(active_rows) > 1:
            if reservation_row is not None:
                session.delete(reservation_row)
                session.commit()
            return AuthzPolicyCompareWriteResult(status="ambiguous_active")
        active_row = active_rows[0]
        current_record = self._read_authz_policy_row(active_row)
        if (
            current_record.record_id != expected_record.record_id
            or current_record.revision != expected_record.revision
            or current_record.policy_sha256 != expected_record.policy_sha256
        ):
            if reservation_row is not None:
                session.delete(reservation_row)
                session.commit()
            return AuthzPolicyCompareWriteResult(status="stale", current_record=current_record)

        if confirmation_consumption is not None:
            if replacement_record is None:
                raise ValueError(
                    "Confirmation consumption requires a replacement authz policy record."
                )
            confirmation_statement = (
                select(LaunchplaneSoloAdministrationConfirmationRow)
                .where(
                    LaunchplaneSoloAdministrationConfirmationRow.confirmation_id
                    == confirmation_consumption.confirmation_id
                )
                .limit(1)
            )
            if not self.database_url.startswith("sqlite"):
                confirmation_statement = confirmation_statement.with_for_update()
            confirmation_row = session.scalar(confirmation_statement)
            if confirmation_row is None:
                raise FileNotFoundError(confirmation_consumption.confirmation_id)
            confirmation_record = SoloAdministrationConfirmationRecord.model_validate(
                confirmation_row.payload
            )
            consumed_record = consume_solo_administration_confirmation(
                confirmation_record,
                active_policy_record_id=confirmation_consumption.active_policy_record_id,
                active_policy_revision=confirmation_consumption.active_policy_revision,
                active_policy_sha256=confirmation_consumption.active_policy_sha256,
                candidate_policy_sha256=confirmation_consumption.candidate_policy_sha256,
                candidate_administrator_quorum=confirmation_consumption.candidate_administrator_quorum,
                candidate_distinct_human_administrator_count=(
                    confirmation_consumption.candidate_distinct_human_administrator_count
                ),
                reviewed_plan_sha256=confirmation_consumption.reviewed_plan_sha256,
                human_session_id_sha256=confirmation_consumption.human_session_id_sha256,
                github_id=confirmation_consumption.github_id,
                idempotency_scope_sha256=confirmation_consumption.idempotency_scope_sha256,
                idempotency_key_sha256=confirmation_consumption.idempotency_key_sha256,
                acknowledgement_sha256=confirmation_consumption.acknowledgement_sha256,
                secret_sha256=confirmation_consumption.secret_sha256,
                terminal_at=self._database_mutation_timestamp(session),
            )
            confirmation_row.state = consumed_record.state
            confirmation_row.terminal_at = consumed_record.terminal_at
            confirmation_row.payload = self._payload_dict(consumed_record)
            if consumed_record.terminal_at is None:
                raise RuntimeError("Consumed confirmation is missing terminal evidence.")
            session.add(
                self._solo_administration_confirmation_event_row(
                    build_solo_administration_confirmation_lifecycle_event(
                        record=consumed_record,
                        event_type="consumed",
                        occurred_at=consumed_record.terminal_at,
                    )
                )
            )

        result_record = current_record
        status: Literal["written", "unchanged"] = "unchanged"
        if replacement_record is not None:
            superseded_record = current_record.model_copy(update={"status": "superseded"})
            active_row.status = "superseded"
            active_row.payload = self._authz_policy_payload(superseded_record)
            session.flush()
            self._after_authz_policy_write_step("supersede_active")
            session.add(self._authz_policy_row(replacement_record))
            session.flush()
            self._after_authz_policy_write_step("insert_active")
            result_record = replacement_record
            status = "written"

        stored_completion: LaunchplaneIdempotencyRecord | None = None
        if reservation_row is not None:
            if mutation_reservation is None or mutation is None:
                raise RuntimeError("Authz policy mutation completion evidence is incomplete.")
            completed_at = self._database_mutation_timestamp(session)
            stored_completion = complete_launchplane_mutation_reservation(
                mutation_reservation,
                response_status_code=mutation.response_status_code,
                response_trace_id=mutation.response_trace_id,
                completed_at=completed_at,
                response_payload=mutation.response_payload,
            )
            self._sync_idempotency_row(reservation_row, stored_completion)
            self._after_authz_policy_write_step("complete_idempotency")
        session.commit()
        return AuthzPolicyCompareWriteResult(
            status=status,
            current_record=result_record,
            idempotency_record=stored_completion,
        )

    def list_authz_policy_records(
        self,
        *,
        status: str = "",
        limit: int | None = None,
    ) -> tuple[LaunchplaneAuthzPolicyRecord, ...]:
        statement = select(LaunchplaneAuthzPolicyRow)
        if status:
            statement = statement.where(LaunchplaneAuthzPolicyRow.status == status)
        statement = statement.order_by(desc(LaunchplaneAuthzPolicyRow.revision))
        if limit is not None:
            statement = statement.limit(max(limit, 0))
        with self._session_factory() as session:
            return tuple(self._read_authz_policy_row(row) for row in session.scalars(statement))

    def write_authz_denial_record(self, record: AuthzDenialRecord) -> None:
        with self._session_factory() as session:
            session.execute(
                delete(LaunchplaneAuthzDenialRow).where(
                    LaunchplaneAuthzDenialRow.expires_at <= record.recorded_at
                )
            )
            existing = session.get(LaunchplaneAuthzDenialRow, record.trace_id)
            if existing is not None:
                stored_record = AuthzDenialRecord.model_validate(existing.payload)
                if stored_record != record:
                    session.rollback()
                    raise ValueError("Authz denial trace id already stores different evidence.")
                session.commit()
                return
            session.add(
                LaunchplaneAuthzDenialRow(
                    trace_id=record.trace_id,
                    recorded_at=record.recorded_at,
                    expires_at=record.expires_at,
                    payload=record.model_dump(mode="json"),
                )
            )
            session.commit()

    def read_authz_denial_record(
        self,
        *,
        trace_id: str,
        observed_at: str,
    ) -> AuthzDenialRecord | None:
        with self._session_factory() as session:
            row = session.get(LaunchplaneAuthzDenialRow, trace_id.strip())
            if row is None or row.expires_at <= observed_at:
                return None
            return AuthzDenialRecord.model_validate(row.payload)

    def write_product_profile_record(self, record: LaunchplaneProductProfileRecord) -> None:
        self._write_row(self._product_profile_row(record))

    def compare_and_write_product_profile_record(
        self,
        *,
        expected_record: LaunchplaneProductProfileRecord,
        replacement_record: LaunchplaneProductProfileRecord,
        mutation: DbOnlyMutationRequest | None = None,
        expected_provider_targets: tuple[ProviderTargetRecord, ...] = (),
        expected_dokploy_targets: tuple[DokployTargetRecord, ...] = (),
        expected_dokploy_target_ids: tuple[DokployTargetIdRecord, ...] = (),
    ) -> ProductProfileCompareWriteResult:
        if expected_record.product != replacement_record.product:
            raise ValueError("Product profile compare-and-write requires matching products.")
        if mutation is not None:
            if not 100 <= mutation.response_status_code <= 599:
                raise ValueError("DB-only mutation response status must be between 100 and 599.")
            if not mutation.response_trace_id.strip():
                raise ValueError("DB-only mutation response trace id is required.")
        statement = (
            select(LaunchplaneProductProfileRow)
            .where(LaunchplaneProductProfileRow.product == expected_record.product)
            .limit(1)
        )
        if not self.database_url.startswith("sqlite"):
            statement = statement.with_for_update()
        if mutation is None:
            with self._session_factory() as session:
                self._begin_serialized_write(session)
                return self._compare_and_write_product_profile_locked(
                    session=session,
                    statement=statement,
                    expected_record=expected_record,
                    replacement_record=replacement_record,
                    expected_provider_targets=expected_provider_targets,
                    expected_dokploy_targets=expected_dokploy_targets,
                    expected_dokploy_target_ids=expected_dokploy_target_ids,
                )

        reservation_insert_error: IntegrityError | None = None
        with self._session_factory() as session:
            self._begin_serialized_write(session)
            observed_at = self._database_mutation_timestamp(session)
            stored_reservation = build_launchplane_mutation_reservation(
                scope=mutation.scope,
                route_path=mutation.route_path,
                idempotency_key=mutation.idempotency_key,
                request_fingerprint=mutation.request_fingerprint,
                lease_owner=mutation.lease_owner,
                lease_expires_at=self._mutation_lease_expiry(
                    observed_at=observed_at,
                    lease_seconds=mutation.lease_seconds,
                ),
                reserved_at=observed_at,
            )
            reservation_row = self._idempotency_row(stored_reservation)
            session.add(reservation_row)
            try:
                session.flush()
            except IntegrityError as error:
                session.rollback()
                reservation_insert_error = error
            if reservation_insert_error is None:
                return self._compare_and_write_product_profile_locked(
                    session=session,
                    statement=statement,
                    expected_record=expected_record,
                    replacement_record=replacement_record,
                    reservation_row=reservation_row,
                    mutation_reservation=stored_reservation,
                    mutation=mutation,
                    expected_provider_targets=expected_provider_targets,
                    expected_dokploy_targets=expected_dokploy_targets,
                    expected_dokploy_target_ids=expected_dokploy_target_ids,
                )

        with self._session_factory() as session:
            self._begin_serialized_write(session)
            reservation_row = session.scalar(
                self._idempotency_statement(
                    scope=mutation.scope,
                    route_path=mutation.route_path,
                    idempotency_key=mutation.idempotency_key,
                    for_update=True,
                )
            )
            if reservation_row is None:
                assert reservation_insert_error is not None
                raise reservation_insert_error
            current_reservation = self._read_payload(
                model_type=LaunchplaneIdempotencyRecord,
                payload=reservation_row.payload,
            )
            if current_reservation.request_fingerprint != mutation.request_fingerprint:
                return ProductProfileCompareWriteResult(
                    status="idempotency_conflict",
                    idempotency_record=current_reservation,
                )
            if current_reservation.state == "completed":
                return ProductProfileCompareWriteResult(
                    status="replayed",
                    idempotency_record=current_reservation,
                )
            if current_reservation.state == "reconcile_required":
                return ProductProfileCompareWriteResult(
                    status="reconciliation_required",
                    idempotency_record=current_reservation,
                )
            observed_at = self._database_mutation_timestamp(session)
            if parse_launchplane_mutation_timestamp(
                current_reservation.lease_expires_at,
                field_name="lease_expires_at",
            ) > parse_launchplane_mutation_timestamp(
                observed_at,
                field_name="observed_at",
            ):
                return ProductProfileCompareWriteResult(
                    status="reservation_in_progress",
                    idempotency_record=current_reservation,
                )
            if current_reservation.reconciliation_key:
                reconcile_record = self._updated_idempotency_record(
                    current_reservation,
                    state="reconcile_required",
                    updated_at=observed_at,
                )
                self._sync_idempotency_row(reservation_row, reconcile_record)
                session.commit()
                return ProductProfileCompareWriteResult(
                    status="reconciliation_required",
                    idempotency_record=reconcile_record,
                )
            reclaimed_reservation = self._updated_idempotency_record(
                current_reservation,
                lease_owner=mutation.lease_owner,
                lease_expires_at=self._mutation_lease_expiry(
                    observed_at=observed_at,
                    lease_seconds=mutation.lease_seconds,
                ),
                attempt=current_reservation.attempt + 1,
                updated_at=observed_at,
                response_status_code=None,
                response_trace_id="",
                recorded_at="",
                response_payload={},
            )
            self._sync_idempotency_row(reservation_row, reclaimed_reservation)
            return self._compare_and_write_product_profile_locked(
                session=session,
                statement=statement,
                expected_record=expected_record,
                replacement_record=replacement_record,
                reservation_row=reservation_row,
                mutation_reservation=reclaimed_reservation,
                mutation=mutation,
                expected_provider_targets=expected_provider_targets,
                expected_dokploy_targets=expected_dokploy_targets,
                expected_dokploy_target_ids=expected_dokploy_target_ids,
            )

    def _compare_and_write_product_profile_locked(
        self,
        *,
        session: Any,
        statement: Any,
        expected_record: LaunchplaneProductProfileRecord,
        replacement_record: LaunchplaneProductProfileRecord,
        reservation_row: LaunchplaneIdempotencyRow | None = None,
        mutation_reservation: LaunchplaneIdempotencyRecord | None = None,
        mutation: DbOnlyMutationRequest | None = None,
        expected_provider_targets: tuple[ProviderTargetRecord, ...] = (),
        expected_dokploy_targets: tuple[DokployTargetRecord, ...] = (),
        expected_dokploy_target_ids: tuple[DokployTargetIdRecord, ...] = (),
    ) -> ProductProfileCompareWriteResult:
        if expected_provider_targets or expected_dokploy_targets or expected_dokploy_target_ids:
            self._lock_product_authority_bundle_write(session)
            target_expectations_match = (
                self._provider_target_expectations_match(
                    session=session,
                    expected_records=expected_provider_targets,
                )
                and self._dokploy_target_expectations_match(
                    session=session,
                    expected_records=expected_dokploy_targets,
                )
                and self._dokploy_target_id_expectations_match(
                    session=session,
                    expected_records=expected_dokploy_target_ids,
                )
            )
            if not target_expectations_match:
                if reservation_row is not None:
                    session.delete(reservation_row)
                    session.commit()
                return ProductProfileCompareWriteResult(status="changed")
        row = session.scalar(statement)
        if row is None:
            if reservation_row is not None:
                session.delete(reservation_row)
                session.commit()
            return ProductProfileCompareWriteResult(status="missing")
        current_record = self._read_product_profile_payload(row.payload)
        if self._payload_dict(current_record) != self._payload_dict(expected_record):
            if reservation_row is not None:
                session.delete(reservation_row)
                session.commit()
            return ProductProfileCompareWriteResult(status="changed")
        row.display_name = replacement_record.display_name
        row.repository = replacement_record.repository
        row.driver_id = replacement_record.driver_id
        row.updated_at = replacement_record.updated_at
        row.payload = self._payload_dict(replacement_record)
        stored_completion: LaunchplaneIdempotencyRecord | None = None
        if reservation_row is not None:
            if mutation_reservation is None or mutation is None:
                raise RuntimeError("Product profile mutation completion evidence is incomplete.")
            completed_at = self._database_mutation_timestamp(session)
            stored_completion = complete_launchplane_mutation_reservation(
                mutation_reservation,
                response_status_code=mutation.response_status_code,
                response_trace_id=mutation.response_trace_id,
                completed_at=completed_at,
                response_payload=mutation.response_payload,
            )
            self._sync_idempotency_row(reservation_row, stored_completion)
        session.commit()
        return ProductProfileCompareWriteResult(
            status="written",
            idempotency_record=stored_completion,
        )

    def _provider_target_expectations_match(
        self,
        *,
        session: Any,
        expected_records: tuple[ProviderTargetRecord, ...],
    ) -> bool:
        expected_routes = {(record.context, record.instance): record for record in expected_records}
        if len(expected_routes) != len(expected_records):
            raise ValueError("Provider target expectations must identify unique routes.")
        expected_identities = {
            (record.provider_id, record.target_category, record.target_id)
            for record in expected_records
        }
        if len(expected_identities) != 1:
            raise ValueError("Provider target expectations must share one physical identity.")
        for route, expected_record in expected_routes.items():
            statement = (
                select(LaunchplaneProviderTargetRow)
                .where(
                    LaunchplaneProviderTargetRow.context == route[0],
                    LaunchplaneProviderTargetRow.instance == route[1],
                )
                .limit(1)
            )
            if not self.database_url.startswith("sqlite"):
                statement = statement.with_for_update()
            row = session.scalar(statement)
            if row is None:
                return False
            current_record = self._read_payload(
                model_type=ProviderTargetRecord,
                payload=row.payload,
            )
            if self._payload_dict(current_record) != self._payload_dict(expected_record):
                return False
        provider_id, target_category, target_id = next(iter(expected_identities))
        identity_statement = select(LaunchplaneProviderTargetRow).where(
            LaunchplaneProviderTargetRow.provider_id == provider_id,
            LaunchplaneProviderTargetRow.target_category == target_category,
            LaunchplaneProviderTargetRow.target_id == target_id,
        )
        if not self.database_url.startswith("sqlite"):
            identity_statement = identity_statement.with_for_update()
        claimed_routes = {
            (row.context, row.instance) for row in session.scalars(identity_statement)
        }
        return claimed_routes == set(expected_routes)

    def _dokploy_target_expectations_match(
        self,
        *,
        session: Any,
        expected_records: tuple[DokployTargetRecord, ...],
    ) -> bool:
        expected_routes = {(record.context, record.instance): record for record in expected_records}
        if len(expected_routes) != len(expected_records):
            raise ValueError("Dokploy target expectations must identify unique routes.")
        for route, expected_record in expected_routes.items():
            statement = (
                select(LaunchplaneDokployTargetRow)
                .where(
                    LaunchplaneDokployTargetRow.context == route[0],
                    LaunchplaneDokployTargetRow.instance == route[1],
                )
                .limit(1)
            )
            if not self.database_url.startswith("sqlite"):
                statement = statement.with_for_update()
            row = session.scalar(statement)
            if row is None:
                return False
            current_record = self._read_payload(
                model_type=DokployTargetRecord,
                payload=row.payload,
            )
            if self._payload_dict(current_record) != self._payload_dict(expected_record):
                return False
        return True

    def _dokploy_target_id_expectations_match(
        self,
        *,
        session: Any,
        expected_records: tuple[DokployTargetIdRecord, ...],
    ) -> bool:
        expected_routes = {(record.context, record.instance): record for record in expected_records}
        if len(expected_routes) != len(expected_records):
            raise ValueError("Dokploy target-id expectations must identify unique routes.")
        for route, expected_record in expected_routes.items():
            statement = (
                select(LaunchplaneDokployTargetIdRow)
                .where(
                    LaunchplaneDokployTargetIdRow.context == route[0],
                    LaunchplaneDokployTargetIdRow.instance == route[1],
                )
                .limit(1)
            )
            if not self.database_url.startswith("sqlite"):
                statement = statement.with_for_update()
            row = session.scalar(statement)
            if row is None:
                return False
            current_record = self._read_payload(
                model_type=DokployTargetIdRecord,
                payload=row.payload,
            )
            if self._payload_dict(current_record) != self._payload_dict(expected_record):
                return False
        return True

    @staticmethod
    def _read_product_profile_payload(payload: PayloadDict) -> LaunchplaneProductProfileRecord:
        return LaunchplaneProductProfileRecord.model_validate(
            migrate_product_profile_lifecycle_payload(
                migrate_product_profile_monitoring_intent_payload(
                    migrate_product_profile_health_monitoring_payload(payload)
                )
            )
        )

    def read_product_profile_record(self, product: str) -> LaunchplaneProductProfileRecord:
        statement = (
            select(LaunchplaneProductProfileRow)
            .where(LaunchplaneProductProfileRow.product == product)
            .limit(1)
        )
        with self._session_factory() as session:
            row = session.scalar(statement)
            if row is None:
                raise FileNotFoundError(
                    "No Launchplane record found in launchplane_product_profiles "
                    f"for product={product!r}"
                )
            return self._read_product_profile_payload(row.payload)

    def list_product_profile_records(
        self,
        *,
        driver_id: str = "",
    ) -> tuple[LaunchplaneProductProfileRecord, ...]:
        filters: list[object] = []
        if driver_id:
            filters.append(LaunchplaneProductProfileRow.driver_id == driver_id)
        statement = select(LaunchplaneProductProfileRow)
        if filters:
            statement = statement.where(*cast(Any, filters))
        statement = statement.order_by(LaunchplaneProductProfileRow.product.asc())
        with self._session_factory() as session:
            rows = session.scalars(statement).all()
            return tuple(self._read_product_profile_payload(row.payload) for row in rows)

    def write_product_retirement_record(self, record: ProductRetirementRecord) -> None:
        with self._session_factory() as session:
            existing = session.get(LaunchplaneProductRetirementRow, record.record_id)
            if existing is not None:
                stored = self._read_payload(
                    model_type=ProductRetirementRecord,
                    payload=existing.payload,
                )
                if stored != record and not (
                    record.mode == "plan"
                    and stored.mode == "plan"
                    and stored.product == record.product
                    and stored.identity.actor == record.identity.actor
                    and stored.idempotency_key == record.idempotency_key
                    and stored.continuity_sha256 == record.continuity_sha256
                ):
                    raise ValueError("Product retirement records are append-only.")
                return
            session.add(
                LaunchplaneProductRetirementRow(
                    record_id=record.record_id,
                    plan_record_id=record.plan_record_id,
                    product=record.product,
                    context=record.context,
                    instance=record.instance,
                    actor=record.identity.actor,
                    idempotency_key=record.idempotency_key,
                    mode=record.mode,
                    outcome=record.outcome,
                    recorded_at=record.recorded_at,
                    payload=self._payload_dict(record),
                )
            )
            try:
                session.commit()
            except IntegrityError as error:
                session.rollback()
                if record.mode != "plan":
                    raise
                existing_plan = session.scalar(
                    select(LaunchplaneProductRetirementRow).where(
                        LaunchplaneProductRetirementRow.product == record.product,
                        LaunchplaneProductRetirementRow.actor == record.identity.actor,
                        LaunchplaneProductRetirementRow.idempotency_key == record.idempotency_key,
                        LaunchplaneProductRetirementRow.mode == "plan",
                    )
                )
                if existing_plan is None:
                    raise error
                stored = self._read_payload(
                    model_type=ProductRetirementRecord,
                    payload=existing_plan.payload,
                )
                if stored.continuity_sha256 != record.continuity_sha256:
                    raise ValueError(
                        "Product retirement plan idempotency key was reused."
                    ) from error

    def read_product_retirement_record(self, record_id: str) -> ProductRetirementRecord:
        with self._session_factory() as session:
            row = session.get(LaunchplaneProductRetirementRow, record_id)
            if row is None:
                raise FileNotFoundError(
                    f"No Launchplane product retirement record found for record_id={record_id!r}."
                )
            return self._read_payload(
                model_type=ProductRetirementRecord,
                payload=row.payload,
            )

    def list_product_retirement_records(
        self,
        *,
        product: str = "",
        actor: str = "",
        mode: str = "",
        idempotency_key: str = "",
        limit: int | None = None,
    ) -> tuple[ProductRetirementRecord, ...]:
        statement = select(LaunchplaneProductRetirementRow)
        filters: list[object] = []
        if product:
            filters.append(LaunchplaneProductRetirementRow.product == product)
        if actor:
            filters.append(LaunchplaneProductRetirementRow.actor == actor)
        if mode:
            filters.append(LaunchplaneProductRetirementRow.mode == mode)
        if idempotency_key:
            filters.append(LaunchplaneProductRetirementRow.idempotency_key == idempotency_key)
        if filters:
            statement = statement.where(*cast(Any, filters))
        statement = statement.order_by(
            desc(LaunchplaneProductRetirementRow.recorded_at),
            desc(LaunchplaneProductRetirementRow.record_id),
        )
        if limit is not None:
            statement = statement.limit(max(limit, 0))
        with self._session_factory() as session:
            return tuple(
                self._read_payload(
                    model_type=ProductRetirementRecord,
                    payload=row.payload,
                )
                for row in session.scalars(statement)
            )

    def write_detached_application_retirement_record(
        self, record: DetachedApplicationRetirementRecord
    ) -> None:
        candidate_target_sha256 = record.candidate_observation.target_id_sha256
        with self._session_factory() as session:
            existing = session.get(
                LaunchplaneDetachedApplicationRetirementRow,
                record.record_id,
            )
            if existing is not None:
                stored = self._read_payload(
                    model_type=DetachedApplicationRetirementRecord,
                    payload=existing.payload,
                )
                if stored != record and not (
                    record.mode == "plan"
                    and stored.mode == "plan"
                    and stored.candidate_observation.target_id_sha256 == candidate_target_sha256
                    and stored.identity.actor == record.identity.actor
                    and stored.idempotency_key == record.idempotency_key
                    and stored.continuity_sha256 == record.continuity_sha256
                ):
                    raise ValueError("Detached application retirement records are append-only.")
                return
            session.add(
                LaunchplaneDetachedApplicationRetirementRow(
                    record_id=record.record_id,
                    plan_record_id=record.plan_record_id,
                    candidate_target_sha256=candidate_target_sha256,
                    actor=record.identity.actor,
                    idempotency_key=record.idempotency_key,
                    mode=record.mode,
                    outcome=record.outcome,
                    recorded_at=record.recorded_at,
                    protected_target_count=len(record.protected_targets),
                    authority_write_count=record.authority_write_count,
                    payload=self._payload_dict(record),
                )
            )
            try:
                session.commit()
            except IntegrityError as error:
                session.rollback()
                if record.mode != "plan":
                    raise
                existing_plan = session.scalar(
                    select(LaunchplaneDetachedApplicationRetirementRow).where(
                        LaunchplaneDetachedApplicationRetirementRow.candidate_target_sha256
                        == candidate_target_sha256,
                        LaunchplaneDetachedApplicationRetirementRow.actor == record.identity.actor,
                        LaunchplaneDetachedApplicationRetirementRow.idempotency_key
                        == record.idempotency_key,
                        LaunchplaneDetachedApplicationRetirementRow.mode == "plan",
                    )
                )
                if existing_plan is None:
                    raise error
                stored = self._read_payload(
                    model_type=DetachedApplicationRetirementRecord,
                    payload=existing_plan.payload,
                )
                if stored.continuity_sha256 != record.continuity_sha256:
                    raise ValueError(
                        "Detached application retirement plan idempotency key was reused."
                    ) from error

    def read_detached_application_retirement_record(
        self, record_id: str
    ) -> DetachedApplicationRetirementRecord:
        with self._session_factory() as session:
            row = session.get(LaunchplaneDetachedApplicationRetirementRow, record_id)
            if row is None:
                raise FileNotFoundError(
                    "No Launchplane detached application retirement record found for "
                    f"record_id={record_id!r}."
                )
            return self._read_payload(
                model_type=DetachedApplicationRetirementRecord,
                payload=row.payload,
            )

    def list_detached_application_retirement_records(
        self,
        *,
        candidate_target_sha256: str = "",
        actor: str = "",
        mode: str = "",
        idempotency_key: str = "",
        limit: int | None = None,
    ) -> tuple[DetachedApplicationRetirementRecord, ...]:
        statement = select(LaunchplaneDetachedApplicationRetirementRow)
        filters: list[object] = []
        if candidate_target_sha256:
            filters.append(
                LaunchplaneDetachedApplicationRetirementRow.candidate_target_sha256
                == candidate_target_sha256
            )
        if actor:
            filters.append(LaunchplaneDetachedApplicationRetirementRow.actor == actor)
        if mode:
            filters.append(LaunchplaneDetachedApplicationRetirementRow.mode == mode)
        if idempotency_key:
            filters.append(
                LaunchplaneDetachedApplicationRetirementRow.idempotency_key == idempotency_key
            )
        if filters:
            statement = statement.where(*cast(Any, filters))
        statement = statement.order_by(
            desc(LaunchplaneDetachedApplicationRetirementRow.recorded_at),
            desc(LaunchplaneDetachedApplicationRetirementRow.record_id),
        )
        if limit is not None:
            statement = statement.limit(max(limit, 0))
        with self._session_factory() as session:
            return tuple(
                self._read_payload(
                    model_type=DetachedApplicationRetirementRecord,
                    payload=row.payload,
                )
                for row in session.scalars(statement)
            )

    def write_public_ingress_observation_record(
        self, record: PublicIngressObservationRecord
    ) -> None:
        self._write_row(
            LaunchplanePublicIngressObservationRow(
                record_id=record.record_id,
                product=record.product,
                context=record.context,
                instance=record.instance,
                status=record.status,
                observed_at=record.observed_at,
                incident_id=record.incident_id,
                check_token=canonical_health_check_record_token(record.check_name),
                check_kind=record.check_kind,
                payload=self._payload_dict(record),
            )
        )

    def write_ingress_route_audit_record(self, record: IngressRouteAuditRecord) -> None:
        self._write_row(
            LaunchplaneIngressRouteAuditRow(
                record_id=record.record_id,
                product=record.product,
                context=record.context,
                mode=record.mode,
                status=record.status,
                provider_host_id=record.provider_host_id,
                recorded_at=record.recorded_at,
                payload=self._payload_dict(record),
            )
        )

    def write_edge_endpoint_record(self, record: EdgeEndpointRecord) -> None:
        self._write_row(
            LaunchplaneEdgeEndpointRow(
                endpoint_key=record.endpoint_key,
                provider=record.provider,
                server_name=record.server_name,
                upstream_host=record.upstream_host,
                upstream_scheme=record.upstream_scheme,
                upstream_port=record.upstream_port,
                status=record.status,
                updated_at=record.updated_at,
                payload=self._payload_dict(record),
            )
        )

    def write_ingress_canary_route_record(self, record: IngressCanaryRouteRecord) -> None:
        self._write_row(
            LaunchplaneIngressCanaryRouteRow(
                canary_key=record.canary_key,
                product=record.product,
                context=record.context,
                domain_name=record.domain_name,
                expected_host_id=record.expected_host_id,
                edge_endpoint_key=record.edge_endpoint_key,
                certificate_id=str(record.certificate_id),
                status=record.status,
                updated_at=record.updated_at,
                payload=self._payload_dict(record),
            )
        )

    def _route_binding_row(
        self,
        record: EnvironmentRouteBindingRecord,
    ) -> LaunchplaneRouteBindingRow:
        primary_domain = next(
            (domain.domain_name for domain in record.domains if domain.role == "primary"),
            "",
        )
        return LaunchplaneRouteBindingRow(
            product=record.product,
            context=record.context,
            instance=record.instance,
            provider_id=record.provider_target.provider_id,
            target_category=record.provider_target.target_category,
            ingress_provider=record.ingress.provider,
            ingress_endpoint_key=record.ingress.endpoint_key,
            termination_kind=record.ingress.termination_kind,
            tls_owner=record.tls.owner,
            primary_domain=primary_domain,
            status=record.status,
            freshness_status=record.source.freshness_status,
            updated_at=record.updated_at,
            payload=self._payload_dict(record),
        )

    def write_route_binding_record(self, record: EnvironmentRouteBindingRecord) -> None:
        self._write_row(self._route_binding_row(record))

    def reconcile_route_binding_record(
        self,
        *,
        expected_record: EnvironmentRouteBindingRecord | None,
        replacement_record: EnvironmentRouteBindingRecord,
        mutation: DbOnlyMutationRequest,
    ) -> RouteBindingReconcileWriteResult:
        if expected_record is not None and (
            expected_record.product,
            expected_record.context,
            expected_record.instance,
        ) != (
            replacement_record.product,
            replacement_record.context,
            replacement_record.instance,
        ):
            raise ValueError("Route binding reconcile requires matching record identities.")
        if not 100 <= mutation.response_status_code <= 599:
            raise ValueError("DB-only mutation response status must be between 100 and 599.")
        if not mutation.response_trace_id.strip():
            raise ValueError("DB-only mutation response trace id is required.")
        statement = (
            select(LaunchplaneRouteBindingRow)
            .where(
                LaunchplaneRouteBindingRow.product == replacement_record.product,
                LaunchplaneRouteBindingRow.context == replacement_record.context,
                LaunchplaneRouteBindingRow.instance == replacement_record.instance,
            )
            .limit(1)
        )
        if not self.database_url.startswith("sqlite"):
            statement = statement.with_for_update()

        reservation_insert_error: IntegrityError | None = None
        with self._session_factory() as session:
            self._begin_serialized_write(session)
            self._lock_route_binding_write(
                session,
                binding_key=replacement_record.binding_key,
            )
            observed_at = self._database_mutation_timestamp(session)
            stored_reservation = build_launchplane_mutation_reservation(
                scope=mutation.scope,
                route_path=mutation.route_path,
                idempotency_key=mutation.idempotency_key,
                request_fingerprint=mutation.request_fingerprint,
                lease_owner=mutation.lease_owner,
                lease_expires_at=self._mutation_lease_expiry(
                    observed_at=observed_at,
                    lease_seconds=mutation.lease_seconds,
                ),
                reserved_at=observed_at,
            )
            reservation_row = self._idempotency_row(stored_reservation)
            session.add(reservation_row)
            try:
                session.flush()
            except IntegrityError as error:
                session.rollback()
                reservation_insert_error = error
            if reservation_insert_error is None:
                return self._reconcile_route_binding_locked(
                    session=session,
                    statement=statement,
                    expected_record=expected_record,
                    replacement_record=replacement_record,
                    reservation_row=reservation_row,
                    mutation_reservation=stored_reservation,
                    mutation=mutation,
                )

        with self._session_factory() as session:
            self._begin_serialized_write(session)
            self._lock_route_binding_write(
                session,
                binding_key=replacement_record.binding_key,
            )
            reservation_row = session.scalar(
                self._idempotency_statement(
                    scope=mutation.scope,
                    route_path=mutation.route_path,
                    idempotency_key=mutation.idempotency_key,
                    for_update=True,
                )
            )
            if reservation_row is None:
                assert reservation_insert_error is not None
                raise reservation_insert_error
            current_reservation = self._read_payload(
                model_type=LaunchplaneIdempotencyRecord,
                payload=reservation_row.payload,
            )
            if current_reservation.request_fingerprint != mutation.request_fingerprint:
                return RouteBindingReconcileWriteResult(
                    status="idempotency_conflict",
                    idempotency_record=current_reservation,
                )
            if current_reservation.state == "completed":
                return RouteBindingReconcileWriteResult(
                    status="replayed",
                    idempotency_record=current_reservation,
                )
            if current_reservation.state == "reconcile_required":
                return RouteBindingReconcileWriteResult(
                    status="reconciliation_required",
                    idempotency_record=current_reservation,
                )
            observed_at = self._database_mutation_timestamp(session)
            if parse_launchplane_mutation_timestamp(
                current_reservation.lease_expires_at,
                field_name="lease_expires_at",
            ) > parse_launchplane_mutation_timestamp(
                observed_at,
                field_name="observed_at",
            ):
                return RouteBindingReconcileWriteResult(
                    status="reservation_in_progress",
                    idempotency_record=current_reservation,
                )
            if current_reservation.reconciliation_key:
                reconcile_record = self._updated_idempotency_record(
                    current_reservation,
                    state="reconcile_required",
                    updated_at=observed_at,
                )
                self._sync_idempotency_row(reservation_row, reconcile_record)
                session.commit()
                return RouteBindingReconcileWriteResult(
                    status="reconciliation_required",
                    idempotency_record=reconcile_record,
                )
            reclaimed_reservation = self._updated_idempotency_record(
                current_reservation,
                lease_owner=mutation.lease_owner,
                lease_expires_at=self._mutation_lease_expiry(
                    observed_at=observed_at,
                    lease_seconds=mutation.lease_seconds,
                ),
                attempt=current_reservation.attempt + 1,
                updated_at=observed_at,
                response_status_code=None,
                response_trace_id="",
                recorded_at="",
                response_payload={},
            )
            self._sync_idempotency_row(reservation_row, reclaimed_reservation)
            return self._reconcile_route_binding_locked(
                session=session,
                statement=statement,
                expected_record=expected_record,
                replacement_record=replacement_record,
                reservation_row=reservation_row,
                mutation_reservation=reclaimed_reservation,
                mutation=mutation,
            )

    def _reconcile_route_binding_locked(
        self,
        *,
        session: Any,
        statement: Any,
        expected_record: EnvironmentRouteBindingRecord | None,
        replacement_record: EnvironmentRouteBindingRecord,
        reservation_row: LaunchplaneIdempotencyRow,
        mutation_reservation: LaunchplaneIdempotencyRecord,
        mutation: DbOnlyMutationRequest,
    ) -> RouteBindingReconcileWriteResult:
        row = session.scalar(statement)
        current_record = (
            self._read_payload(
                model_type=EnvironmentRouteBindingRecord,
                payload=row.payload,
            )
            if row is not None
            else None
        )
        if expected_record is None and current_record is not None:
            session.delete(reservation_row)
            session.commit()
            return RouteBindingReconcileWriteResult(
                status="changed",
                current_record=current_record,
            )
        if expected_record is not None and current_record is None:
            session.delete(reservation_row)
            session.commit()
            return RouteBindingReconcileWriteResult(status="missing")
        if (
            expected_record is not None
            and current_record is not None
            and (self._payload_dict(current_record) != self._payload_dict(expected_record))
        ):
            session.delete(reservation_row)
            session.commit()
            return RouteBindingReconcileWriteResult(
                status="changed",
                current_record=current_record,
            )

        if row is None:
            session.add(self._route_binding_row(replacement_record))
            status: RouteBindingReconcileWriteStatus = "created"
        elif current_record == replacement_record:
            status = "unchanged"
        else:
            self._sync_route_binding_row(row, replacement_record)
            status = "refreshed"
        completed_at = self._database_mutation_timestamp(session)
        completion = complete_launchplane_mutation_reservation(
            mutation_reservation,
            response_status_code=mutation.response_status_code,
            response_trace_id=mutation.response_trace_id,
            completed_at=completed_at,
            response_payload=mutation.response_payload,
        )
        self._sync_idempotency_row(reservation_row, completion)
        session.commit()
        return RouteBindingReconcileWriteResult(
            status=status,
            current_record=replacement_record,
            idempotency_record=completion,
        )

    def _sync_route_binding_row(
        self,
        row: LaunchplaneRouteBindingRow,
        record: EnvironmentRouteBindingRecord,
    ) -> None:
        replacement_row = self._route_binding_row(record)
        row.provider_id = replacement_row.provider_id
        row.target_category = replacement_row.target_category
        row.ingress_provider = replacement_row.ingress_provider
        row.ingress_endpoint_key = replacement_row.ingress_endpoint_key
        row.termination_kind = replacement_row.termination_kind
        row.tls_owner = replacement_row.tls_owner
        row.primary_domain = replacement_row.primary_domain
        row.status = replacement_row.status
        row.freshness_status = replacement_row.freshness_status
        row.updated_at = replacement_row.updated_at
        row.payload = replacement_row.payload

    def read_route_binding_record(
        self,
        *,
        product: str,
        context_name: str,
        instance_name: str,
    ) -> EnvironmentRouteBindingRecord:
        return self._read_model(
            model_type=EnvironmentRouteBindingRecord,
            orm_model=LaunchplaneRouteBindingRow,
            filters=(
                LaunchplaneRouteBindingRow.product == product,
                LaunchplaneRouteBindingRow.context == context_name,
                LaunchplaneRouteBindingRow.instance == instance_name,
            ),
        )

    def list_route_binding_records(
        self,
        *,
        product: str = "",
        context_name: str = "",
        instance_name: str = "",
        status: str = "",
        limit: int | None = None,
    ) -> tuple[EnvironmentRouteBindingRecord, ...]:
        filters: list[object] = []
        if product:
            filters.append(LaunchplaneRouteBindingRow.product == product)
        if context_name:
            filters.append(LaunchplaneRouteBindingRow.context == context_name)
        if instance_name:
            filters.append(LaunchplaneRouteBindingRow.instance == instance_name)
        if status:
            filters.append(LaunchplaneRouteBindingRow.status == status)
        return self._list_models(
            model_type=EnvironmentRouteBindingRecord,
            orm_model=LaunchplaneRouteBindingRow,
            filters=filters,
            order_by=(
                LaunchplaneRouteBindingRow.product.asc(),
                LaunchplaneRouteBindingRow.context.asc(),
                LaunchplaneRouteBindingRow.instance.asc(),
            ),
            limit=limit,
        )

    def read_ingress_canary_route_record(self, canary_key: str) -> IngressCanaryRouteRecord:
        return self._read_model(
            model_type=IngressCanaryRouteRecord,
            orm_model=LaunchplaneIngressCanaryRouteRow,
            filters=(LaunchplaneIngressCanaryRouteRow.canary_key == canary_key,),
        )

    def list_ingress_canary_route_records(
        self,
        *,
        product: str = "",
        context_name: str = "",
        status: str = "",
        limit: int | None = None,
    ) -> tuple[IngressCanaryRouteRecord, ...]:
        filters: list[object] = []
        if product:
            filters.append(LaunchplaneIngressCanaryRouteRow.product == product)
        if context_name:
            filters.append(LaunchplaneIngressCanaryRouteRow.context == context_name)
        if status:
            filters.append(LaunchplaneIngressCanaryRouteRow.status == status)
        return self._list_models(
            model_type=IngressCanaryRouteRecord,
            orm_model=LaunchplaneIngressCanaryRouteRow,
            filters=filters,
            order_by=(
                LaunchplaneIngressCanaryRouteRow.product.asc(),
                LaunchplaneIngressCanaryRouteRow.context.asc(),
                LaunchplaneIngressCanaryRouteRow.canary_key.asc(),
            ),
            limit=limit,
        )

    def read_edge_endpoint_record(self, endpoint_key: str) -> EdgeEndpointRecord:
        return self._read_model(
            model_type=EdgeEndpointRecord,
            orm_model=LaunchplaneEdgeEndpointRow,
            filters=(LaunchplaneEdgeEndpointRow.endpoint_key == endpoint_key,),
        )

    def list_edge_endpoint_records(
        self,
        *,
        provider: str = "",
        status: str = "",
        limit: int | None = None,
    ) -> tuple[EdgeEndpointRecord, ...]:
        filters: list[object] = []
        if provider:
            filters.append(LaunchplaneEdgeEndpointRow.provider == provider)
        if status:
            filters.append(LaunchplaneEdgeEndpointRow.status == status)
        return self._list_models(
            model_type=EdgeEndpointRecord,
            orm_model=LaunchplaneEdgeEndpointRow,
            filters=filters,
            order_by=(
                LaunchplaneEdgeEndpointRow.provider.asc(),
                LaunchplaneEdgeEndpointRow.endpoint_key.asc(),
            ),
            limit=limit,
        )

    def write_private_health_endpoint_record(self, record: PrivateHealthEndpointRecord) -> None:
        self._write_row(
            LaunchplanePrivateHealthEndpointRow(
                endpoint_key=record.endpoint_key,
                product=record.product,
                context=record.context,
                instance=record.instance,
                url=record.url,
                status=record.status,
                updated_at=record.updated_at,
                payload=self._payload_dict(record),
            )
        )

    def read_private_health_endpoint_record(self, endpoint_key: str) -> PrivateHealthEndpointRecord:
        return self._read_model(
            model_type=PrivateHealthEndpointRecord,
            orm_model=LaunchplanePrivateHealthEndpointRow,
            filters=(LaunchplanePrivateHealthEndpointRow.endpoint_key == endpoint_key,),
        )

    def list_private_health_endpoint_records(
        self,
        *,
        product: str = "",
        context_name: str = "",
        instance_name: str = "",
        status: str = "",
        limit: int | None = None,
    ) -> tuple[PrivateHealthEndpointRecord, ...]:
        filters: list[object] = []
        if product:
            filters.append(LaunchplanePrivateHealthEndpointRow.product == product)
        if context_name:
            filters.append(LaunchplanePrivateHealthEndpointRow.context == context_name)
        if instance_name:
            filters.append(LaunchplanePrivateHealthEndpointRow.instance == instance_name)
        if status:
            filters.append(LaunchplanePrivateHealthEndpointRow.status == status)
        return self._list_models(
            model_type=PrivateHealthEndpointRecord,
            orm_model=LaunchplanePrivateHealthEndpointRow,
            filters=filters,
            order_by=(
                LaunchplanePrivateHealthEndpointRow.product.asc(),
                LaunchplanePrivateHealthEndpointRow.context.asc(),
                LaunchplanePrivateHealthEndpointRow.instance.asc(),
                LaunchplanePrivateHealthEndpointRow.endpoint_key.asc(),
            ),
            limit=limit,
        )

    def read_ingress_route_audit_record(self, record_id: str) -> IngressRouteAuditRecord:
        return self._read_model(
            model_type=IngressRouteAuditRecord,
            orm_model=LaunchplaneIngressRouteAuditRow,
            filters=(LaunchplaneIngressRouteAuditRow.record_id == record_id,),
        )

    def list_ingress_route_audit_records(
        self,
        *,
        product: str = "",
        context_name: str = "",
        limit: int | None = None,
    ) -> tuple[IngressRouteAuditRecord, ...]:
        filters: list[object] = []
        if product:
            filters.append(LaunchplaneIngressRouteAuditRow.product == product)
        if context_name:
            filters.append(LaunchplaneIngressRouteAuditRow.context == context_name)
        return self._list_models(
            model_type=IngressRouteAuditRecord,
            orm_model=LaunchplaneIngressRouteAuditRow,
            filters=filters,
            order_by=(
                LaunchplaneIngressRouteAuditRow.recorded_at.desc(),
                LaunchplaneIngressRouteAuditRow.record_id.desc(),
            ),
            limit=limit,
        )

    def list_public_ingress_observation_records(
        self,
        *,
        product: str = "",
        context_name: str = "",
        instance_name: str = "",
        check_name: str = "",
        check_kind: str = "",
        incident_id: str = "",
        limit: int | None = None,
    ) -> tuple[PublicIngressObservationRecord, ...]:
        filters: list[object] = []
        if product:
            filters.append(LaunchplanePublicIngressObservationRow.product == product)
        if context_name:
            filters.append(LaunchplanePublicIngressObservationRow.context == context_name)
        if instance_name:
            filters.append(LaunchplanePublicIngressObservationRow.instance == instance_name)
        if check_name:
            filters.append(
                LaunchplanePublicIngressObservationRow.check_token
                == canonical_health_check_record_token(check_name)
            )
        if check_kind:
            filters.append(LaunchplanePublicIngressObservationRow.check_kind == check_kind)
        if incident_id:
            filters.append(LaunchplanePublicIngressObservationRow.incident_id == incident_id)
        records = self._list_models(
            model_type=PublicIngressObservationRecord,
            orm_model=LaunchplanePublicIngressObservationRow,
            filters=filters,
            order_by=(
                LaunchplanePublicIngressObservationRow.observed_at.desc(),
                LaunchplanePublicIngressObservationRow.record_id.desc(),
            ),
            limit=limit,
        )
        return records

    def write_public_ingress_incident_record(self, record: PublicIngressIncidentRecord) -> None:
        self._write_row(
            LaunchplanePublicIngressIncidentRow(
                incident_id=record.incident_id,
                product=record.product,
                context=record.context,
                instance=record.instance,
                status=record.status,
                check_token=canonical_health_check_record_token(record.check_name),
                check_kind=record.check_kind,
                state_version=record.state_version,
                opened_at=record.opened_at,
                latest_observed_at=record.latest_observed_at,
                payload=self._payload_dict(record),
            )
        )

    def read_public_ingress_incident_record(self, incident_id: str) -> PublicIngressIncidentRecord:
        return self._read_model(
            model_type=PublicIngressIncidentRecord,
            orm_model=LaunchplanePublicIngressIncidentRow,
            filters=(LaunchplanePublicIngressIncidentRow.incident_id == incident_id,),
        )

    def write_public_ingress_incident_event_record(
        self, record: PublicIngressIncidentEventRecord
    ) -> None:
        self._write_row(
            LaunchplanePublicIngressIncidentEventRow(
                event_id=record.event_id,
                incident_id=record.incident_id,
                event=record.event,
                occurred_at=record.occurred_at,
                payload=self._payload_dict(record),
            )
        )

    def list_public_ingress_incident_event_records(
        self,
        *,
        incident_id: str = "",
        event: str = "",
        limit: int | None = None,
    ) -> tuple[PublicIngressIncidentEventRecord, ...]:
        filters: list[object] = []
        if incident_id:
            filters.append(LaunchplanePublicIngressIncidentEventRow.incident_id == incident_id)
        if event:
            filters.append(LaunchplanePublicIngressIncidentEventRow.event == event)
        return self._list_models(
            model_type=PublicIngressIncidentEventRecord,
            orm_model=LaunchplanePublicIngressIncidentEventRow,
            filters=filters,
            order_by=(
                LaunchplanePublicIngressIncidentEventRow.occurred_at.desc(),
                LaunchplanePublicIngressIncidentEventRow.event_id.desc(),
            ),
            limit=limit,
        )

    def write_public_ingress_incident_reminder_state_record(
        self, record: PublicIngressIncidentReminderStateRecord
    ) -> None:
        self._write_row(
            LaunchplanePublicIngressIncidentReminderRow(
                reminder_state_id=record.reminder_state_id,
                incident_id=record.incident_id,
                policy_id=record.policy_id,
                status=record.status,
                next_reminder_at=record.next_reminder_at,
                updated_at=record.updated_at,
                payload=self._payload_dict(record),
            )
        )

    def list_public_ingress_incident_reminder_state_records(
        self,
        *,
        incident_id: str = "",
        policy_id: str = "",
        status: str = "",
        limit: int | None = None,
    ) -> tuple[PublicIngressIncidentReminderStateRecord, ...]:
        filters: list[object] = []
        if incident_id:
            filters.append(LaunchplanePublicIngressIncidentReminderRow.incident_id == incident_id)
        if policy_id:
            filters.append(LaunchplanePublicIngressIncidentReminderRow.policy_id == policy_id)
        if status:
            filters.append(LaunchplanePublicIngressIncidentReminderRow.status == status)
        return self._list_models(
            model_type=PublicIngressIncidentReminderStateRecord,
            orm_model=LaunchplanePublicIngressIncidentReminderRow,
            filters=filters,
            order_by=(
                LaunchplanePublicIngressIncidentReminderRow.updated_at.desc(),
                LaunchplanePublicIngressIncidentReminderRow.reminder_state_id.desc(),
            ),
            limit=limit,
        )

    def write_public_ingress_transition_with_outbox(
        self,
        *,
        observation: PublicIngressObservationRecord,
        incidents: tuple[PublicIngressIncidentRecord, ...],
        incident_events: tuple[PublicIngressIncidentEventRecord, ...] = (),
        reminder_states: tuple[PublicIngressIncidentReminderStateRecord, ...] = (),
        outbox_deliveries: tuple[OutboxDeliveryRecord, ...] = (),
        expected_open_incident_id: str = "",
        expected_open_incident_state_version: int = 0,
        expected_open_incident_sha256: str = "",
        expected_profile_sha256: str = "",
        expected_private_endpoint_key: str = "",
        expected_private_endpoint_sha256: str = "",
        expected_route_binding_sha256: str = "",
    ) -> PublicIngressTransitionWriteResult:
        check_token = canonical_health_check_record_token(observation.check_name)
        with self._session_factory() as session:
            self._begin_serialized_write(session)
            authority_changed = False
            if expected_profile_sha256:
                profile_statement = (
                    select(LaunchplaneProductProfileRow)
                    .where(LaunchplaneProductProfileRow.product == observation.product)
                    .limit(1)
                )
                if not self.database_url.startswith("sqlite"):
                    profile_statement = profile_statement.with_for_update()
                profile_row = session.scalar(profile_statement)
                authority_changed = (
                    profile_row is None
                    or product_profile_record_sha256(
                        self._read_product_profile_payload(profile_row.payload)
                    )
                    != expected_profile_sha256
                )
            if not authority_changed and expected_private_endpoint_sha256:
                private_endpoint_statement = (
                    select(LaunchplanePrivateHealthEndpointRow)
                    .where(
                        LaunchplanePrivateHealthEndpointRow.endpoint_key
                        == expected_private_endpoint_key
                    )
                    .limit(1)
                )
                if not self.database_url.startswith("sqlite"):
                    private_endpoint_statement = private_endpoint_statement.with_for_update()
                private_endpoint_row = session.scalar(private_endpoint_statement)
                private_endpoint_sha256 = (
                    "missing"
                    if private_endpoint_row is None
                    else private_health_endpoint_record_sha256(
                        PrivateHealthEndpointRecord.model_validate(private_endpoint_row.payload)
                    )
                )
                authority_changed = private_endpoint_sha256 != expected_private_endpoint_sha256
            if not authority_changed and expected_route_binding_sha256:
                route_binding_statement = (
                    select(LaunchplaneRouteBindingRow)
                    .where(
                        LaunchplaneRouteBindingRow.product == observation.product,
                        LaunchplaneRouteBindingRow.context == observation.context,
                        LaunchplaneRouteBindingRow.instance == observation.instance,
                    )
                    .limit(1)
                )
                if not self.database_url.startswith("sqlite"):
                    route_binding_statement = route_binding_statement.with_for_update()
                route_binding_row = session.scalar(route_binding_statement)
                route_binding_sha256 = (
                    "missing"
                    if route_binding_row is None
                    else route_binding_record_sha256(
                        EnvironmentRouteBindingRecord.model_validate(route_binding_row.payload)
                    )
                )
                authority_changed = route_binding_sha256 != expected_route_binding_sha256
            if authority_changed:
                observation_row = LaunchplanePublicIngressObservationRow(
                    record_id=observation.record_id,
                    product=observation.product,
                    context=observation.context,
                    instance=observation.instance,
                    status=observation.status,
                    observed_at=observation.observed_at,
                    check_token=check_token,
                    check_kind=observation.check_kind,
                    payload=self._payload_dict(
                        observation.model_copy(
                            update={
                                "incident_id": "",
                                "incident_event_id": "",
                            }
                        )
                    ),
                )
                observation_row.incident_id = ""
                session.merge(observation_row)
                session.commit()
                return PublicIngressTransitionWriteResult(status="authority_changed")
            self._lock_public_ingress_incident_write(
                session,
                product=observation.product,
                context_name=observation.context,
                instance_name=observation.instance,
                check_token=check_token,
                check_kind=observation.check_kind,
            )
            current_incident_statement = (
                select(LaunchplanePublicIngressIncidentRow)
                .where(
                    LaunchplanePublicIngressIncidentRow.product == observation.product,
                    LaunchplanePublicIngressIncidentRow.context == observation.context,
                    LaunchplanePublicIngressIncidentRow.instance == observation.instance,
                    LaunchplanePublicIngressIncidentRow.check_token == check_token,
                    LaunchplanePublicIngressIncidentRow.check_kind == observation.check_kind,
                    LaunchplanePublicIngressIncidentRow.status == "open",
                )
                .limit(1)
            )
            if not self.database_url.startswith("sqlite"):
                current_incident_statement = current_incident_statement.with_for_update()
            current_incident_row = session.scalar(current_incident_statement)
            current_incident_sha256 = ""
            if current_incident_row is not None:
                try:
                    current_incident_sha256 = public_ingress_incident_record_sha256(
                        PublicIngressIncidentRecord.model_validate(current_incident_row.payload)
                    )
                except ValueError:
                    current_incident_sha256 = "invalid"
            incident_changed = (
                current_incident_row is None and bool(expected_open_incident_id)
            ) or (
                current_incident_row is not None
                and (
                    not expected_open_incident_id
                    or current_incident_row.incident_id != expected_open_incident_id
                    or current_incident_row.state_version != expected_open_incident_state_version
                    or (
                        bool(expected_open_incident_sha256)
                        and current_incident_sha256 != expected_open_incident_sha256
                    )
                )
            )
            if incident_changed:
                unlinked_observation = observation.model_copy(
                    update={"incident_id": "", "incident_event_id": ""}
                )
                observation_row = LaunchplanePublicIngressObservationRow(
                    record_id=observation.record_id,
                    product=observation.product,
                    context=observation.context,
                    instance=observation.instance,
                    status=observation.status,
                    observed_at=observation.observed_at,
                    check_token=check_token,
                    check_kind=observation.check_kind,
                    payload=self._payload_dict(unlinked_observation),
                )
                observation_row.incident_id = ""
                session.merge(observation_row)
                session.commit()
                return PublicIngressTransitionWriteResult(status="incident_changed")
            self._lock_outbox_dedupe_keys(
                session,
                dedupe_keys=tuple(delivery.dedupe_key for delivery in outbox_deliveries),
            )
            session.merge(
                LaunchplanePublicIngressObservationRow(
                    record_id=observation.record_id,
                    product=observation.product,
                    context=observation.context,
                    instance=observation.instance,
                    status=observation.status,
                    observed_at=observation.observed_at,
                    incident_id=observation.incident_id,
                    check_token=check_token,
                    check_kind=observation.check_kind,
                    payload=self._payload_dict(observation),
                )
            )
            for incident in incidents:
                session.merge(
                    LaunchplanePublicIngressIncidentRow(
                        incident_id=incident.incident_id,
                        product=incident.product,
                        context=incident.context,
                        instance=incident.instance,
                        status=incident.status,
                        check_token=canonical_health_check_record_token(incident.check_name),
                        check_kind=incident.check_kind,
                        state_version=incident.state_version,
                        opened_at=incident.opened_at,
                        latest_observed_at=incident.latest_observed_at,
                        payload=self._payload_dict(incident),
                    )
                )
            for event_record in incident_events:
                existing_event = session.get(
                    LaunchplanePublicIngressIncidentEventRow, event_record.event_id
                )
                if existing_event is None:
                    session.add(
                        LaunchplanePublicIngressIncidentEventRow(
                            event_id=event_record.event_id,
                            incident_id=event_record.incident_id,
                            event=event_record.event,
                            occurred_at=event_record.occurred_at,
                            payload=self._payload_dict(event_record),
                        )
                    )
                elif existing_event.payload != self._payload_dict(event_record):
                    raise ValueError(
                        "public ingress incident event identity reused with different payload"
                    )
            for reminder_state in reminder_states:
                session.merge(
                    LaunchplanePublicIngressIncidentReminderRow(
                        reminder_state_id=reminder_state.reminder_state_id,
                        incident_id=reminder_state.incident_id,
                        policy_id=reminder_state.policy_id,
                        status=reminder_state.status,
                        next_reminder_at=reminder_state.next_reminder_at,
                        updated_at=reminder_state.updated_at,
                        payload=self._payload_dict(reminder_state),
                    )
                )
            for delivery in outbox_deliveries:
                existing_delivery = session.scalar(
                    select(LaunchplaneOutboxDeliveryRow)
                    .where(LaunchplaneOutboxDeliveryRow.dedupe_key == delivery.dedupe_key)
                    .limit(1)
                )
                if existing_delivery is None:
                    session.add(self._outbox_delivery_row(delivery))
            session.commit()
            return PublicIngressTransitionWriteResult(status="written")

    def list_public_ingress_incident_records(
        self,
        *,
        product: str = "",
        context_name: str = "",
        instance_name: str = "",
        check_name: str = "",
        check_kind: str = "",
        status: str = "",
        limit: int | None = None,
    ) -> tuple[PublicIngressIncidentRecord, ...]:
        filters: list[object] = []
        if product:
            filters.append(LaunchplanePublicIngressIncidentRow.product == product)
        if context_name:
            filters.append(LaunchplanePublicIngressIncidentRow.context == context_name)
        if instance_name:
            filters.append(LaunchplanePublicIngressIncidentRow.instance == instance_name)
        if check_kind:
            filters.append(LaunchplanePublicIngressIncidentRow.check_kind == check_kind)
        if check_name:
            filters.append(
                LaunchplanePublicIngressIncidentRow.check_token
                == canonical_health_check_record_token(check_name)
            )
        if status:
            filters.append(LaunchplanePublicIngressIncidentRow.status == status)
        records = self._list_models(
            model_type=PublicIngressIncidentRecord,
            orm_model=LaunchplanePublicIngressIncidentRow,
            filters=filters,
            order_by=(
                LaunchplanePublicIngressIncidentRow.opened_at.desc(),
                LaunchplanePublicIngressIncidentRow.incident_id.desc(),
            ),
            limit=limit,
        )
        return records

    def write_public_ingress_notification_policy_record(
        self, record: PublicIngressNotificationPolicyRecord
    ) -> None:
        self._write_row(
            LaunchplanePublicIngressNotificationPolicyRow(
                policy_id=record.policy_id,
                product=record.product,
                context=record.context,
                instance=record.instance,
                status=record.status,
                updated_at=record.updated_at,
                payload=self._payload_dict(record),
            )
        )

    def list_public_ingress_notification_policy_records(
        self,
        *,
        product: str = "",
        context_name: str = "",
        instance_name: str = "",
        status: str = "",
        limit: int | None = None,
    ) -> tuple[PublicIngressNotificationPolicyRecord, ...]:
        filters: list[object] = []
        if product:
            filters.append(LaunchplanePublicIngressNotificationPolicyRow.product.in_(("", product)))
        if context_name:
            filters.append(
                LaunchplanePublicIngressNotificationPolicyRow.context.in_(("", context_name))
            )
        if instance_name:
            filters.append(
                LaunchplanePublicIngressNotificationPolicyRow.instance.in_(("", instance_name))
            )
        if status:
            filters.append(LaunchplanePublicIngressNotificationPolicyRow.status == status)
        return self._list_models(
            model_type=PublicIngressNotificationPolicyRecord,
            orm_model=LaunchplanePublicIngressNotificationPolicyRow,
            filters=filters,
            order_by=(
                LaunchplanePublicIngressNotificationPolicyRow.updated_at.desc(),
                LaunchplanePublicIngressNotificationPolicyRow.policy_id.desc(),
            ),
            limit=limit,
        )

    def write_public_ingress_notification_attempt_record(
        self, record: PublicIngressNotificationAttemptRecord
    ) -> None:
        self._write_row(
            LaunchplanePublicIngressNotificationAttemptRow(
                attempt_id=record.attempt_id,
                incident_id=record.incident_id,
                event=record.event,
                destination_kind=record.destination_kind,
                delivery_status=record.delivery_status,
                attempted_at=record.attempted_at,
                payload=self._payload_dict(record),
            )
        )

    def list_public_ingress_notification_attempt_records(
        self,
        *,
        incident_id: str = "",
        event: str = "",
        destination_kind: str = "",
        limit: int | None = None,
    ) -> tuple[PublicIngressNotificationAttemptRecord, ...]:
        filters: list[object] = []
        if incident_id:
            filters.append(
                LaunchplanePublicIngressNotificationAttemptRow.incident_id == incident_id
            )
        if event:
            filters.append(LaunchplanePublicIngressNotificationAttemptRow.event == event)
        if destination_kind:
            filters.append(
                LaunchplanePublicIngressNotificationAttemptRow.destination_kind == destination_kind
            )
        return self._list_models(
            model_type=PublicIngressNotificationAttemptRecord,
            orm_model=LaunchplanePublicIngressNotificationAttemptRow,
            filters=filters,
            order_by=(
                LaunchplanePublicIngressNotificationAttemptRow.attempted_at.desc(),
                LaunchplanePublicIngressNotificationAttemptRow.attempt_id.desc(),
            ),
            limit=limit,
        )

    def write_dokploy_target_id_record(self, record: DokployTargetIdRecord) -> None:
        self._write_row(self._dokploy_target_id_row(record))

    def read_dokploy_target_id_record(
        self, *, context_name: str, instance_name: str
    ) -> DokployTargetIdRecord:
        return self._read_model(
            model_type=DokployTargetIdRecord,
            orm_model=LaunchplaneDokployTargetIdRow,
            filters=(
                LaunchplaneDokployTargetIdRow.context == context_name,
                LaunchplaneDokployTargetIdRow.instance == instance_name,
            ),
        )

    def list_dokploy_target_id_records(self) -> tuple[DokployTargetIdRecord, ...]:
        return self._list_models(
            model_type=DokployTargetIdRecord,
            orm_model=LaunchplaneDokployTargetIdRow,
            order_by=(
                LaunchplaneDokployTargetIdRow.context.asc(),
                LaunchplaneDokployTargetIdRow.instance.asc(),
            ),
        )

    def delete_dokploy_target_id_record(
        self,
        *,
        expected_record: DokployTargetIdRecord,
    ) -> CurrentAuthorityDeleteStatus:
        statement = (
            select(LaunchplaneDokployTargetIdRow)
            .where(
                LaunchplaneDokployTargetIdRow.context == expected_record.context,
                LaunchplaneDokployTargetIdRow.instance == expected_record.instance,
            )
            .limit(1)
            .with_for_update()
        )
        with self._session_factory() as session:
            row = session.scalar(statement)
            if row is None:
                return "missing"
            current_record = self._read_payload(
                model_type=DokployTargetIdRecord,
                payload=row.payload,
            )
            if self._payload_dict(current_record) != self._payload_dict(expected_record):
                return "changed"
            session.delete(row)
            session.commit()
            return "deleted"

    def write_dokploy_target_record(self, record: DokployTargetRecord) -> None:
        self._write_row(self._dokploy_target_row(record))

    def compare_and_write_dokploy_target_domains(
        self,
        *,
        expected_record: DokployTargetRecord,
        expected_target_id_record: DokployTargetIdRecord,
        expected_provider_target_record: ProviderTargetRecord,
        domains: tuple[str, ...],
        updated_at: str,
        source_label: str,
    ) -> tuple[DokployTargetRecord, ProviderTargetRecord]:
        replacement_record = expected_record.model_copy(
            update={
                "domains": domains,
                "updated_at": updated_at,
                "source_label": source_label,
            }
        )
        replacement_provider_target_record = expected_provider_target_record.model_copy(
            update={
                "updated_at": updated_at,
                "source_label": source_label,
            }
        )
        statement = (
            select(LaunchplaneDokployTargetRow)
            .where(
                LaunchplaneDokployTargetRow.context == expected_record.context,
                LaunchplaneDokployTargetRow.instance == expected_record.instance,
            )
            .limit(1)
        )
        if not self.database_url.startswith("sqlite"):
            statement = statement.with_for_update()
        with self._session_factory() as session:
            row = session.scalar(statement)
            if row is None:
                raise FileNotFoundError(
                    "Dokploy target record was missing during compare-and-write."
                )
            current_record = self._read_payload(
                model_type=DokployTargetRecord,
                payload=_payload_from_row(row),
            )
            if current_record != expected_record:
                raise ValueError("Dokploy target record changed during domain authority repair.")
            target_id_statement = (
                select(LaunchplaneDokployTargetIdRow)
                .where(
                    LaunchplaneDokployTargetIdRow.context == expected_target_id_record.context,
                    LaunchplaneDokployTargetIdRow.instance == expected_target_id_record.instance,
                )
                .limit(1)
            )
            if not self.database_url.startswith("sqlite"):
                target_id_statement = target_id_statement.with_for_update()
            target_id_row = session.scalar(target_id_statement)
            if target_id_row is None:
                raise FileNotFoundError(
                    "Dokploy target-id record was missing during compare-and-write."
                )
            current_target_id_record = self._read_payload(
                model_type=DokployTargetIdRecord,
                payload=_payload_from_row(target_id_row),
            )
            if current_target_id_record != expected_target_id_record:
                raise ValueError("Dokploy target-id record changed during domain authority repair.")
            provider_statement = (
                select(LaunchplaneProviderTargetRow)
                .where(
                    LaunchplaneProviderTargetRow.context == expected_provider_target_record.context,
                    LaunchplaneProviderTargetRow.instance
                    == expected_provider_target_record.instance,
                )
                .limit(1)
            )
            if not self.database_url.startswith("sqlite"):
                provider_statement = provider_statement.with_for_update()
            provider_row = session.scalar(provider_statement)
            if provider_row is None:
                raise FileNotFoundError(
                    "Provider-target record was missing during compare-and-write."
                )
            current_provider_target_record = self._read_payload(
                model_type=ProviderTargetRecord,
                payload=_payload_from_row(provider_row),
            )
            if current_provider_target_record != expected_provider_target_record:
                raise ValueError("Provider-target record changed during domain authority repair.")
            row.updated_at = replacement_record.updated_at
            row.payload = self._payload_dict(replacement_record)
            provider_row.updated_at = replacement_provider_target_record.updated_at
            provider_row.payload = self._payload_dict(replacement_provider_target_record)
            session.commit()
        return replacement_record, replacement_provider_target_record

    def read_dokploy_target_record(
        self, *, context_name: str, instance_name: str
    ) -> DokployTargetRecord:
        return self._read_model(
            model_type=DokployTargetRecord,
            orm_model=LaunchplaneDokployTargetRow,
            filters=(
                LaunchplaneDokployTargetRow.context == context_name,
                LaunchplaneDokployTargetRow.instance == instance_name,
            ),
        )

    def list_dokploy_target_records(self) -> tuple[DokployTargetRecord, ...]:
        return self._list_models(
            model_type=DokployTargetRecord,
            orm_model=LaunchplaneDokployTargetRow,
            order_by=(
                LaunchplaneDokployTargetRow.context.asc(),
                LaunchplaneDokployTargetRow.instance.asc(),
            ),
        )

    def read_provider_target_record(
        self, *, context_name: str, instance_name: str
    ) -> ProviderTargetRecord:
        return self._read_model(
            model_type=ProviderTargetRecord,
            orm_model=LaunchplaneProviderTargetRow,
            filters=(
                LaunchplaneProviderTargetRow.context == context_name,
                LaunchplaneProviderTargetRow.instance == instance_name,
            ),
        )

    def list_provider_target_records(
        self, *, provider_id: str = ""
    ) -> tuple[ProviderTargetRecord, ...]:
        normalized_provider_id = provider_id.strip().lower()
        filters: Sequence[object] = ()
        if normalized_provider_id:
            filters = (LaunchplaneProviderTargetRow.provider_id == normalized_provider_id,)
        return self._list_models(
            model_type=ProviderTargetRecord,
            orm_model=LaunchplaneProviderTargetRow,
            filters=filters,
            order_by=(
                LaunchplaneProviderTargetRow.context.asc(),
                LaunchplaneProviderTargetRow.instance.asc(),
            ),
        )

    def list_physical_provider_target_records(self) -> tuple[ProviderTargetRecord, ...]:
        return self._list_models(
            model_type=ProviderTargetRecord,
            orm_model=LaunchplaneProviderTargetRow,
            order_by=(
                LaunchplaneProviderTargetRow.context.asc(),
                LaunchplaneProviderTargetRow.instance.asc(),
            ),
        )

    def write_provider_target_record(self, record: ProviderTargetRecord) -> None:
        with self._session_factory() as session:
            self._begin_serialized_write(session)
            self._lock_product_authority_bundle_write(session)
            current_row = session.scalar(
                select(LaunchplaneProviderTargetRow)
                .where(
                    LaunchplaneProviderTargetRow.context == record.context,
                    LaunchplaneProviderTargetRow.instance == record.instance,
                )
                .limit(1)
            )
            current_record = (
                self._read_payload(
                    model_type=ProviderTargetRecord,
                    payload=current_row.payload,
                )
                if current_row is not None
                else None
            )
            self._write_provider_target_with_expectation(
                session=session,
                write=ProviderTargetWrite(
                    record=record,
                    expected_record=current_record,
                    expected_absent=current_record is None,
                ),
            )
            session.commit()

    def create_provider_target_record_if_absent(
        self, record: ProviderTargetRecord
    ) -> ProviderTargetCreateStatus:
        with self._session_factory() as session:
            self._begin_serialized_write(session)
            self._lock_product_authority_bundle_write(session)
            current_row = session.scalar(
                select(LaunchplaneProviderTargetRow)
                .where(
                    LaunchplaneProviderTargetRow.context == record.context,
                    LaunchplaneProviderTargetRow.instance == record.instance,
                )
                .limit(1)
            )
            if current_row is not None:
                return "exists"
            self._write_provider_target_with_expectation(
                session=session,
                write=ProviderTargetWrite(record=record, expected_absent=True),
            )
            session.commit()
        return "created"

    def delete_provider_target_record(
        self,
        *,
        expected_record: ProviderTargetRecord,
    ) -> CurrentAuthorityDeleteStatus:
        statement = (
            select(LaunchplaneProviderTargetRow)
            .where(
                LaunchplaneProviderTargetRow.context == expected_record.context,
                LaunchplaneProviderTargetRow.instance == expected_record.instance,
            )
            .limit(1)
            .with_for_update()
        )
        with self._session_factory() as session:
            row = session.scalar(statement)
            if row is None:
                return "missing"
            current_record = self._read_payload(
                model_type=ProviderTargetRecord,
                payload=row.payload,
            )
            if self._payload_dict(current_record) != self._payload_dict(expected_record):
                return "changed"
            session.delete(row)
            session.commit()
            return "deleted"

    def delete_dokploy_target_record(
        self,
        *,
        expected_record: DokployTargetRecord,
    ) -> CurrentAuthorityDeleteStatus:
        statement = (
            select(LaunchplaneDokployTargetRow)
            .where(
                LaunchplaneDokployTargetRow.context == expected_record.context,
                LaunchplaneDokployTargetRow.instance == expected_record.instance,
            )
            .limit(1)
            .with_for_update()
        )
        with self._session_factory() as session:
            row = session.scalar(statement)
            if row is None:
                return "missing"
            current_record = self._read_payload(
                model_type=DokployTargetRecord,
                payload=row.payload,
            )
            if self._payload_dict(current_record) != self._payload_dict(expected_record):
                return "changed"
            session.delete(row)
            session.commit()
            return "deleted"

    def write_runtime_environment_record(self, record: RuntimeEnvironmentRecord) -> None:
        self._write_row(self._runtime_environment_row(record))

    def delete_runtime_environment_record_with_event(
        self,
        *,
        event: RuntimeEnvironmentDeleteEvent,
        expected_record: RuntimeEnvironmentRecord,
    ) -> RuntimeEnvironmentDeleteStatus:
        statement = (
            select(LaunchplaneRuntimeEnvironmentRow)
            .where(
                LaunchplaneRuntimeEnvironmentRow.scope == event.scope,
                LaunchplaneRuntimeEnvironmentRow.context == event.context,
                LaunchplaneRuntimeEnvironmentRow.instance == event.instance,
            )
            .limit(1)
            .with_for_update()
        )
        with self._session_factory() as session:
            row = session.scalar(statement)
            if row is None:
                return "missing"
            current_record = self._read_payload(
                model_type=RuntimeEnvironmentRecord,
                payload=row.payload,
            )
            if self._payload_dict(current_record) != self._payload_dict(expected_record):
                return "changed"
            session.delete(row)
            session.add(
                LaunchplaneRuntimeEnvironmentDeleteEventRow(
                    event_id=event.event_id,
                    scope=event.scope,
                    context=event.context,
                    instance=event.instance,
                    recorded_at=event.recorded_at,
                    payload=self._payload_dict(event),
                )
            )
            session.commit()
            return "deleted"

    def write_runtime_environment_delete_event(self, event: RuntimeEnvironmentDeleteEvent) -> None:
        self._write_row(self._runtime_environment_delete_event_row(event))

    def list_runtime_environment_delete_events(
        self,
        *,
        scope: str = "",
        context_name: str = "",
        instance_name: str = "",
    ) -> tuple[RuntimeEnvironmentDeleteEvent, ...]:
        filters: list[object] = []
        if scope:
            filters.append(LaunchplaneRuntimeEnvironmentDeleteEventRow.scope == scope)
        if context_name:
            filters.append(LaunchplaneRuntimeEnvironmentDeleteEventRow.context == context_name)
        if instance_name:
            filters.append(LaunchplaneRuntimeEnvironmentDeleteEventRow.instance == instance_name)
        return self._list_models(
            model_type=RuntimeEnvironmentDeleteEvent,
            orm_model=LaunchplaneRuntimeEnvironmentDeleteEventRow,
            filters=filters,
            order_by=(
                LaunchplaneRuntimeEnvironmentDeleteEventRow.recorded_at.desc(),
                LaunchplaneRuntimeEnvironmentDeleteEventRow.event_id.desc(),
            ),
        )

    def list_runtime_environment_records(
        self,
        *,
        scope: str = "",
        context_name: str = "",
        instance_name: str = "",
    ) -> tuple[RuntimeEnvironmentRecord, ...]:
        filters: list[object] = []
        if scope:
            filters.append(LaunchplaneRuntimeEnvironmentRow.scope == scope)
        if context_name:
            filters.append(LaunchplaneRuntimeEnvironmentRow.context == context_name)
        if instance_name:
            filters.append(LaunchplaneRuntimeEnvironmentRow.instance == instance_name)
        return self._list_models(
            model_type=RuntimeEnvironmentRecord,
            orm_model=LaunchplaneRuntimeEnvironmentRow,
            filters=filters,
            order_by=(
                LaunchplaneRuntimeEnvironmentRow.scope.asc(),
                LaunchplaneRuntimeEnvironmentRow.context.asc(),
                LaunchplaneRuntimeEnvironmentRow.instance.asc(),
            ),
        )

    def write_runtime_key_safety_policy_record(self, record: RuntimeKeySafetyPolicyRecord) -> None:
        self._write_row(
            LaunchplaneRuntimeKeySafetyPolicyRow(
                record_id=record.record_id,
                status=record.status,
                source=record.source,
                updated_at=record.updated_at,
                payload=self._payload_dict(record),
            )
        )

    def list_runtime_key_safety_policy_records(
        self,
        *,
        status: str = "",
        limit: int | None = None,
    ) -> tuple[RuntimeKeySafetyPolicyRecord, ...]:
        filters: list[object] = []
        if status:
            filters.append(LaunchplaneRuntimeKeySafetyPolicyRow.status == status)
        return self._list_models(
            model_type=RuntimeKeySafetyPolicyRecord,
            orm_model=LaunchplaneRuntimeKeySafetyPolicyRow,
            filters=filters,
            order_by=(
                LaunchplaneRuntimeKeySafetyPolicyRow.updated_at.desc(),
                LaunchplaneRuntimeKeySafetyPolicyRow.record_id.desc(),
            ),
            limit=limit,
        )

    def read_lane_summary(self, *, context_name: str, instance_name: str) -> LaunchplaneLaneSummary:
        with self._product_authority_bundle_read_guard():
            return self._read_lane_summary_unlocked(
                context_name=context_name,
                instance_name=instance_name,
            )

    def _read_lane_summary_unlocked(
        self, *, context_name: str, instance_name: str
    ) -> LaunchplaneLaneSummary:
        runtime_environment_records = (
            *self.list_runtime_environment_records(scope="global"),
            *self.list_runtime_environment_records(scope="context", context_name=context_name),
            *self.list_runtime_environment_records(
                scope="instance",
                context_name=context_name,
                instance_name=instance_name,
            ),
        )
        inventory = self._read_optional_model(
            model_type=EnvironmentInventory,
            orm_model=LaunchplaneInventoryRow,
            filters=(
                LaunchplaneInventoryRow.context == context_name,
                LaunchplaneInventoryRow.instance == instance_name,
            ),
        )
        latest_deployment = next(
            iter(
                self.list_deployment_records(
                    context_name=context_name,
                    instance_name=instance_name,
                    limit=1,
                )
            ),
            None,
        )
        artifact_manifest = self._read_optional_artifact_manifest(
            inventory=inventory,
            latest_deployment=latest_deployment,
        )
        return LaunchplaneLaneSummary(
            context=context_name,
            instance=instance_name,
            inventory=inventory,
            artifact_manifest=artifact_manifest,
            release_tuple=self._read_optional_model(
                model_type=ReleaseTupleRecord,
                orm_model=LaunchplaneReleaseTupleRow,
                filters=(
                    LaunchplaneReleaseTupleRow.context == context_name,
                    LaunchplaneReleaseTupleRow.channel == instance_name,
                ),
            ),
            latest_deployment=latest_deployment,
            latest_promotion=next(
                iter(
                    self.list_promotion_records(
                        context_name=context_name,
                        to_instance_name=instance_name,
                        limit=1,
                    )
                ),
                None,
            ),
            latest_backup_gate=next(
                iter(
                    self.list_backup_gate_records(
                        context_name=context_name,
                        instance_name=instance_name,
                        limit=1,
                    )
                ),
                None,
            ),
            provider_target=self._read_optional_physical_provider_target_record(
                context_name=context_name,
                instance_name=instance_name,
            ),
            dokploy_target_id=self._read_optional_model(
                model_type=DokployTargetIdRecord,
                orm_model=LaunchplaneDokployTargetIdRow,
                filters=(
                    LaunchplaneDokployTargetIdRow.context == context_name,
                    LaunchplaneDokployTargetIdRow.instance == instance_name,
                ),
            ),
            dokploy_target=self._read_optional_model(
                model_type=DokployTargetRecord,
                orm_model=LaunchplaneDokployTargetRow,
                filters=(
                    LaunchplaneDokployTargetRow.context == context_name,
                    LaunchplaneDokployTargetRow.instance == instance_name,
                ),
            ),
            runtime_environment_records=runtime_environment_records,
            odoo_instance_override=self._read_optional_model(
                model_type=OdooInstanceOverrideRecord,
                orm_model=LaunchplaneOdooInstanceOverrideRow,
                filters=(
                    LaunchplaneOdooInstanceOverrideRow.context == context_name,
                    LaunchplaneOdooInstanceOverrideRow.instance == instance_name,
                ),
            ),
            secret_bindings=self.list_secret_bindings(
                context_name=context_name,
                instance_name=instance_name,
            ),
        )

    def _read_optional_physical_provider_target_record(
        self, *, context_name: str, instance_name: str
    ) -> ProviderTargetRecord | None:
        return self._read_optional_model(
            model_type=ProviderTargetRecord,
            orm_model=LaunchplaneProviderTargetRow,
            filters=(
                LaunchplaneProviderTargetRow.context == context_name,
                LaunchplaneProviderTargetRow.instance == instance_name,
            ),
        )

    def _read_optional_artifact_manifest(
        self,
        *,
        inventory: EnvironmentInventory | None,
        latest_deployment: DeploymentRecord | None,
    ) -> ArtifactIdentityManifest | None:
        artifact_id = _artifact_id_for_lane(
            inventory=inventory,
            latest_deployment=latest_deployment,
        )
        if not artifact_id:
            return None
        try:
            return self.read_artifact_manifest(artifact_id)
        except FileNotFoundError:
            return None

    def write_odoo_instance_override_record(self, record: OdooInstanceOverrideRecord) -> None:
        self._write_row(
            LaunchplaneOdooInstanceOverrideRow(
                context=record.context,
                instance=record.instance,
                updated_at=record.updated_at,
                payload=self._payload_dict(record),
            )
        )

    def read_odoo_instance_override_record(
        self, *, context_name: str, instance_name: str
    ) -> OdooInstanceOverrideRecord:
        return self._read_model(
            model_type=OdooInstanceOverrideRecord,
            orm_model=LaunchplaneOdooInstanceOverrideRow,
            filters=(
                LaunchplaneOdooInstanceOverrideRow.context == context_name,
                LaunchplaneOdooInstanceOverrideRow.instance == instance_name,
            ),
        )

    def list_odoo_instance_override_records(self) -> tuple[OdooInstanceOverrideRecord, ...]:
        return self._list_models(
            model_type=OdooInstanceOverrideRecord,
            orm_model=LaunchplaneOdooInstanceOverrideRow,
            order_by=(
                LaunchplaneOdooInstanceOverrideRow.context.asc(),
                LaunchplaneOdooInstanceOverrideRow.instance.asc(),
            ),
        )

    def write_secret_record(self, record: SecretRecord) -> None:
        self._write_row(self._secret_row(record))

    def read_secret_record(self, secret_id: str) -> SecretRecord:
        return self._read_model(
            model_type=SecretRecord,
            orm_model=LaunchplaneSecretRow,
            filters=(LaunchplaneSecretRow.secret_id == secret_id,),
        )

    def find_secret_record(
        self,
        *,
        scope: str,
        integration: str,
        name: str,
        context: str = "",
        instance: str = "",
    ) -> SecretRecord | None:
        records = self._list_models(
            model_type=SecretRecord,
            orm_model=LaunchplaneSecretRow,
            filters=(
                LaunchplaneSecretRow.scope == scope,
                LaunchplaneSecretRow.integration == integration,
                LaunchplaneSecretRow.name == name,
                LaunchplaneSecretRow.context == context,
                LaunchplaneSecretRow.instance == instance,
            ),
            order_by=(
                LaunchplaneSecretRow.updated_at.desc(),
                LaunchplaneSecretRow.secret_id.desc(),
            ),
            limit=1,
        )
        return records[0] if records else None

    def list_secret_records(
        self,
        *,
        integration: str = "",
        context_name: str = "",
        instance_name: str = "",
        limit: int | None = None,
    ) -> tuple[SecretRecord, ...]:
        filters: list[object] = []
        if integration:
            filters.append(LaunchplaneSecretRow.integration == integration)
        if context_name:
            filters.append(LaunchplaneSecretRow.context == context_name)
        if instance_name:
            filters.append(LaunchplaneSecretRow.instance == instance_name)
        return self._list_models(
            model_type=SecretRecord,
            orm_model=LaunchplaneSecretRow,
            filters=filters,
            order_by=(
                LaunchplaneSecretRow.updated_at.desc(),
                LaunchplaneSecretRow.secret_id.desc(),
            ),
            limit=limit,
        )

    def write_secret_version(self, version: SecretVersion) -> None:
        self._write_row(self._secret_version_row(version))

    def read_secret_version(self, version_id: str) -> SecretVersion:
        return self._read_model(
            model_type=SecretVersion,
            orm_model=LaunchplaneSecretVersionRow,
            filters=(LaunchplaneSecretVersionRow.version_id == version_id,),
        )

    def list_secret_versions(self, *, secret_id: str) -> tuple[SecretVersion, ...]:
        return self._list_models(
            model_type=SecretVersion,
            orm_model=LaunchplaneSecretVersionRow,
            filters=(LaunchplaneSecretVersionRow.secret_id == secret_id,),
            order_by=(
                LaunchplaneSecretVersionRow.created_at.desc(),
                LaunchplaneSecretVersionRow.version_id.desc(),
            ),
        )

    def write_secret_binding(self, binding: SecretBinding) -> None:
        self._write_row(self._secret_binding_row(binding))

    def list_secret_bindings(
        self,
        *,
        integration: str = "",
        context_name: str = "",
        instance_name: str = "",
        limit: int | None = None,
    ) -> tuple[SecretBinding, ...]:
        filters: list[object] = []
        if integration:
            filters.append(LaunchplaneSecretBindingRow.integration == integration)
        if context_name:
            filters.append(LaunchplaneSecretBindingRow.context == context_name)
        if instance_name:
            filters.append(LaunchplaneSecretBindingRow.instance == instance_name)
        return self._list_models(
            model_type=SecretBinding,
            orm_model=LaunchplaneSecretBindingRow,
            filters=filters,
            order_by=(
                LaunchplaneSecretBindingRow.updated_at.desc(),
                LaunchplaneSecretBindingRow.binding_id.desc(),
            ),
            limit=limit,
        )

    def write_secret_audit_event(self, event: SecretAuditEvent) -> None:
        self._write_row(self._secret_audit_event_row(event))

    def write_secret_rotations(
        self,
        rotations: tuple[SecretRotationWrite, ...],
        *,
        idempotency_record: LaunchplaneIdempotencyRecord | None = None,
    ) -> None:
        ordered_rotations = tuple(sorted(rotations, key=lambda item: item.record.secret_id))
        with self._session_factory() as session:
            self._begin_serialized_write(session)
            for rotation in ordered_rotations:
                statement = (
                    select(LaunchplaneSecretRow)
                    .where(LaunchplaneSecretRow.secret_id == rotation.record.secret_id)
                    .with_for_update()
                )
                current_row = session.scalar(statement)
                if current_row is None:
                    raise FileNotFoundError(
                        f"No Launchplane secret record found for {rotation.record.secret_id!r}"
                    )
                if current_row.current_version_id != rotation.expected_current_version_id:
                    raise ValueError("Managed secret changed after rotation preflight.")
            for rotation in ordered_rotations:
                version = rotation.version
                record = rotation.record
                event = rotation.audit_event
                session.merge(self._secret_version_row(version))
                self._after_product_authority_bundle_step("write_secret_rotation_version")
                session.merge(self._secret_row(record))
                self._after_product_authority_bundle_step("write_secret_rotation_record")
                session.merge(self._secret_audit_event_row(event))
                self._after_product_authority_bundle_step("write_secret_rotation_audit")
            if idempotency_record is not None:
                session.merge(self._idempotency_row(idempotency_record))
                self._after_product_authority_bundle_step("write_idempotency")
            session.commit()

    def list_secret_audit_events(self, *, secret_id: str) -> tuple[SecretAuditEvent, ...]:
        return self._list_models(
            model_type=SecretAuditEvent,
            orm_model=LaunchplaneSecretAuditEventRow,
            filters=(LaunchplaneSecretAuditEventRow.secret_id == secret_id,),
            order_by=(
                LaunchplaneSecretAuditEventRow.recorded_at.desc(),
                LaunchplaneSecretAuditEventRow.event_id.desc(),
            ),
        )

    def compare_and_write_product_owner_policy_record(
        self,
        record: ProductOwnerPolicyRecord,
        *,
        expected_current_record_id: str,
        expected_current_policy_digest: str,
    ) -> Literal["written", "replayed"]:
        return self._compare_and_write_product_owner_revision_record(
            model_type=ProductOwnerPolicyRecord,
            row_type=LaunchplaneProductOwnerPolicyRow,
            row_factory=self._product_owner_policy_row,
            record=record,
            revision_field="policy_revision",
            digest_field="policy_digest",
            expected_current_record_id=expected_current_record_id,
            expected_current_digest=expected_current_policy_digest,
            sequence_error=ProductOwnerPolicySequenceError,
            conflict_error=ProductOwnerPolicyConflictError,
            label="product Owner policy",
        )

    def write_product_owner_policy_record(
        self,
        record: ProductOwnerPolicyRecord,
    ) -> Literal["written", "replayed"]:
        current = self.list_product_owner_policy_records(
            product=record.product,
            system=record.system,
            status="active",
            limit=1,
        )
        return self.compare_and_write_product_owner_policy_record(
            record,
            expected_current_record_id=current[0].record_id if current else "",
            expected_current_policy_digest=current[0].policy_digest if current else "",
        )

    def read_product_owner_policy_record(self, record_id: str) -> ProductOwnerPolicyRecord:
        return self._read_model(
            model_type=ProductOwnerPolicyRecord,
            orm_model=LaunchplaneProductOwnerPolicyRow,
            filters=(LaunchplaneProductOwnerPolicyRow.record_id == record_id,),
        )

    def list_product_owner_policy_records(
        self,
        *,
        product: str = "",
        system: str = "",
        status: str = "",
        limit: int | None = None,
    ) -> tuple[ProductOwnerPolicyRecord, ...]:
        filters: list[object] = []
        if product:
            filters.append(LaunchplaneProductOwnerPolicyRow.product == product)
        if system:
            filters.append(LaunchplaneProductOwnerPolicyRow.system == system)
        if status:
            filters.append(LaunchplaneProductOwnerPolicyRow.status == status)
        return self._list_models(
            model_type=ProductOwnerPolicyRecord,
            orm_model=LaunchplaneProductOwnerPolicyRow,
            filters=filters,
            order_by=(
                LaunchplaneProductOwnerPolicyRow.policy_revision.desc(),
                LaunchplaneProductOwnerPolicyRow.product.desc(),
                LaunchplaneProductOwnerPolicyRow.system.desc(),
                LaunchplaneProductOwnerPolicyRow.record_id.desc(),
            ),
            limit=limit,
        )

    def compare_and_write_product_owner_requirement_record(
        self,
        record: ProductOwnerRequirementRecord,
        *,
        expected_current_record_id: str,
        expected_current_requirement_digest: str,
    ) -> Literal["written", "replayed"]:
        return self._compare_and_write_product_owner_revision_record(
            model_type=ProductOwnerRequirementRecord,
            row_type=LaunchplaneProductOwnerRequirementRow,
            row_factory=self._product_owner_requirement_row,
            record=record,
            revision_field="requirement_revision",
            digest_field="requirement_digest",
            expected_current_record_id=expected_current_record_id,
            expected_current_digest=expected_current_requirement_digest,
            sequence_error=ProductOwnerRequirementSequenceError,
            conflict_error=ProductOwnerRequirementConflictError,
            label="product Owner requirement",
        )

    def write_product_owner_requirement_record(
        self,
        record: ProductOwnerRequirementRecord,
    ) -> Literal["written", "replayed"]:
        current = self.list_product_owner_requirement_records(
            product=record.product,
            system=record.system,
            status="active",
            limit=1,
        )
        return self.compare_and_write_product_owner_requirement_record(
            record,
            expected_current_record_id=current[0].record_id if current else "",
            expected_current_requirement_digest=(current[0].requirement_digest if current else ""),
        )

    def read_product_owner_requirement_record(
        self,
        record_id: str,
    ) -> ProductOwnerRequirementRecord:
        return self._read_model(
            model_type=ProductOwnerRequirementRecord,
            orm_model=LaunchplaneProductOwnerRequirementRow,
            filters=(LaunchplaneProductOwnerRequirementRow.record_id == record_id,),
        )

    def list_product_owner_requirement_records(
        self,
        *,
        product: str = "",
        system: str = "",
        status: str = "",
        limit: int | None = None,
    ) -> tuple[ProductOwnerRequirementRecord, ...]:
        filters: list[object] = []
        if product:
            filters.append(LaunchplaneProductOwnerRequirementRow.product == product)
        if system:
            filters.append(LaunchplaneProductOwnerRequirementRow.system == system)
        if status:
            filters.append(LaunchplaneProductOwnerRequirementRow.status == status)
        return self._list_models(
            model_type=ProductOwnerRequirementRecord,
            orm_model=LaunchplaneProductOwnerRequirementRow,
            filters=filters,
            order_by=(
                LaunchplaneProductOwnerRequirementRow.requirement_revision.desc(),
                LaunchplaneProductOwnerRequirementRow.product.desc(),
                LaunchplaneProductOwnerRequirementRow.system.desc(),
                LaunchplaneProductOwnerRequirementRow.record_id.desc(),
            ),
            limit=limit,
        )

    def compare_and_write_product_owner_routing_record(
        self,
        record: ProductOwnerRoutingRecord,
        *,
        expected_current_record_id: str,
        expected_current_routing_digest: str,
    ) -> Literal["written", "replayed"]:
        return self._compare_and_write_product_owner_revision_record(
            model_type=ProductOwnerRoutingRecord,
            row_type=LaunchplaneProductOwnerRoutingRow,
            row_factory=self._product_owner_routing_row,
            record=record,
            revision_field="routing_revision",
            digest_field="routing_digest",
            expected_current_record_id=expected_current_record_id,
            expected_current_digest=expected_current_routing_digest,
            sequence_error=ProductOwnerRoutingSequenceError,
            conflict_error=ProductOwnerRoutingConflictError,
            label="product Owner routing",
        )

    def write_product_owner_routing_record(
        self,
        record: ProductOwnerRoutingRecord,
    ) -> Literal["written", "replayed"]:
        current = self.list_product_owner_routing_records(
            product=record.product,
            system=record.system,
            status="active",
            limit=1,
        )
        return self.compare_and_write_product_owner_routing_record(
            record,
            expected_current_record_id=current[0].record_id if current else "",
            expected_current_routing_digest=current[0].routing_digest if current else "",
        )

    def read_product_owner_routing_record(self, record_id: str) -> ProductOwnerRoutingRecord:
        return self._read_model(
            model_type=ProductOwnerRoutingRecord,
            orm_model=LaunchplaneProductOwnerRoutingRow,
            filters=(LaunchplaneProductOwnerRoutingRow.record_id == record_id,),
        )

    def list_product_owner_routing_records(
        self,
        *,
        product: str = "",
        system: str = "",
        status: str = "",
        limit: int | None = None,
    ) -> tuple[ProductOwnerRoutingRecord, ...]:
        filters: list[object] = []
        if product:
            filters.append(LaunchplaneProductOwnerRoutingRow.product == product)
        if system:
            filters.append(LaunchplaneProductOwnerRoutingRow.system == system)
        if status:
            filters.append(LaunchplaneProductOwnerRoutingRow.status == status)
        return self._list_models(
            model_type=ProductOwnerRoutingRecord,
            orm_model=LaunchplaneProductOwnerRoutingRow,
            filters=filters,
            order_by=(
                LaunchplaneProductOwnerRoutingRow.routing_revision.desc(),
                LaunchplaneProductOwnerRoutingRow.product.desc(),
                LaunchplaneProductOwnerRoutingRow.system.desc(),
                LaunchplaneProductOwnerRoutingRow.record_id.desc(),
            ),
            limit=limit,
        )

    def compare_and_write_change_impact_policy_record(
        self,
        record: ChangeImpactPolicyRecord,
        *,
        expected_current_record_id: str,
        expected_current_policy_digest: str,
    ) -> Literal["written", "replayed"]:
        return self._compare_and_write_change_impact_policy_record(
            record,
            audit=None,
            expected_current_record_id=expected_current_record_id,
            expected_current_policy_digest=expected_current_policy_digest,
        ).status

    def compare_and_write_change_impact_policy_record_with_audit(
        self,
        record: ChangeImpactPolicyRecord,
        *,
        audit: ChangeImpactPolicyAuditRecord,
        expected_current_record_id: str,
        expected_current_policy_digest: str,
    ) -> ChangeImpactPolicyAuditedWriteResult:
        validated = ChangeImpactPolicyAuditRecord.model_validate(audit.model_dump(mode="json"))
        if (
            validated.record_id != record.record_id
            or validated.policy_digest != record.policy_digest
        ):
            raise ChangeImpactPolicyConflictError(
                "Policy audit does not match the policy identity."
            )
        return self._compare_and_write_change_impact_policy_record(
            record,
            audit=validated,
            expected_current_record_id=expected_current_record_id,
            expected_current_policy_digest=expected_current_policy_digest,
        )

    def _compare_and_write_change_impact_policy_record(
        self,
        record: ChangeImpactPolicyRecord,
        *,
        audit: ChangeImpactPolicyAuditRecord | None,
        expected_current_record_id: str,
        expected_current_policy_digest: str,
    ) -> ChangeImpactPolicyAuditedWriteResult:
        with self._session_factory() as session:
            self._begin_serialized_write(session)
            try:
                ChangeImpactPolicyRecord.model_validate(record.model_dump())
            except ValueError as error:
                raise ChangeImpactPolicyConflictError(
                    "Incoming change impact policy payload does not match its derived identifiers."
                ) from error
            if record.status != "active":
                raise ChangeImpactPolicySequenceError(
                    "Incoming change impact policy revision must have active status."
                )
            if record.effective_at > _utc_now_timestamp():
                raise ChangeImpactPolicySequenceError(
                    "Incoming active change impact policy revision cannot take effect in the future."
                )
            if self.database_dialect_name == "postgresql":
                session.execute(
                    text("select pg_advisory_xact_lock(hashtextextended(:lock_name, 0))"),
                    {"lock_name": f"change-impact:{record.repository_id}"},
                )
            statement = (
                select(LaunchplaneChangeImpactPolicyRow)
                .where(LaunchplaneChangeImpactPolicyRow.repository_id == record.repository_id)
                .order_by(LaunchplaneChangeImpactPolicyRow.policy_revision.desc())
            )
            if self.database_dialect_name == "postgresql":
                statement = statement.with_for_update()
            rows = tuple(session.scalars(statement).all())
            records = tuple(
                self._read_payload(model_type=ChangeImpactPolicyRecord, payload=row.payload)
                for row in rows
            )
            same_id = tuple(
                existing for existing in records if existing.record_id == record.record_id
            )
            if same_id:
                if len(same_id) != 1 or same_id[0].policy_digest != record.policy_digest:
                    raise ChangeImpactPolicyConflictError(
                        "change impact policy record id conflicts with history."
                    )
                if same_id[0].status != record.status:
                    raise ChangeImpactPolicyConflictError(
                        "change impact policy replay status conflicts with history."
                    )
                original_row = next(row for row in rows if row.record_id == record.record_id)
                original_audit = self._change_impact_audit_from_row(original_row)
                session.commit()
                return ChangeImpactPolicyAuditedWriteResult(status="replayed", audit=original_audit)
            current_records = tuple(existing for existing in records if existing.status == "active")
            if len(current_records) > 1:
                raise ChangeImpactPolicySequenceError(
                    "change impact policy history has multiple active revisions."
                )
            current = current_records[0] if current_records else None
            if current is None:
                if record.policy_revision != 1:
                    raise ChangeImpactPolicySequenceError(
                        "Initial change impact policy revision must be 1."
                    )
                if expected_current_record_id.strip() or expected_current_policy_digest.strip():
                    raise ChangeImpactPolicyConflictError(
                        "Initial change impact policy expected tip must be absent."
                    )
            else:
                if (
                    expected_current_record_id.strip() != current.record_id
                    or expected_current_policy_digest.strip().lower() != current.policy_digest
                ):
                    raise ChangeImpactPolicyConflictError(
                        "Expected current change impact policy tip is stale."
                    )
                if record.policy_revision != current.policy_revision + 1:
                    raise ChangeImpactPolicySequenceError(
                        "Next change impact policy revision is not linear."
                    )
                if record.supersedes_record_id != current.record_id:
                    raise ChangeImpactPolicySequenceError(
                        "Next change impact policy must supersede the current record."
                    )
                if record.effective_at < current.effective_at:
                    raise ChangeImpactPolicySequenceError(
                        "Next change impact policy effective_at cannot precede current revision."
                    )
                current_row = next(row for row in rows if row.record_id == current.record_id)
                current_row.status = "superseded"
                current_row.payload = self._payload_dict(
                    current.model_copy(update={"status": "superseded"})
                )
                session.flush()
            row = self._change_impact_policy_row(record)
            row.audit_payload = self._payload_dict(audit) if audit is not None else None
            session.add(row)
            session.commit()
            return ChangeImpactPolicyAuditedWriteResult(status="written", audit=audit)

    def read_change_impact_policy_audit(
        self, record_id: str
    ) -> ChangeImpactPolicyAuditRecord | None:
        with self._session_factory() as session:
            row = session.get(LaunchplaneChangeImpactPolicyRow, record_id)
            if row is None:
                raise FileNotFoundError(record_id)
            return self._change_impact_audit_from_row(row)

    @staticmethod
    def _change_impact_audit_from_row(
        row: LaunchplaneChangeImpactPolicyRow,
    ) -> ChangeImpactPolicyAuditRecord | None:
        if row.audit_payload is None:
            return None
        audit = ChangeImpactPolicyAuditRecord.model_validate(row.audit_payload)
        if audit.record_id != row.record_id or audit.policy_digest != row.policy_digest:
            raise ChangeImpactPolicyConflictError("Stored policy audit identity is inconsistent.")
        return audit

    def write_change_impact_policy_record(
        self,
        record: ChangeImpactPolicyRecord,
    ) -> Literal["written", "replayed"]:
        current = self.list_change_impact_policy_records(
            repository_id=record.repository_id,
            status="active",
            limit=1,
        )
        return self.compare_and_write_change_impact_policy_record(
            record,
            expected_current_record_id=current[0].record_id if current else "",
            expected_current_policy_digest=current[0].policy_digest if current else "",
        )

    def read_change_impact_policy_record(self, record_id: str) -> ChangeImpactPolicyRecord:
        return self._read_model(
            model_type=ChangeImpactPolicyRecord,
            orm_model=LaunchplaneChangeImpactPolicyRow,
            filters=(LaunchplaneChangeImpactPolicyRow.record_id == record_id,),
        )

    def list_change_impact_policy_records(
        self,
        *,
        repository_id: str = "",
        repository: str = "",
        status: str = "",
        limit: int | None = None,
    ) -> tuple[ChangeImpactPolicyRecord, ...]:
        filters: list[object] = []
        if repository_id:
            filters.append(LaunchplaneChangeImpactPolicyRow.repository_id == repository_id)
        if repository:
            filters.append(
                LaunchplaneChangeImpactPolicyRow.repository == repository.strip().lower()
            )
        if status:
            filters.append(LaunchplaneChangeImpactPolicyRow.status == status)
        return self._list_models(
            model_type=ChangeImpactPolicyRecord,
            orm_model=LaunchplaneChangeImpactPolicyRow,
            filters=filters,
            order_by=(
                LaunchplaneChangeImpactPolicyRow.policy_revision.desc(),
                LaunchplaneChangeImpactPolicyRow.repository_id.desc(),
                LaunchplaneChangeImpactPolicyRow.record_id.desc(),
            ),
            limit=limit,
        )

    def _product_owner_policy_row(
        self,
        record: ProductOwnerPolicyRecord,
    ) -> LaunchplaneProductOwnerPolicyRow:
        return LaunchplaneProductOwnerPolicyRow(
            record_id=record.record_id,
            product=record.product,
            system=record.system,
            status=record.status,
            policy_revision=record.policy_revision,
            quorum=record.quorum,
            effective_at=record.effective_at,
            source=record.source,
            supersedes_record_id=record.supersedes_record_id,
            policy_digest=record.policy_digest,
            payload=self._payload_dict(record),
        )

    def _product_owner_requirement_row(
        self,
        record: ProductOwnerRequirementRecord,
    ) -> LaunchplaneProductOwnerRequirementRow:
        return LaunchplaneProductOwnerRequirementRow(
            record_id=record.record_id,
            product=record.product,
            system=record.system,
            status=record.status,
            requirement_revision=record.requirement_revision,
            effective_at=record.effective_at,
            source=record.source,
            supersedes_record_id=record.supersedes_record_id,
            requirement_digest=record.requirement_digest,
            payload=self._payload_dict(record),
        )

    def _product_owner_routing_row(
        self,
        record: ProductOwnerRoutingRecord,
    ) -> LaunchplaneProductOwnerRoutingRow:
        return LaunchplaneProductOwnerRoutingRow(
            record_id=record.record_id,
            product=record.product,
            system=record.system,
            status=record.status,
            routing_revision=record.routing_revision,
            authoritative=record.authoritative,
            effective_at=record.effective_at,
            source=record.source,
            supersedes_record_id=record.supersedes_record_id,
            routing_digest=record.routing_digest,
            payload=self._payload_dict(record),
        )

    def _change_impact_policy_row(
        self,
        record: ChangeImpactPolicyRecord,
    ) -> LaunchplaneChangeImpactPolicyRow:
        return LaunchplaneChangeImpactPolicyRow(
            record_id=record.record_id,
            repository_id=record.repository_id,
            repository_owner_id=record.repository_owner_id,
            repository=record.repository,
            status=record.status,
            policy_revision=record.policy_revision,
            default_unknown_review_tier=record.default_unknown_review_tier,
            effective_at=record.effective_at,
            source=record.source,
            supersedes_record_id=record.supersedes_record_id,
            policy_digest=record.policy_digest,
            payload=self._payload_dict(record),
        )

    def _compare_and_write_product_owner_revision_record(
        self,
        *,
        model_type: type[Any],
        row_type: type[Any],
        row_factory: Callable[[Any], Any],
        record: Any,
        revision_field: str,
        digest_field: str,
        expected_current_record_id: str,
        expected_current_digest: str,
        sequence_error: type[ValueError],
        conflict_error: type[ValueError],
        label: str,
    ) -> Literal["written", "replayed"]:
        model_class: Any = model_type
        row_class: Any = row_type
        with self._session_factory() as session:
            self._begin_serialized_write(session)
            try:
                model_class.model_validate(record.model_dump())
            except ValueError as error:
                raise conflict_error(
                    f"Incoming {label} payload does not match its derived identifiers."
                ) from error
            if record.status != "active":
                raise sequence_error(f"Incoming {label} revision must have active status.")
            if record.effective_at > _utc_now_timestamp():
                raise sequence_error(
                    f"Incoming active {label} revision cannot take effect in the future."
                )
            if self.database_dialect_name == "postgresql":
                session.execute(
                    text("select pg_advisory_xact_lock(hashtextextended(:lock_name, 0))"),
                    {
                        "lock_name": (
                            f"product-owner:{row_class.__tablename__}:"
                            f"{record.product}:{record.system}"
                        )
                    },
                )
            statement = (
                select(row_class)
                .where(row_class.product == record.product, row_class.system == record.system)
                .order_by(getattr(row_class, revision_field).desc())
            )
            if self.database_dialect_name == "postgresql":
                statement = statement.with_for_update()
            rows = tuple(session.scalars(statement).all())
            records = tuple(model_class.model_validate(row.payload) for row in rows)
            same_id = tuple(
                existing for existing in records if existing.record_id == record.record_id
            )
            if same_id:
                if len(same_id) != 1 or getattr(same_id[0], digest_field) != getattr(
                    record,
                    digest_field,
                ):
                    raise conflict_error(f"{label} record id conflicts with history.")
                session.commit()
                return "replayed"
            current_records = tuple(existing for existing in records if existing.status == "active")
            if len(current_records) > 1:
                raise sequence_error(f"{label} history has multiple active revisions.")
            current = current_records[0] if current_records else None
            revision = int(getattr(record, revision_field))
            if current is None:
                if revision != 1:
                    raise sequence_error(f"Initial {label} revision must be 1.")
                if expected_current_record_id.strip() or expected_current_digest.strip():
                    raise conflict_error(f"Initial {label} expected tip must be absent.")
            else:
                if (
                    expected_current_record_id.strip() != current.record_id
                    or expected_current_digest.strip().lower() != getattr(current, digest_field)
                ):
                    raise conflict_error(f"Expected current {label} tip is stale.")
                if revision != int(getattr(current, revision_field)) + 1:
                    raise sequence_error(f"Next {label} revision is not linear.")
                if record.supersedes_record_id != current.record_id:
                    raise sequence_error(f"Next {label} must supersede the current record.")
                if record.effective_at < current.effective_at:
                    raise sequence_error(
                        f"Next {label} effective_at cannot precede the current revision."
                    )
                current_row = next(row for row in rows if row.record_id == current.record_id)
                superseded = current.model_copy(update={"status": "superseded"})
                current_row.status = "superseded"
                current_row.payload = self._payload_dict(superseded)
                session.flush()
            session.add(row_factory(record))
            session.flush()
            session.commit()
            return "written"

    def import_core_records_from_filesystem(
        self, filesystem_store: FilesystemRecordStore
    ) -> dict[str, int]:
        counts = {
            "artifacts": 0,
            "authz_policies": 0,
            "backup_gates": 0,
            "deployments": 0,
            "promotions": 0,
            "inventory": 0,
            "odoo_instance_overrides": 0,
            "product_profiles": 0,
            "change_impact_policies": 0,
            "product_owner_policies": 0,
            "product_owner_requirements": 0,
            "product_owner_routing": 0,
            "preview_records": 0,
            "preview_enablement": 0,
            "preview_generations": 0,
            "manager_preview_approval_events": 0,
            "owner_acceptance_events": 0,
            "privileged_operation_events": 0,
            "privileged_operations": 0,
            "preview_desired_states": 0,
            "preview_inventory_scans": 0,
            "preview_lifecycle_cleanups": 0,
            "preview_lifecycle_plans": 0,
            "preview_pr_feedback": 0,
            "runner_host_hygiene_audits": 0,
            "runner_lane_registration_audits": 0,
            "every_code_preview_gates": 0,
            "agent_write_intents": 0,
            "merge_train_pr_feedback": 0,
            "merge_train_batch_candidates": 0,
            "merge_train_controller_states": 0,
            "merge_train_batch_landing_plans": 0,
            "merge_admissions": 0,
            "merge_landing_outcomes": 0,
            "merge_train_stack_collapse_plans": 0,
            "merge_train_policies": 0,
            "merge_train_runs": 0,
            "odoo_stable_bootstrap_operations": 0,
            "odoo_prod_backup_restore_operations": 0,
            "odoo_prod_retained_volume_backup_import_operations": 0,
            "odoo_stable_target_replacement_operations": 0,
            "release_tuples": 0,
            "runtime_key_safety_policies": 0,
            "tenant_repository_classifications": 0,
            "repository_inventory": 0,
        }
        for artifact_manifest in filesystem_store.list_artifact_manifests():
            self.write_artifact_manifest(artifact_manifest)
            counts["artifacts"] += 1
        for policy_record in filesystem_store.list_runtime_key_safety_policy_records():
            self.write_runtime_key_safety_policy_record(policy_record)
            counts["runtime_key_safety_policies"] += 1
        if hasattr(filesystem_store, "list_tenant_repository_classification_records"):
            classification_records = sorted(
                filesystem_store.list_tenant_repository_classification_records(),
                key=lambda record: (
                    record.repository_id,
                    record.classification_revision,
                    record.record_id,
                ),
            )
            for classification_record in classification_records:
                self.write_tenant_repository_classification_record(classification_record)
                counts["tenant_repository_classifications"] += 1
        if hasattr(filesystem_store, "list_repository_inventory_records"):
            repository_inventory_records = sorted(
                filesystem_store.list_repository_inventory_records(),
                key=lambda record: (
                    record.repository_id,
                    record.inventory_revision,
                    record.record_id,
                ),
            )
            for repository_inventory_record in repository_inventory_records:
                self.write_repository_inventory_record(repository_inventory_record)
                counts["repository_inventory"] += 1
        for owner_policy in sorted(
            filesystem_store.list_product_owner_policy_records(),
            key=lambda record: (record.product, record.system, record.policy_revision),
        ):
            self.write_product_owner_policy_record(
                owner_policy.model_copy(update={"status": "active"})
            )
            counts["product_owner_policies"] += 1
        if hasattr(filesystem_store, "list_change_impact_policy_records"):
            for impact_policy in sorted(
                filesystem_store.list_change_impact_policy_records(),
                key=lambda record: (record.repository_id, record.policy_revision),
            ):
                self.write_change_impact_policy_record(
                    impact_policy.model_copy(update={"status": "active"})
                )
                counts["change_impact_policies"] += 1
        for owner_requirement in sorted(
            filesystem_store.list_product_owner_requirement_records(),
            key=lambda record: (
                record.product,
                record.system,
                record.requirement_revision,
            ),
        ):
            self.write_product_owner_requirement_record(
                owner_requirement.model_copy(update={"status": "active"})
            )
            counts["product_owner_requirements"] += 1
        for owner_routing in sorted(
            filesystem_store.list_product_owner_routing_records(),
            key=lambda record: (record.product, record.system, record.routing_revision),
        ):
            self.write_product_owner_routing_record(
                owner_routing.model_copy(update={"status": "active"})
            )
            counts["product_owner_routing"] += 1
        for backup_gate_record in filesystem_store.list_backup_gate_records():
            self.write_backup_gate_record(backup_gate_record)
            counts["backup_gates"] += 1
        for deployment_record in filesystem_store.list_deployment_records():
            self.write_deployment_record(deployment_record)
            counts["deployments"] += 1
        for promotion_record in filesystem_store.list_promotion_records():
            self.write_promotion_record(promotion_record)
            counts["promotions"] += 1
        for inventory_record in filesystem_store.list_environment_inventory():
            self.write_environment_inventory(inventory_record)
            counts["inventory"] += 1
        for override_record in filesystem_store.list_odoo_instance_override_records():
            self.write_odoo_instance_override_record(override_record)
            counts["odoo_instance_overrides"] += 1
        for product_profile_record in filesystem_store.list_product_profile_records():
            self.write_product_profile_record(product_profile_record)
            counts["product_profiles"] += 1
        for preview_record in filesystem_store.list_preview_records():
            self.write_preview_record(preview_record)
            counts["preview_records"] += 1
        if hasattr(filesystem_store, "list_preview_enablement_records"):
            for enablement_record in filesystem_store.list_preview_enablement_records():
                self.write_preview_enablement_record(enablement_record)
                counts["preview_enablement"] += 1
        for generation_record in filesystem_store.list_preview_generation_records():
            self.write_preview_generation_record(generation_record)
            counts["preview_generations"] += 1
        if hasattr(filesystem_store, "list_manager_preview_approval_event_records"):
            for (
                manager_approval_event
            ) in filesystem_store.list_manager_preview_approval_event_records():
                self.write_manager_preview_approval_event_record(manager_approval_event)
                counts["manager_preview_approval_events"] += 1
        if hasattr(filesystem_store, "list_owner_acceptance_event_records"):
            owner_acceptance_events = sorted(
                filesystem_store.list_owner_acceptance_event_records(),
                key=lambda event: (*owner_acceptance_subject_key(event), event.subject_sequence),
            )
            for owner_acceptance_event in owner_acceptance_events:
                self.write_owner_acceptance_event_record(owner_acceptance_event)
                counts["owner_acceptance_events"] += 1
        if hasattr(filesystem_store, "list_privileged_operation_records"):
            for privileged_operation in filesystem_store.list_privileged_operation_records():
                events = tuple(
                    sorted(
                        filesystem_store.list_privileged_operation_event_records(
                            operation_id=privileged_operation.operation_id
                        ),
                        key=lambda event: event.sequence,
                    )
                )
                if not events or events[0].action != "planned":
                    raise PrivilegedOperationConflictError(
                        "Filesystem privileged operation is missing its planned event."
                    )
                planned_record = privileged_operation.model_copy(
                    update={
                        "status": "planned",
                        "updated_at": privileged_operation.created_at,
                        "terminal_at": "",
                        "terminal_reason": "",
                    }
                )
                self.write_privileged_operation_plan(planned_record, events[0])
                counts["privileged_operation_events"] += 1
                if privileged_operation.status != "planned":
                    if len(events) != 2:
                        raise PrivilegedOperationConflictError(
                            "Terminal filesystem privileged operation requires two events."
                        )
                    self.transition_privileged_operation(privileged_operation, events[1])
                    counts["privileged_operation_events"] += 1
                counts["privileged_operations"] += 1
        if hasattr(filesystem_store, "list_preview_inventory_scan_records"):
            for scan_record in filesystem_store.list_preview_inventory_scan_records():
                self.write_preview_inventory_scan_record(scan_record)
                counts["preview_inventory_scans"] += 1
        if hasattr(filesystem_store, "list_preview_desired_state_records"):
            for desired_state_record in filesystem_store.list_preview_desired_state_records():
                self.write_preview_desired_state_record(desired_state_record)
                counts["preview_desired_states"] += 1
        if hasattr(filesystem_store, "list_preview_lifecycle_plan_records"):
            for lifecycle_plan_record in filesystem_store.list_preview_lifecycle_plan_records():
                self.write_preview_lifecycle_plan_record(lifecycle_plan_record)
                counts["preview_lifecycle_plans"] += 1
        if hasattr(filesystem_store, "list_preview_lifecycle_cleanup_records"):
            for cleanup_record in filesystem_store.list_preview_lifecycle_cleanup_records():
                self.write_preview_lifecycle_cleanup_record(cleanup_record)
                counts["preview_lifecycle_cleanups"] += 1
        if hasattr(filesystem_store, "list_preview_pr_feedback_records"):
            for pr_feedback_record in filesystem_store.list_preview_pr_feedback_records():
                self.write_preview_pr_feedback_record(pr_feedback_record)
                counts["preview_pr_feedback"] += 1
        if hasattr(filesystem_store, "list_runner_host_hygiene_audit_records"):
            for hygiene_audit_record in filesystem_store.list_runner_host_hygiene_audit_records():
                self.write_runner_host_hygiene_audit_record(hygiene_audit_record)
                counts["runner_host_hygiene_audits"] += 1
        if hasattr(filesystem_store, "list_runner_lane_registration_audit_records"):
            for (
                registration_audit_record
            ) in filesystem_store.list_runner_lane_registration_audit_records():
                self.write_runner_lane_registration_audit_record(registration_audit_record)
                counts["runner_lane_registration_audits"] += 1
        if hasattr(filesystem_store, "list_every_code_preview_gate_records"):
            for gate_record in filesystem_store.list_every_code_preview_gate_records():
                self.write_every_code_preview_gate_record(gate_record)
                counts["every_code_preview_gates"] += 1
        if hasattr(filesystem_store, "list_agent_write_intent_records"):
            for intent_record in filesystem_store.list_agent_write_intent_records():
                self.write_agent_write_intent_record(intent_record)
                counts["agent_write_intents"] += 1
        if hasattr(filesystem_store, "list_merge_train_run_records"):
            for run_record in filesystem_store.list_merge_train_run_records():
                self.write_merge_train_run_record(run_record)
                counts["merge_train_runs"] += 1
        if hasattr(filesystem_store, "list_merge_train_pr_feedback_records"):
            for feedback_record in filesystem_store.list_merge_train_pr_feedback_records():
                self.write_merge_train_pr_feedback_record(feedback_record)
                counts["merge_train_pr_feedback"] += 1
        if hasattr(filesystem_store, "list_merge_train_batch_candidate_records"):
            for candidate_record in filesystem_store.list_merge_train_batch_candidate_records():
                self.write_merge_train_batch_candidate_record(candidate_record)
                counts["merge_train_batch_candidates"] += 1
        if hasattr(filesystem_store, "list_merge_train_controller_state_records"):
            for (
                controller_state_record
            ) in filesystem_store.list_merge_train_controller_state_records():
                self.write_merge_train_controller_state_record(controller_state_record)
                counts["merge_train_controller_states"] += 1
        if hasattr(filesystem_store, "list_merge_train_batch_landing_plan_records"):
            for plan_record in filesystem_store.list_merge_train_batch_landing_plan_records():
                self.write_merge_train_batch_landing_plan_record(plan_record)
                counts["merge_train_batch_landing_plans"] += 1
        if hasattr(filesystem_store, "list_merge_admission_records"):
            for admission_record in sorted(
                filesystem_store.list_merge_admission_records(),
                key=lambda record: (record.created_at, record.admission_id),
            ):
                self.create_merge_admission_record_if_absent(admission_record)
                counts["merge_admissions"] += 1
        if hasattr(filesystem_store, "list_merge_landing_outcome_records"):
            for outcome_record in sorted(
                filesystem_store.list_merge_landing_outcome_records(),
                key=lambda record: (
                    record.admission_id,
                    record.observation_sequence,
                    record.observed_at,
                    record.outcome_id,
                ),
            ):
                self.create_merge_landing_outcome_record_if_absent(outcome_record)
                counts["merge_landing_outcomes"] += 1
        if hasattr(filesystem_store, "list_merge_train_stack_collapse_plan_records"):
            for collapse_record in filesystem_store.list_merge_train_stack_collapse_plan_records():
                self.write_merge_train_stack_collapse_plan_record(collapse_record)
                counts["merge_train_stack_collapse_plans"] += 1
        if hasattr(filesystem_store, "list_merge_train_policy_records"):
            for merge_train_policy_record in filesystem_store.list_merge_train_policy_records():
                self.write_merge_train_policy_record(merge_train_policy_record)
                counts["merge_train_policies"] += 1
        if hasattr(filesystem_store, "list_odoo_stable_bootstrap_operation_records"):
            for (
                bootstrap_operation_record
            ) in filesystem_store.list_odoo_stable_bootstrap_operation_records():
                self.write_odoo_stable_bootstrap_operation_record(bootstrap_operation_record)
                counts["odoo_stable_bootstrap_operations"] += 1
        if hasattr(filesystem_store, "list_odoo_stable_target_replacement_operation_records"):
            for (
                replacement_operation_record
            ) in filesystem_store.list_odoo_stable_target_replacement_operation_records():
                self.write_odoo_stable_target_replacement_operation_record(
                    replacement_operation_record
                )
                counts["odoo_stable_target_replacement_operations"] += 1
        if hasattr(filesystem_store, "list_odoo_prod_backup_restore_operation_records"):
            for (
                restore_operation_record
            ) in filesystem_store.list_odoo_prod_backup_restore_operation_records():
                self.write_odoo_prod_backup_restore_operation_record(restore_operation_record)
                counts["odoo_prod_backup_restore_operations"] += 1
        if hasattr(
            filesystem_store,
            "list_odoo_prod_retained_volume_backup_import_operation_records",
        ):
            for (
                retained_import_record
            ) in filesystem_store.list_odoo_prod_retained_volume_backup_import_operation_records():
                self.write_odoo_prod_retained_volume_backup_import_operation_record(
                    retained_import_record
                )
                counts["odoo_prod_retained_volume_backup_import_operations"] += 1
        for release_tuple_record in filesystem_store.list_release_tuple_records():
            self.write_release_tuple_record(release_tuple_record)
            counts["release_tuples"] += 1
        return counts
