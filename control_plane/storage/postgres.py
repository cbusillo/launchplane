from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from typing import Any, Literal, Protocol, TypeVar, cast

from pydantic import BaseModel
from sqlalchemy import (
    JSON,
    Index,
    Integer,
    String,
    create_engine,
    delete,
    desc,
    inspect,
    text,
    select,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from control_plane.contracts.artifact_identity import ArtifactIdentityManifest
from control_plane.contracts.agent_write_intent import AgentWriteIntentRecord
from control_plane.contracts.authz_policy_record import LaunchplaneAuthzPolicyRecord
from control_plane.contracts.backup_gate_record import BackupGateRecord
from control_plane.contracts.deployment_record import DeploymentRecord
from control_plane.contracts.deploy_target import ProviderTargetRecord
from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.edge_endpoint_record import EdgeEndpointRecord
from control_plane.contracts.environment_inventory import EnvironmentInventory
from control_plane.contracts.every_code_preview_gate_record import EveryCodePreviewGateRecord
from control_plane.contracts.every_code_work_request import (
    EveryCodeWorkRequestRecord,
    claim_every_code_work_request,
)
from control_plane.contracts.every_code_pr_feedback_record import EveryCodePrFeedbackRecord
from control_plane.contracts.generic_web_rollback import GenericWebRollbackPlanRecord
from control_plane.contracts.idempotency_record import LaunchplaneIdempotencyRecord
from control_plane.contracts.ingress_route_audit_record import IngressRouteAuditRecord
from control_plane.contracts.lane_summary import LaunchplaneLaneSummary
from control_plane.contracts.merge_train_batch import (
    MergeTrainBatchCandidateRecord,
    MergeTrainBatchLandingPlanRecord,
)
from control_plane.contracts.merge_train_stack_collapse import (
    MergeTrainStackCollapsePlanRecord,
)
from control_plane.contracts.merge_train_run_record import MergeTrainRunRecord
from control_plane.contracts.merge_train_policy import MergeTrainPolicyRecord
from control_plane.contracts.merge_train_pr_feedback_record import (
    MergeTrainPrFeedbackRecord,
)
from control_plane.contracts.odoo_instance_override_record import OdooInstanceOverrideRecord
from control_plane.contracts.odoo_stable_bootstrap_operation import (
    OdooStableBootstrapOperationRecord,
)
from control_plane.contracts.odoo_stable_target_replacement_operation import (
    OdooStableTargetReplacementOperationRecord,
)
from control_plane.contracts.preview_desired_state_record import PreviewDesiredStateRecord
from control_plane.contracts.preview_enablement_record import PreviewEnablementRecord
from control_plane.contracts.preview_generation_record import PreviewGenerationRecord
from control_plane.contracts.preview_inventory_scan_record import PreviewInventoryScanRecord
from control_plane.contracts.preview_lifecycle_cleanup_record import PreviewLifecycleCleanupRecord
from control_plane.contracts.preview_lifecycle_plan_record import PreviewLifecyclePlanRecord
from control_plane.contracts.preview_pr_feedback_record import PreviewPrFeedbackRecord
from control_plane.contracts.preview_record import PreviewRecord
from control_plane.contracts.preview_summary import LaunchplanePreviewSummary
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.public_ingress_monitoring import (
    PublicIngressNotificationAttemptRecord,
)
from control_plane.contracts.public_ingress_monitoring import (
    PublicIngressNotificationPolicyRecord,
)
from control_plane.contracts.public_ingress_monitoring import PublicIngressIncidentRecord
from control_plane.contracts.public_ingress_monitoring import PublicIngressObservationRecord
from control_plane.contracts.promotion_record import PromotionRecord
from control_plane.contracts.release_tuple_record import ReleaseTupleRecord
from control_plane.contracts.runtime_environment_record import (
    RuntimeEnvironmentDeleteEvent,
    RuntimeEnvironmentRecord,
)
from control_plane.contracts.runtime_key_safety_policy import RuntimeKeySafetyPolicyRecord
from control_plane.contracts.runner_host_hygiene import RunnerHostHygieneApplyAuditRecord
from control_plane.contracts.runner_lane_registration import RunnerLaneRegistrationAuditRecord
from control_plane.contracts.secret_record import (
    SecretAuditEvent,
    SecretBinding,
    SecretRecord,
    SecretVersion,
)
from control_plane.service_auth import GitHubHumanIdentity
from control_plane.service_human_auth import HumanSessionStore, LaunchplaneHumanSession
from control_plane.storage.filesystem import FilesystemRecordStore

RecordModel = TypeVar("RecordModel", bound=BaseModel)
ConnectionFactory = Callable[[], Any]
PayloadDict = dict[str, Any]
PayloadJsonType = JSON().with_variant(JSONB(), "postgresql")
RuntimeEnvironmentDeleteStatus = Literal["deleted", "missing", "changed"]
CurrentAuthorityDeleteStatus = Literal["deleted", "missing", "changed"]
ProviderTargetCreateStatus = Literal["created", "exists"]


class _PayloadRow(Protocol):
    payload: PayloadDict


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
    __table_args__ = (Index("launchplane_authz_policies_updated_idx", desc("updated_at")),)

    record_id: Mapped[str] = mapped_column(String, primary_key=True)
    status: Mapped[str] = mapped_column(String, nullable=False)
    source: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)
    policy_sha256: Mapped[str] = mapped_column(String, nullable=False)
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
    )

    record_id: Mapped[str] = mapped_column(String, primary_key=True)
    product: Mapped[str] = mapped_column(String, nullable=False)
    context: Mapped[str] = mapped_column(String, nullable=False)
    instance: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    observed_at: Mapped[str] = mapped_column(String, nullable=False)
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
    )

    incident_id: Mapped[str] = mapped_column(String, primary_key=True)
    product: Mapped[str] = mapped_column(String, nullable=False)
    context: Mapped[str] = mapped_column(String, nullable=False)
    instance: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    opened_at: Mapped[str] = mapped_column(String, nullable=False)
    latest_observed_at: Mapped[str] = mapped_column(String, nullable=False)
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
    )

    request_id: Mapped[str] = mapped_column(String, primary_key=True)
    source: Mapped[str] = mapped_column(String, nullable=False)
    state: Mapped[str] = mapped_column(String, nullable=False)
    repository: Mapped[str] = mapped_column(String, nullable=False)
    issue_number: Mapped[int] = mapped_column(Integer, nullable=False)
    trigger_label: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)
    claimed_by_host: Mapped[str] = mapped_column(String, nullable=False)
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
    )

    record_id: Mapped[str] = mapped_column(String, primary_key=True)
    scope: Mapped[str] = mapped_column(String, nullable=False)
    route_path: Mapped[str] = mapped_column(String, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String, nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String, nullable=False)
    response_status_code: Mapped[int] = mapped_column(Integer, nullable=False)
    response_trace_id: Mapped[str] = mapped_column(String, nullable=False)
    recorded_at: Mapped[str] = mapped_column(String, nullable=False)
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
    return LaunchplaneHumanSession(
        session_id=str(payload.get("session_id") or ""),
        created_at=datetime.fromisoformat(str(payload.get("created_at") or "")),
        expires_at=datetime.fromisoformat(str(payload.get("expires_at") or "")),
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
    database_url: str, *, connection_factory: ConnectionFactory | None = None
) -> Engine:
    engine_kwargs: dict[str, Any] = {}
    if connection_factory is not None:
        engine_kwargs["creator"] = connection_factory
    if database_url.startswith("sqlite"):
        engine_kwargs["connect_args"] = {"check_same_thread": False}
    return create_engine(database_url, **engine_kwargs)


class PostgresRecordStore(HumanSessionStore):
    def __init__(
        self,
        *,
        database_url: str,
        connection_factory: ConnectionFactory | None = None,
    ) -> None:
        self.database_url = database_url
        self._engine = _build_engine(database_url, connection_factory=connection_factory)
        self._session_factory = sessionmaker(self._engine, expire_on_commit=False)

    @property
    def backend_name(self) -> str:
        return "postgres"

    def ensure_schema(self) -> None:
        Base.metadata.create_all(self._engine)

    def verify_schema(self) -> None:
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
                    missing_columns.append(f"{table_name}.{column_name}")
        if missing_columns:
            missing = ", ".join(missing_columns)
            raise RuntimeError(
                "Launchplane shared storage schema is missing required column(s): "
                f"{missing}. Run Alembic migrations before starting the hosted service."
            )

    def close(self) -> None:
        self._engine.dispose()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            return None

    def _payload_dict(self, model: BaseModel) -> PayloadDict:
        return model.model_dump(mode="json", exclude_none=True)

    def _read_payload(self, *, model_type: type[RecordModel], payload: PayloadDict) -> RecordModel:
        return model_type.model_validate(payload)

    def _write_row(self, row: Base) -> None:
        with self._session_factory() as session:
            session.merge(row)
            session.commit()

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
                    payload=cast(_PayloadRow, row).payload,
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

    def write_idempotency_record(self, record: LaunchplaneIdempotencyRecord) -> None:
        self._write_row(
            LaunchplaneIdempotencyRow(
                record_id=record.record_id,
                scope=record.scope,
                route_path=record.route_path,
                idempotency_key=record.idempotency_key,
                request_fingerprint=record.request_fingerprint,
                response_status_code=record.response_status_code,
                response_trace_id=record.response_trace_id,
                recorded_at=record.recorded_at,
                payload=self._payload_dict(record),
            )
        )

    def read_idempotency_record(
        self,
        *,
        scope: str,
        route_path: str,
        idempotency_key: str,
    ) -> LaunchplaneIdempotencyRecord | None:
        statement = (
            select(LaunchplaneIdempotencyRow)
            .where(
                LaunchplaneIdempotencyRow.scope == scope,
                LaunchplaneIdempotencyRow.route_path == route_path,
                LaunchplaneIdempotencyRow.idempotency_key == idempotency_key,
            )
            .limit(1)
        )
        with self._session_factory() as session:
            row = session.scalar(statement)
            if row is None:
                return None
            return self._read_payload(model_type=LaunchplaneIdempotencyRecord, payload=row.payload)

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
                payload=self._payload_dict(record),
            )
        )

    def create_odoo_stable_bootstrap_operation_record_if_no_active_lane(
        self, record: OdooStableBootstrapOperationRecord
    ) -> tuple[OdooStableBootstrapOperationRecord, bool]:
        with self._session_factory() as session:
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
                    statuses=("pending", "running"),
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
                payload=self._payload_dict(record),
            )
        )

    def create_odoo_stable_target_replacement_operation_record_if_no_active_lane(
        self, record: OdooStableTargetReplacementOperationRecord
    ) -> tuple[OdooStableTargetReplacementOperationRecord, bool]:
        with self._session_factory() as session:
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
                    statuses=("pending", "running"),
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

    def delete_session(self, session_id: str) -> None:
        with self._session_factory() as session:
            session.execute(
                delete(LaunchplaneHumanSessionRow).where(
                    LaunchplaneHumanSessionRow.session_id == session_id
                )
            )
            session.commit()

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
        self._write_row(
            LaunchplaneInventoryRow(
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
        )

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

    def claim_every_code_work_request_record(
        self,
        *,
        request_id: str,
        host: str,
        claimed_at: str,
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
            )
            if claimed_record is None:
                return None
            row.state = claimed_record.state
            row.updated_at = claimed_record.updated_at
            row.claimed_by_host = claimed_record.claimed_by_host
            row.payload = self._payload_dict(claimed_record)
            session.commit()
            return claimed_record

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
        self._write_row(
            LaunchplaneMergeTrainPolicyRow(
                record_id=record.record_id,
                status=record.status,
                source=record.source,
                updated_at=record.updated_at,
                policy_sha256=record.policy_sha256,
                payload=self._payload_dict(record),
            )
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

    def write_runner_host_hygiene_audit_record(
        self, record: RunnerHostHygieneApplyAuditRecord
    ) -> None:
        self._write_row(
            LaunchplaneRunnerHostHygieneAuditRow(
                audit_record_key=record.audit_record_key,
                host_name=record.request.host_name,
                action=record.request.action,
                status=record.status,
                mutate=int(record.request.mutate),
                payload=self._payload_dict(record),
            )
        )

    def list_runner_host_hygiene_audit_records(
        self,
        *,
        host_name: str = "",
        status: str = "",
        limit: int | None = None,
    ) -> tuple[RunnerHostHygieneApplyAuditRecord, ...]:
        filters: list[object] = []
        normalized_host_name = host_name.strip().lower()
        if normalized_host_name:
            filters.append(LaunchplaneRunnerHostHygieneAuditRow.host_name == normalized_host_name)
        if status:
            filters.append(LaunchplaneRunnerHostHygieneAuditRow.status == status)
        return self._list_models(
            model_type=RunnerHostHygieneApplyAuditRecord,
            orm_model=LaunchplaneRunnerHostHygieneAuditRow,
            filters=filters,
            order_by=(LaunchplaneRunnerHostHygieneAuditRow.audit_record_key.desc(),),
            limit=limit,
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
        self._write_row(
            LaunchplaneReleaseTupleRow(
                context=record.context,
                channel=record.channel,
                tuple_id=record.tuple_id,
                artifact_id=record.artifact_id,
                minted_at=record.minted_at,
                provenance=record.provenance,
                payload=self._payload_dict(record),
            )
        )

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

    def write_authz_policy_record(self, record: LaunchplaneAuthzPolicyRecord) -> None:
        self._write_row(
            LaunchplaneAuthzPolicyRow(
                record_id=record.record_id,
                status=record.status,
                source=record.source,
                updated_at=record.updated_at,
                policy_sha256=record.policy_sha256,
                payload=self._payload_dict(record),
            )
        )

    def list_authz_policy_records(
        self,
        *,
        status: str = "",
        limit: int | None = None,
    ) -> tuple[LaunchplaneAuthzPolicyRecord, ...]:
        filters: list[object] = []
        if status:
            filters.append(LaunchplaneAuthzPolicyRow.status == status)
        records = self._list_models(
            model_type=LaunchplaneAuthzPolicyRecord,
            orm_model=LaunchplaneAuthzPolicyRow,
            filters=filters,
            order_by=(
                desc(LaunchplaneAuthzPolicyRow.updated_at),
                desc(LaunchplaneAuthzPolicyRow.record_id),
            ),
        )
        if limit is not None:
            return records[:limit]
        return records

    def write_product_profile_record(self, record: LaunchplaneProductProfileRecord) -> None:
        self._write_row(
            LaunchplaneProductProfileRow(
                product=record.product,
                display_name=record.display_name,
                repository=record.repository,
                driver_id=record.driver_id,
                updated_at=record.updated_at,
                payload=self._payload_dict(record),
            )
        )

    def read_product_profile_record(self, product: str) -> LaunchplaneProductProfileRecord:
        return self._read_model(
            model_type=LaunchplaneProductProfileRecord,
            orm_model=LaunchplaneProductProfileRow,
            filters=(LaunchplaneProductProfileRow.product == product,),
        )

    def list_product_profile_records(
        self,
        *,
        driver_id: str = "",
    ) -> tuple[LaunchplaneProductProfileRecord, ...]:
        filters: list[object] = []
        if driver_id:
            filters.append(LaunchplaneProductProfileRow.driver_id == driver_id)
        return self._list_models(
            model_type=LaunchplaneProductProfileRecord,
            orm_model=LaunchplaneProductProfileRow,
            filters=filters,
            order_by=(LaunchplaneProductProfileRow.product.asc(),),
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
        limit: int | None = None,
    ) -> tuple[PublicIngressObservationRecord, ...]:
        filters: list[object] = []
        if product:
            filters.append(LaunchplanePublicIngressObservationRow.product == product)
        if context_name:
            filters.append(LaunchplanePublicIngressObservationRow.context == context_name)
        if instance_name:
            filters.append(LaunchplanePublicIngressObservationRow.instance == instance_name)
        return self._list_models(
            model_type=PublicIngressObservationRecord,
            orm_model=LaunchplanePublicIngressObservationRow,
            filters=filters,
            order_by=(
                LaunchplanePublicIngressObservationRow.observed_at.desc(),
                LaunchplanePublicIngressObservationRow.record_id.desc(),
            ),
            limit=limit,
        )

    def write_public_ingress_incident_record(self, record: PublicIngressIncidentRecord) -> None:
        self._write_row(
            LaunchplanePublicIngressIncidentRow(
                incident_id=record.incident_id,
                product=record.product,
                context=record.context,
                instance=record.instance,
                status=record.status,
                opened_at=record.opened_at,
                latest_observed_at=record.latest_observed_at,
                payload=self._payload_dict(record),
            )
        )

    def list_public_ingress_incident_records(
        self,
        *,
        product: str = "",
        context_name: str = "",
        instance_name: str = "",
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
        if status:
            filters.append(LaunchplanePublicIngressIncidentRow.status == status)
        return self._list_models(
            model_type=PublicIngressIncidentRecord,
            orm_model=LaunchplanePublicIngressIncidentRow,
            filters=filters,
            order_by=(
                LaunchplanePublicIngressIncidentRow.opened_at.desc(),
                LaunchplanePublicIngressIncidentRow.incident_id.desc(),
            ),
            limit=limit,
        )

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
        self._write_row(
            LaunchplaneDokployTargetIdRow(
                context=record.context,
                instance=record.instance,
                target_id=record.target_id,
                updated_at=record.updated_at,
                payload=self._payload_dict(record),
            )
        )

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
        self._write_row(
            LaunchplaneDokployTargetRow(
                context=record.context,
                instance=record.instance,
                updated_at=record.updated_at,
                payload=self._payload_dict(record),
            )
        )

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
        self._write_row(
            LaunchplaneProviderTargetRow(
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
        )

    def create_provider_target_record_if_absent(
        self, record: ProviderTargetRecord
    ) -> ProviderTargetCreateStatus:
        row = LaunchplaneProviderTargetRow(
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
        with self._session_factory() as session:
            session.add(row)
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                return "exists"
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
        self._write_row(
            LaunchplaneRuntimeEnvironmentRow(
                scope=record.scope,
                context=record.context,
                instance=record.instance,
                updated_at=record.updated_at,
                payload=self._payload_dict(record),
            )
        )

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
        self._write_row(
            LaunchplaneRuntimeEnvironmentDeleteEventRow(
                event_id=event.event_id,
                scope=event.scope,
                context=event.context,
                instance=event.instance,
                recorded_at=event.recorded_at,
                payload=self._payload_dict(event),
            )
        )

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
        self._write_row(
            LaunchplaneSecretRow(
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
        )

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
        self._write_row(
            LaunchplaneSecretVersionRow(
                version_id=version.version_id,
                secret_id=version.secret_id,
                created_at=version.created_at,
                payload=self._payload_dict(version),
            )
        )

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
        self._write_row(
            LaunchplaneSecretBindingRow(
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
        )

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
        self._write_row(
            LaunchplaneSecretAuditEventRow(
                event_id=event.event_id,
                secret_id=event.secret_id,
                event_type=event.event_type,
                recorded_at=event.recorded_at,
                payload=self._payload_dict(event),
            )
        )

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
            "preview_records": 0,
            "preview_enablement": 0,
            "preview_generations": 0,
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
            "merge_train_batch_landing_plans": 0,
            "merge_train_stack_collapse_plans": 0,
            "merge_train_policies": 0,
            "merge_train_runs": 0,
            "odoo_stable_bootstrap_operations": 0,
            "odoo_stable_target_replacement_operations": 0,
            "release_tuples": 0,
            "runtime_key_safety_policies": 0,
        }
        for artifact_manifest in filesystem_store.list_artifact_manifests():
            self.write_artifact_manifest(artifact_manifest)
            counts["artifacts"] += 1
        for authz_policy_record in filesystem_store.list_authz_policy_records():
            self.write_authz_policy_record(authz_policy_record)
            counts["authz_policies"] += 1
        for policy_record in filesystem_store.list_runtime_key_safety_policy_records():
            self.write_runtime_key_safety_policy_record(policy_record)
            counts["runtime_key_safety_policies"] += 1
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
        if hasattr(filesystem_store, "list_merge_train_batch_landing_plan_records"):
            for plan_record in filesystem_store.list_merge_train_batch_landing_plan_records():
                self.write_merge_train_batch_landing_plan_record(plan_record)
                counts["merge_train_batch_landing_plans"] += 1
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
        for release_tuple_record in filesystem_store.list_release_tuple_records():
            self.write_release_tuple_record(release_tuple_record)
            counts["release_tuples"] += 1
        return counts
