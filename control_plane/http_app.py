import hashlib
import json
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path as FilePath
from typing import Annotated, Literal, Protocol, cast
from uuid import uuid4
import click
from fastapi import Depends, FastAPI, Header, HTTPException, Path, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from jwt import InvalidTokenError
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from control_plane import dokploy as control_plane_dokploy
from control_plane.dokploy_target_inspect import (
    DokployTargetInspectRequest,
    DokployTargetInspectStore,
    inspect_dokploy_target,
)
from control_plane.dokploy_target_setup_http import (
    DokployTargetSetupEnvelope,
    execute_dokploy_target_setup,
)
from control_plane import product_context_audit as control_plane_product_context_audit
from control_plane import product_context_cutover as control_plane_product_context_cutover
from control_plane import product_read_service as control_plane_product_read_service
from control_plane import secrets as control_plane_secrets
from control_plane import service_status as control_plane_service_status
from control_plane import tracked_target_logs as control_plane_tracked_target_logs
from control_plane.agent_context_service import (
    AgentContextPayload,
    agent_context_action_allowed,
    agent_context_allowed,
    build_agent_context_service_payload,
)
from control_plane.contracts.authz_policy_record import (
    LaunchplaneAuthzPolicyRecord,
    authz_policy_sha256,
    build_authz_policy_record_id,
)
from control_plane.contracts.backup_gate_record import BackupGateRecord
from control_plane.contracts.deployment_record import DeploymentRecord
from control_plane.contracts.driver_descriptor import DriverContextView, DriverDescriptor
from control_plane.contracts.edge_endpoint_record import EdgeEndpointRecord
from control_plane.contracts.environment_inventory import EnvironmentInventory
from control_plane.contracts.every_code_preview_gate_record import EveryCodePreviewGateRecord
from control_plane.contracts.every_code_pr_feedback_record import (
    EveryCodePrFeedbackRecord,
    EveryCodePrFeedbackStatus,
    apply_every_code_pr_feedback_status,
)
from control_plane.contracts.every_code_notifications import (
    EveryCodeNotificationAttemptRecord,
    EveryCodeNotificationPolicyRecord,
)
from control_plane.contracts.every_code_summary_read_model import (
    EveryCodeSummaryReadModel,
    build_every_code_summary_read_model,
)
from control_plane.contracts.every_code_work_request import EveryCodeWorkRequestRecord
from control_plane.contracts.idempotency_record import (
    LaunchplaneIdempotencyRecord,
    build_launchplane_idempotency_record_id,
)
from control_plane.contracts.data_provenance import DataProvenance, FreshnessStatus
from control_plane.contracts.ingress_canary_route_record import IngressCanaryRouteRecord
from control_plane.contracts.ingress_route_audit_record import (
    IngressRouteAuditOperation,
    IngressRouteAuditRecord,
    build_ingress_route_audit_record_id,
)
from control_plane.contracts.odoo_stable_bootstrap_operation import (
    OdooStableBootstrapOperationRecord,
)
from control_plane.contracts.odoo_stable_target_replacement_operation import (
    OdooStableTargetReplacementOperationRecord,
)
from control_plane.merge_train_admission import (
    MergeTrainRunHistoryStore,
    build_merge_train_controller_status_read_model,
    evaluate_merge_train_admission_from_store,
)
from control_plane.merge_train_policy_source import (
    MergeTrainPolicyStoreMissingError,
    resolve_merge_train_policy_record,
)
from control_plane.contracts.product_environment_read_model import (
    ActionAllowed,
    ProductActivityReadModel,
    ProductEnvironmentConfigStatus,
    ProductEnvironmentDetail,
    ProductEnvironmentSummary,
    ProductReadModelStore,
    ProductSiteOverview,
)
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.repo_product_mapping_read_model import RepoProductMapping
from control_plane.contracts.preview_evidence import (
    PreviewDestroyedEvidenceEnvelope,
    PreviewGenerationEvidenceEnvelope,
)
from control_plane.contracts.preview_generation_record import PreviewGenerationRecord
from control_plane.contracts.preview_pr_feedback_notifications import (
    PreviewPrFeedbackNotificationAttemptRecord,
    PreviewPrFeedbackNotificationPolicyRecord,
)
from control_plane.contracts.preview_readiness_read_model import (
    PreviewReadinessReadModel,
    build_preview_readiness_read_model,
)
from control_plane.contracts.preview_record import PreviewRecord
from control_plane.contracts.promotion_record import PromotionRecord
from control_plane.contracts.work_graph_read_model import WorkGraphSnapshot
from control_plane.contracts.protected_artifacts import (
    ProtectedArtifactStore,
    ProtectedArtifactSet,
    build_protected_artifact_set,
)
from control_plane.contracts.private_health_endpoint_record import PrivateHealthEndpointRecord
from control_plane.contracts.runner_host_hygiene import RunnerHostHygieneApplyAuditRecord
from control_plane.contracts.runner_host_hygiene_evidence import (
    RunnerHostHygieneAuditEvidenceEnvelope,
)
from control_plane.contracts.runner_lane_registration import RunnerLaneRegistrationAuditRecord
from control_plane.contracts.runner_lane_registration_evidence import (
    RunnerLaneRegistrationAuditEvidenceEnvelope,
)
from control_plane.contracts.secret_record import SecretScope
from control_plane.contracts.public_ingress_monitoring import PublicIngressNotificationPolicyRecord
from control_plane.drivers.registry import build_driver_context_view, list_driver_descriptors
from control_plane.drivers.registry import read_driver_descriptor as read_driver_descriptor_record
from control_plane.every_code_work_request_write import (
    EveryCodeWorkRequestCreateEnvelope,
    build_every_code_work_request_record,
)
from control_plane.runtime_key_safety_http import (
    RuntimeKeySafetyPolicyApplyEnvelope,
    apply_runtime_key_safety_policy_route,
)
from control_plane.service_auth import (
    BearerIdentityConfig,
    GitHubActionsIdentity,
    GitHubHumanIdentity,
    LaunchplaneAuthzPolicy,
    LaunchplaneIdentity,
    LocalAdminIdentity,
    LocalOperatorIdentity,
    TerminalAgentIdentity,
    TokenVerifier,
    bearer_identity_from_token,
)
from control_plane.service_human_auth import HumanSessionManager, LaunchplaneHumanSession
from control_plane.launchplane_mutations import (
    LaunchplaneDestroyPreviewStore,
    LaunchplaneMutationStore,
    apply_launchplane_destroy_preview,
    apply_launchplane_generation_evidence,
)
from control_plane.storage.factory import build_shared_record_store
from control_plane.storage.factory import storage_backend_name
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.workflows.evidence_ingestion import (
    EvidenceIngestionStore,
    PromotionEvidenceValidationError,
    apply_deployment_evidence,
    apply_promotion_evidence,
)
from control_plane.workflows.public_ingress_monitor import (
    PublicIngressMonitorStore,
    public_ingress_notification_drivers,
    run_public_ingress_monitor_once,
)
from control_plane.workflows.ingress_provider import (
    IngressProvider,
    NpmplusIngressProvider,
    default_ingress_provider,
)
from control_plane.workflows.npmplus_ingress import (
    NpmplusIngressApplyRequest,
    NpmplusIngressApplyResult,
    NpmplusIngressClient,
)
from control_plane.workflows.odoo_stable_operation_worker import (
    DEFAULT_ODOO_STABLE_WORKER_MAX_ATTEMPTS,
    reconcile_stale_odoo_stable_operation_records,
)
from control_plane.workflows.ship import utc_now_timestamp
from control_plane.work_graph_issue_inbox import (
    GitHubIssueInboxReadModel,
    GitHubIssueInboxReconcileRequest,
)
from control_plane.work_graph_service import (
    WorkGraphIssueInboxProvider,
    WorkGraphIssueInboxReconcileProvider,
    WorkGraphPlanningFactsProvider,
    WorkGraphRankEnvelope,
    WorkGraphWorkRequestStore,
    build_repo_product_mapping_service_payload,
    build_work_graph_rank_result,
    build_work_graph_snapshot_service_payload,
)


_BEARER_CHALLENGE_HEADER = {"WWW-Authenticate": 'Bearer realm="Launchplane API"'}
_LAUNCHPLANE_DRIVER_READ_PRODUCT = "launchplane"
_LAUNCHPLANE_DRIVER_READ_CONTEXT = "launchplane"
_DEPLOYMENT_EVIDENCE_ROUTE = "/v1/evidence/deployments"
_BACKUP_GATE_EVIDENCE_ROUTE = "/v1/evidence/backup-gates"
_PROMOTION_EVIDENCE_ROUTE = "/v1/evidence/promotions"
_PREVIEW_GENERATION_EVIDENCE_ROUTE = "/v1/evidence/previews/generations"
_PREVIEW_DESTROYED_EVIDENCE_ROUTE = "/v1/evidence/previews/destroyed"
_RUNNER_HOST_HYGIENE_AUDIT_EVIDENCE_ROUTE = "/v1/evidence/runner-host-hygiene/audits"
_RUNNER_LANE_REGISTRATION_AUDIT_EVIDENCE_ROUTE = "/v1/evidence/runner-lane-registration/audits"
_DOKPLOY_TARGET_SETUP_ROUTE = "/v1/dokploy-targets/setup"
_PUBLIC_INGRESS_MONITOR_RUN_ONCE_ROUTE = "/v1/products/public-ingress-monitor/run-once"
_PUBLIC_INGRESS_NOTIFICATION_POLICY_APPLY_ROUTE = "/v1/public-ingress/notification-policies/apply"
_EVERY_CODE_NOTIFICATION_POLICY_APPLY_ROUTE = "/v1/every-code/notification-policies/apply"
_PREVIEW_PR_FEEDBACK_NOTIFICATION_POLICY_APPLY_ROUTE = (
    "/v1/previews/pr-feedback/notification-policies/apply"
)
_RUNTIME_KEY_SAFETY_POLICY_APPLY_ROUTE = "/v1/runtime-key-safety/policies/apply"
_EDGE_ENDPOINT_APPLY_ROUTE = "/v1/edge-endpoints/apply"
_PRIVATE_HEALTH_ENDPOINT_APPLY_ROUTE = "/v1/private-health-endpoints/apply"
_INGRESS_ROUTE_APPLY_ROUTE = "/v1/drivers/ingress/route-apply"
_INGRESS_CANARY_ROUTE_RECORD_APPLY_ROUTE = "/v1/ingress/canary-routes/records/apply"
_INGRESS_CANARY_ROUTE_APPLY_ROUTE = "/v1/ingress/canary-routes/apply"
_PRODUCT_PROFILES_ROUTE = "/v1/product-profiles"
_PRODUCT_CONTEXT_CUTOVER_APPLY_ROUTE = "/v1/product-profiles/context-cutover/apply"
_PRODUCT_LEGACY_CONTEXT_CLEANUP_APPLY_ROUTE = "/v1/product-profiles/legacy-context-cleanup/apply"
_LAUNCHPLANE_SERVICE_CONTEXT = "launchplane"


class RepoProductMappingReadStore(Protocol):
    def list_product_profile_records(
        self,
        *,
        driver_id: str = "",
    ) -> tuple[LaunchplaneProductProfileRecord, ...]: ...

    def list_every_code_work_request_records(
        self,
        *,
        state: str = "",
        repository: str = "",
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[EveryCodeWorkRequestRecord, ...]: ...


class AgentContextReadStore(ProductReadModelStore, Protocol):
    def list_every_code_work_request_records(
        self,
        *,
        state: str = "",
        repository: str = "",
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[EveryCodeWorkRequestRecord, ...]: ...

    def list_every_code_preview_gate_records(
        self,
        *,
        request_id: str = "",
        repository: str = "",
        pr_number: int | None = None,
        status: str = "",
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[EveryCodePreviewGateRecord, ...]: ...


class ProductProfileWriteStore(Protocol):
    def write_product_profile_record(self, record: LaunchplaneProductProfileRecord) -> object: ...


class WorkGraphSnapshotReadStore(ProductReadModelStore, WorkGraphWorkRequestStore, Protocol):
    pass


class OdooStableBootstrapOperationReadStore(Protocol):
    def read_odoo_stable_bootstrap_operation_record(
        self, operation_id: str
    ) -> OdooStableBootstrapOperationRecord: ...


class OdooStableTargetReplacementOperationReadStore(Protocol):
    def read_odoo_stable_target_replacement_operation_record(
        self, operation_id: str
    ) -> OdooStableTargetReplacementOperationRecord: ...


class LaunchplaneErrorDetail(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    message: str


class LaunchplaneErrorResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str = "rejected"
    trace_id: str
    error: LaunchplaneErrorDetail


class HealthResponse(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "status": "ok",
                    "trace_id": "launchplane_req_00000000000000000000000000000000",
                    "storage_backend": "postgres",
                }
            ]
        },
    )

    status: Literal["ok"] = "ok"
    trace_id: str
    storage_backend: str


class LaunchplaneRuntimeStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    authz_policy_sha256: str
    authz_policy_source: str
    bootstrap_authz_policy_sha256: str
    docker_image_reference: str
    service_audience: str
    storage_backend: str


class LaunchplaneRuntimeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    runtime: LaunchplaneRuntimeStatus


class OdooStableOperationLeaseSummaryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation_kind: str
    operation_id: str
    product: str
    context: str
    instance: str
    status: str
    phase: str
    attempt: int
    lease_owner: str
    lease_expires_at: str
    heartbeat_at: str
    heartbeat_age_seconds: int | None
    lease_expired: bool


class OdooStableOperationWorkerStatusResponseModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str
    recorded_at: str
    pending_count: int
    running_count: int
    stalled_count: int
    terminal_count: int
    counts_by_kind_status: dict[str, int]
    operations: tuple[OdooStableOperationLeaseSummaryResponse, ...]


class OdooStableOperationWorkerStatusResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    worker_status: OdooStableOperationWorkerStatusResponseModel


class OdooStableOperationWorkerReconcileResultResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reconciled_bootstrap_ids: tuple[str, ...]
    reconciled_replacement_ids: tuple[str, ...]
    reconciled_count: int


class OdooStableOperationWorkerReconcileResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    reconcile_result: OdooStableOperationWorkerReconcileResultResponse


class OdooStableBootstrapOperationStatusResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    operation: dict[str, object]
    result: dict[str, object] | None = None


class OdooStableTargetReplacementOperationStatusResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    operation: dict[str, object]
    result: dict[str, object] | None = None


class ProductEnvironmentConfigStatusResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str = "ok"
    trace_id: str
    config_status: ProductEnvironmentConfigStatus


class ProductEnvironmentListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    products: tuple[ProductSiteOverview, ...]


class ProductOverviewResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    product: ProductSiteOverview


class ProductActivityResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    activity: ProductActivityReadModel


class ProductEnvironmentsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    product: str
    display_name: str
    repository: str
    driver_id: str
    base_driver_id: str = ""
    environments: tuple[ProductEnvironmentSummary, ...]
    trust_state: FreshnessStatus
    provenance: DataProvenance


class ProductEnvironmentResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    environment: ProductEnvironmentDetail


class RepoProductMappingResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    mapping: RepoProductMapping
    source: dict[str, object]


class AgentContextResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    context: AgentContextPayload


class WorkGraphSnapshotResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    snapshot: WorkGraphSnapshot
    source: dict[str, object]


class WorkGraphIssueInboxResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    configured: bool
    inbox: GitHubIssueInboxReadModel


class DriverDescriptorsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    drivers: tuple[DriverDescriptor, ...]


class DriverDescriptorResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    driver: DriverDescriptor


class DriverContextViewResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    view: DriverContextView


class MergeTrainAdmissionQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repository: str
    base_branch: str = "main"

    @model_validator(mode="after")
    def _validate_query(self) -> "MergeTrainAdmissionQuery":
        self.repository = self.repository.strip()
        self.base_branch = self.base_branch.strip()
        if not self.repository:
            raise ValueError("merge train admission requires repository")
        if "/" not in self.repository:
            raise ValueError("merge train repository must be owner/name")
        if not self.base_branch:
            raise ValueError("merge train admission requires base_branch")
        return self


class MergeTrainAdmissionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    admission: dict[str, object]


class MergeTrainControllerStatusResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    controller_status: dict[str, object]


class MergeTrainPolicyTargetsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    policy: dict[str, object]
    targets: list[dict[str, object]]


class DokployTargetInspectResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    inspect: dict[str, object]


class TrackedTargetLogTargetResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_id: str
    target_type: str
    target_name: str
    app_name: str
    server_id: str
    source_label: str


class TrackedTargetLogRequestResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    line_count: int
    since: str
    search: str


class TrackedTargetLogLinesResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    line_count: int
    lines: tuple[str, ...]
    redacted: bool


class TrackedTargetLogsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    context: str
    instance: str
    target: TrackedTargetLogTargetResponse
    request: TrackedTargetLogRequestResponse
    logs: TrackedTargetLogLinesResponse


class EdgeEndpointRecordResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    record: EdgeEndpointRecord


class EdgeEndpointRecordsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    limit: int
    count: int
    records: tuple[EdgeEndpointRecord, ...]


class PrivateHealthEndpointRecordResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    record: PrivateHealthEndpointRecord


class PrivateHealthEndpointRecordsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    product: str
    context: str
    instance: str
    limit: int
    count: int
    records: tuple[PrivateHealthEndpointRecord, ...]


class IngressCanaryRouteRecordResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    record: IngressCanaryRouteRecord


class IngressCanaryRouteRecordsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    limit: int
    count: int
    records: tuple[IngressCanaryRouteRecord, ...]


class IngressRouteAuditRecordResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    record: IngressRouteAuditRecord


class IngressRouteAuditRecordsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    product: str
    context: str
    limit: int
    count: int
    records: tuple[IngressRouteAuditRecord, ...]


class EveryCodeWorkRequestRecordsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    state: str
    repository: str
    requests: tuple[EveryCodeWorkRequestRecord, ...]


class EveryCodeWorkRequestRecordResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    request: EveryCodeWorkRequestRecord


class EveryCodeSummaryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    summary: EveryCodeSummaryReadModel


class PreviewReadinessResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    readiness: PreviewReadinessReadModel


class EveryCodePrFeedbackRecordsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    request_id: str
    repository: str
    status_filter: str
    feedback: tuple[EveryCodePrFeedbackRecord, ...]


class EveryCodePrFeedbackStatusEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    feedback_id: str
    request_id: str
    status: EveryCodePrFeedbackStatus

    @model_validator(mode="after")
    def _validate_status(self) -> "EveryCodePrFeedbackStatusEnvelope":
        if not self.feedback_id.strip():
            raise ValueError("Every Code PR feedback status requires feedback_id")
        if not self.request_id.strip():
            raise ValueError("Every Code PR feedback status requires request_id")
        return self


class EveryCodePreviewGateRecordsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    request_id: str
    repository: str
    status_filter: str
    gates: tuple[EveryCodePreviewGateRecord, ...]


class EveryCodeNotificationAttemptRecordsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    request_id: str
    event_filter: str
    destination_kind_filter: str
    attempts: tuple[EveryCodeNotificationAttemptRecord, ...]


class PreviewPrFeedbackNotificationAttemptRecordsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    feedback_id: str
    event_filter: str
    destination_kind_filter: str
    attempts: tuple[PreviewPrFeedbackNotificationAttemptRecord, ...]


class DeploymentRecordResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    record: DeploymentRecord


class PromotionRecordResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    record: PromotionRecord


class PreviewRecordResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    record: PreviewRecord


class PreviewHistoryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    preview: PreviewRecord
    generations: tuple[PreviewGenerationRecord, ...]


class EnvironmentInventoryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    record: EnvironmentInventory


class RecentOperationsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    context: str
    storage_backend: str
    inventory: tuple[EnvironmentInventory, ...]
    recent_deployments: tuple[DeploymentRecord, ...]
    recent_promotions: tuple[PromotionRecord, ...]
    recent_previews: tuple[PreviewRecord, ...]


class ProductProfileListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    driver_id: str
    profiles: tuple[LaunchplaneProductProfileRecord, ...]


class ProductProfileResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    profile: LaunchplaneProductProfileRecord


class SecretStatusBinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    binding_id: str
    binding_type: str
    binding_key: str
    status: str
    context: str
    instance: str
    updated_at: str


class SecretStatusAuditEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str
    event_type: str
    recorded_at: str
    actor: str
    detail: str
    metadata: dict[str, str]


class SecretStatusReadModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    secret_id: str
    scope: SecretScope
    integration: str
    name: str
    context: str
    instance: str
    policy: str
    status: str
    description: str
    created_at: str
    updated_at: str
    updated_by: str
    last_validated_at: str
    current_version_id: str
    version_count: int
    current_version_created_at: str
    binding: SecretStatusBinding | None = None
    recent_audit_events: tuple[SecretStatusAuditEvent, ...]


class SecretStatusResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    secret: SecretStatusReadModel


class SecretStatusListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    context: str
    instance: str
    secrets: tuple[SecretStatusReadModel, ...]


class ProductContextCutoverAuditResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    audit: dict[str, object]


class ProtectedArtifactsResponse(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "status": "ok",
                    "trace_id": "launchplane_req_00000000000000000000000000000000",
                    "protected_artifacts": {
                        "schema_version": 1,
                        "product": "example-product",
                        "context": "",
                        "entries": [],
                        "artifact_ids": ["artifact-example-prod"],
                        "image_references": ["ghcr.io/example-org/example-app@sha256:abc123"],
                        "image_digests": ["sha256:abc123"],
                        "warnings": [],
                    },
                }
            ]
        },
    )

    status: Literal["ok"] = "ok"
    trace_id: str
    protected_artifacts: ProtectedArtifactSet


class DeploymentEvidenceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    product: str
    deployment: DeploymentRecord

    def model_post_init(self, _context: object) -> None:
        if not self.product.strip():
            raise ValueError("deployment evidence requires product")


class BackupGateEvidenceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    product: str
    backup_gate: BackupGateRecord

    def model_post_init(self, _context: object) -> None:
        if not self.product.strip():
            raise ValueError("backup gate evidence requires product")


class PromotionEvidenceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    product: str
    promotion: PromotionRecord

    def model_post_init(self, _context: object) -> None:
        if not self.product.strip():
            raise ValueError("promotion evidence requires product")


class AcceptedEvidenceResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["accepted"] = "accepted"
    trace_id: str
    records: dict[str, str]
    result: dict[str, object] | None = None
    replayed: bool | None = None
    original_trace_id: str | None = None


class PublicIngressNotificationPolicyApplyEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    mode: Literal["dry-run", "apply"] = "dry-run"
    policy: PublicIngressNotificationPolicyRecord
    reason: str = ""

    @model_validator(mode="after")
    def _validate_request(self) -> "PublicIngressNotificationPolicyApplyEnvelope":
        self.reason = self.reason.strip()
        if self.policy.product and self.policy.product != "launchplane":
            raise ValueError(
                "public ingress notification policy apply requires product 'launchplane'"
            )
        return self


class EveryCodeNotificationPolicyApplyEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    mode: Literal["dry-run", "apply"] = "dry-run"
    policy: EveryCodeNotificationPolicyRecord
    reason: str = ""

    @model_validator(mode="after")
    def _validate_request(self) -> "EveryCodeNotificationPolicyApplyEnvelope":
        self.reason = self.reason.strip()
        return self


class PreviewPrFeedbackNotificationPolicyApplyEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    mode: Literal["dry-run", "apply"] = "dry-run"
    policy: PreviewPrFeedbackNotificationPolicyRecord
    reason: str = ""

    @model_validator(mode="after")
    def _validate_request(self) -> "PreviewPrFeedbackNotificationPolicyApplyEnvelope":
        self.reason = self.reason.strip()
        return self


class EdgeEndpointApplyEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    mode: Literal["dry-run", "apply"] = "dry-run"
    endpoint: EdgeEndpointRecord
    reason: str = ""
    confirmation: str = ""

    @model_validator(mode="after")
    def _validate_envelope(self) -> "EdgeEndpointApplyEnvelope":
        if self.schema_version != 1:
            raise ValueError("Unsupported edge endpoint apply schema version")
        self.reason = self.reason.strip()
        self.confirmation = self.confirmation.strip()
        if self.mode == "apply":
            if not self.reason:
                raise ValueError("Edge endpoint apply requires a reason")
            if self.confirmation != "APPLY LAUNCHPLANE EDGE ENDPOINT":
                raise ValueError("Edge endpoint apply requires exact confirmation text")
        return self


class PrivateHealthEndpointApplyEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    mode: Literal["dry-run", "apply"] = "dry-run"
    endpoint: PrivateHealthEndpointRecord
    reason: str = ""
    confirmation: str = ""

    @model_validator(mode="after")
    def _validate_envelope(self) -> "PrivateHealthEndpointApplyEnvelope":
        if self.schema_version != 1:
            raise ValueError("Unsupported private health endpoint apply schema version")
        self.reason = self.reason.strip()
        self.confirmation = self.confirmation.strip()
        if self.mode == "apply":
            if not self.reason:
                raise ValueError("Private health endpoint apply requires a reason")
            if self.confirmation != "APPLY LAUNCHPLANE PRIVATE HEALTH ENDPOINT":
                raise ValueError("Private health endpoint apply requires exact confirmation text")
        return self


class NpmplusIngressApplyEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    product: str
    context: str
    ingress: NpmplusIngressApplyRequest

    @field_validator("product", "context", mode="after")
    @classmethod
    def _validate_required_text(cls, value: str) -> str:
        normalized_value = value.strip()
        if not normalized_value:
            raise ValueError("NPMplus ingress apply requires non-empty product/context")
        return normalized_value

    @model_validator(mode="after")
    def _validate_envelope(self) -> "NpmplusIngressApplyEnvelope":
        if self.schema_version != 1:
            raise ValueError("Unsupported NPMplus ingress apply schema version")
        return self


class IngressCanaryRouteRecordApplyEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    mode: Literal["dry-run", "apply"] = "dry-run"
    route: IngressCanaryRouteRecord
    reason: str = ""
    confirmation: str = ""

    @model_validator(mode="after")
    def _validate_envelope(self) -> "IngressCanaryRouteRecordApplyEnvelope":
        if self.schema_version != 1:
            raise ValueError("Unsupported ingress canary route apply schema version")
        self.reason = self.reason.strip()
        self.confirmation = self.confirmation.strip()
        if self.mode == "apply":
            if not self.reason:
                raise ValueError("Ingress canary route record apply requires a reason")
            if self.confirmation != "APPLY LAUNCHPLANE INGRESS CANARY ROUTE RECORD":
                raise ValueError(
                    "Ingress canary route record apply requires exact confirmation text"
                )
        return self


class IngressCanaryRouteApplyEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    product: str
    context: str
    canary_key: str
    reason: str

    @field_validator("product", "context", "canary_key", "reason", mode="after")
    @classmethod
    def _validate_required_text(cls, value: str) -> str:
        normalized_value = value.strip()
        if not normalized_value:
            raise ValueError("Ingress canary route apply requires non-empty text fields")
        return normalized_value

    @model_validator(mode="after")
    def _validate_envelope(self) -> "IngressCanaryRouteApplyEnvelope":
        if self.schema_version != 1:
            raise ValueError("Unsupported ingress canary route apply schema version")
        return self


class _RecordStoreFactory(Protocol):
    def __call__(self) -> object: ...


class _IngressProviderFactory(Protocol):
    def __call__(self) -> IngressProvider: ...


class _NpmplusIngressClientFactory(Protocol):
    def __call__(self) -> NpmplusIngressClient: ...


class _IdempotencyCapableStore(Protocol):
    def read_idempotency_record(
        self,
        *,
        scope: str,
        route_path: str,
        idempotency_key: str,
    ) -> LaunchplaneIdempotencyRecord | None: ...

    def write_idempotency_record(self, record: LaunchplaneIdempotencyRecord) -> object: ...


class _BackupGateEvidenceStore(Protocol):
    def write_backup_gate_record(self, record: BackupGateRecord) -> object: ...


class _RunnerHostHygieneAuditEvidenceStore(Protocol):
    def write_runner_host_hygiene_audit_record(
        self,
        record: RunnerHostHygieneApplyAuditRecord,
    ) -> object: ...


class _RunnerLaneRegistrationAuditEvidenceStore(Protocol):
    def write_runner_lane_registration_audit_record(
        self,
        record: RunnerLaneRegistrationAuditRecord,
    ) -> object: ...


class _PublicIngressNotificationPolicyApplyStore(Protocol):
    def write_public_ingress_notification_policy_record(
        self,
        record: PublicIngressNotificationPolicyRecord,
    ) -> object: ...


class _EveryCodeNotificationPolicyApplyStore(Protocol):
    def write_every_code_notification_policy_record(
        self,
        record: EveryCodeNotificationPolicyRecord,
    ) -> object: ...


class _PreviewPrFeedbackNotificationPolicyApplyStore(Protocol):
    def write_preview_pr_feedback_notification_policy_record(
        self,
        record: PreviewPrFeedbackNotificationPolicyRecord,
    ) -> object: ...


class _EdgeEndpointApplyStore(Protocol):
    def write_edge_endpoint_record(self, record: EdgeEndpointRecord) -> object: ...

    def read_edge_endpoint_record(self, endpoint_key: str) -> EdgeEndpointRecord: ...


class _PrivateHealthEndpointApplyStore(Protocol):
    def write_private_health_endpoint_record(
        self,
        record: PrivateHealthEndpointRecord,
    ) -> object: ...

    def read_private_health_endpoint_record(
        self,
        endpoint_key: str,
    ) -> PrivateHealthEndpointRecord: ...


class _IngressCanaryRouteRecordApplyStore(Protocol):
    def write_ingress_canary_route_record(
        self,
        record: IngressCanaryRouteRecord,
    ) -> object: ...


class _IngressRouteApplyStore(Protocol):
    def write_ingress_route_audit_record(self, record: IngressRouteAuditRecord) -> object: ...


class _IngressEdgeEndpointReadStore(Protocol):
    def read_edge_endpoint_record(self, endpoint_key: str) -> EdgeEndpointRecord: ...


class _IngressCanaryRouteApplyStore(
    _IngressRouteApplyStore,
    _IngressEdgeEndpointReadStore,
    Protocol,
):
    def read_ingress_canary_route_record(self, canary_key: str) -> IngressCanaryRouteRecord: ...


class PublicIngressMonitorRunOnceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    product: str = "launchplane"
    timeout_seconds: int = Field(default=10, ge=1, le=120)
    notify: bool = True

    @model_validator(mode="after")
    def _validate_request(self) -> "PublicIngressMonitorRunOnceRequest":
        if self.product.strip() != "launchplane":
            raise ValueError("public ingress monitor run requires product 'launchplane'")
        self.product = "launchplane"
        return self


class _DeploymentReadStore(Protocol):
    def read_deployment_record(self, record_id: str) -> DeploymentRecord: ...


class _PromotionReadStore(Protocol):
    def read_promotion_record(self, record_id: str) -> PromotionRecord: ...


class _PreviewReadStore(Protocol):
    def read_preview_record(self, preview_id: str) -> PreviewRecord: ...


class _PreviewHistoryReadStore(Protocol):
    def read_preview_record(self, preview_id: str) -> PreviewRecord: ...

    def list_preview_generation_records(
        self,
        *,
        preview_id: str = "",
        limit: int | None = None,
    ) -> tuple[PreviewGenerationRecord, ...]: ...


class _EnvironmentInventoryReadStore(Protocol):
    def read_environment_inventory(
        self,
        *,
        context_name: str,
        instance_name: str,
    ) -> EnvironmentInventory: ...


class _RecentOperationsReadStore(Protocol):
    def list_environment_inventory(self) -> tuple[EnvironmentInventory, ...]: ...

    def list_deployment_records(
        self,
        *,
        context_name: str = "",
        instance_name: str = "",
        limit: int | None = None,
    ) -> tuple[DeploymentRecord, ...]: ...

    def list_promotion_records(
        self,
        *,
        context_name: str = "",
        from_instance_name: str = "",
        to_instance_name: str = "",
        limit: int | None = None,
    ) -> tuple[PromotionRecord, ...]: ...

    def list_preview_records(
        self,
        *,
        context_name: str = "",
        anchor_repo: str = "",
        anchor_pr_number: int | None = None,
        limit: int | None = None,
    ) -> tuple[PreviewRecord, ...]: ...


class _EdgeEndpointReadStore(Protocol):
    def read_edge_endpoint_record(self, endpoint_key: str) -> EdgeEndpointRecord: ...

    def list_edge_endpoint_records(
        self,
        *,
        provider: str = "",
        status: str = "",
        limit: int | None = None,
    ) -> tuple[EdgeEndpointRecord, ...]: ...


class _PrivateHealthEndpointReadStore(Protocol):
    def read_private_health_endpoint_record(
        self, endpoint_key: str
    ) -> PrivateHealthEndpointRecord: ...

    def list_private_health_endpoint_records(
        self,
        *,
        product: str = "",
        context_name: str = "",
        instance_name: str = "",
        status: str = "",
        limit: int | None = None,
    ) -> tuple[PrivateHealthEndpointRecord, ...]: ...


class _IngressCanaryRouteReadStore(Protocol):
    def read_ingress_canary_route_record(self, canary_key: str) -> IngressCanaryRouteRecord: ...

    def list_ingress_canary_route_records(
        self,
        *,
        product: str = "",
        context_name: str = "",
        status: str = "",
        limit: int | None = None,
    ) -> tuple[IngressCanaryRouteRecord, ...]: ...


class _IngressRouteAuditRecordReadStore(Protocol):
    def read_ingress_route_audit_record(self, record_id: str) -> IngressRouteAuditRecord: ...

    def list_ingress_route_audit_records(
        self,
        *,
        product: str = "",
        context_name: str = "",
        limit: int | None = None,
    ) -> tuple[IngressRouteAuditRecord, ...]: ...


class _EveryCodeWorkRequestListStore(Protocol):
    def list_every_code_work_request_records(
        self,
        *,
        state: str = "",
        repository: str = "",
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[EveryCodeWorkRequestRecord, ...]: ...


class _EveryCodeWorkRequestRecordStore(Protocol):
    def read_every_code_work_request_record(
        self, request_id: str
    ) -> EveryCodeWorkRequestRecord: ...


class _EveryCodeWorkRequestWriteStore(Protocol):
    def write_every_code_work_request_record(
        self, record: EveryCodeWorkRequestRecord
    ) -> object: ...


class _EveryCodePrFeedbackReadStore(Protocol):
    def list_every_code_pr_feedback_records(
        self,
        *,
        request_id: str = "",
        repository: str = "",
        pr_number: int | None = None,
        status: str = "",
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[EveryCodePrFeedbackRecord, ...]: ...


class _EveryCodePrFeedbackWriteStore(Protocol):
    def write_every_code_pr_feedback_record(self, record: EveryCodePrFeedbackRecord) -> object: ...


class _EveryCodePrFeedbackStatusStore(
    _EveryCodePrFeedbackReadStore, _EveryCodePrFeedbackWriteStore, Protocol
):
    pass


class _EveryCodePreviewGateReadStore(Protocol):
    def list_every_code_preview_gate_records(
        self,
        *,
        request_id: str = "",
        repository: str = "",
        pr_number: int | None = None,
        status: str = "",
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[EveryCodePreviewGateRecord, ...]: ...


class _EveryCodePreviewGateWriteStore(Protocol):
    def write_every_code_preview_gate_record(
        self, record: EveryCodePreviewGateRecord
    ) -> object: ...


class _EveryCodeNotificationAttemptReadStore(Protocol):
    def list_every_code_notification_attempt_records(
        self,
        *,
        request_id: str = "",
        event: str = "",
        destination_kind: str = "",
        limit: int | None = None,
    ) -> tuple[EveryCodeNotificationAttemptRecord, ...]: ...


class _PreviewPrFeedbackNotificationAttemptReadStore(Protocol):
    def list_preview_pr_feedback_notification_attempt_records(
        self,
        *,
        feedback_id: str = "",
        event: str = "",
        destination_kind: str = "",
        limit: int | None = None,
    ) -> tuple[PreviewPrFeedbackNotificationAttemptRecord, ...]: ...


def require_protected_artifact_store(record_store: object) -> ProtectedArtifactStore:
    required_methods = (
        "list_artifact_manifests",
        "list_environment_inventory",
        "list_product_profile_records",
        "list_release_tuple_records",
        "list_preview_records",
        "list_preview_generation_records",
        "list_preview_pr_feedback_records",
    )
    missing_methods = [
        method_name
        for method_name in required_methods
        if not callable(getattr(record_store, method_name, None))
    ]
    if missing_methods:
        missing_summary = ", ".join(missing_methods)
        raise TypeError(
            "Launchplane record store does not support protected artifact inventory "
            f"reads: {missing_summary}"
        )
    return cast(ProtectedArtifactStore, record_store)


def require_deployment_evidence_store(record_store: object) -> EvidenceIngestionStore:
    required_methods = (
        "write_deployment_record",
        "write_environment_inventory",
    )
    missing_methods = [
        method_name
        for method_name in required_methods
        if not callable(getattr(record_store, method_name, None))
    ]
    if missing_methods:
        missing_summary = ", ".join(missing_methods)
        raise TypeError(
            "Launchplane record store does not support deployment evidence writes: "
            f"{missing_summary}"
        )
    return cast(EvidenceIngestionStore, record_store)


def require_promotion_evidence_store(
    record_store: object,
    promotion_record: PromotionRecord,
) -> EvidenceIngestionStore:
    if promotion_record.deployment_record_id.strip():
        required_methods = ["read_deployment_record", "write_promotion_evidence_records"]
    else:
        required_methods = ["write_promotion_record"]
    missing_methods = [
        method_name
        for method_name in required_methods
        if not callable(getattr(record_store, method_name, None))
    ]
    if missing_methods:
        missing_summary = ", ".join(missing_methods)
        raise TypeError(
            "Launchplane record store does not support promotion evidence writes: "
            f"{missing_summary}"
        )
    return cast(EvidenceIngestionStore, record_store)


def require_preview_generation_evidence_store(record_store: object) -> LaunchplaneMutationStore:
    required_methods = (
        "list_preview_records",
        "list_preview_generation_records",
        "write_preview_record",
        "write_preview_generation_record",
        "write_preview_generation_evidence_records",
    )
    missing_methods = [
        method_name
        for method_name in required_methods
        if not callable(getattr(record_store, method_name, None))
    ]
    if missing_methods:
        missing_summary = ", ".join(missing_methods)
        raise TypeError(
            "Launchplane record store does not support preview generation evidence writes: "
            f"{missing_summary}"
        )
    return cast(LaunchplaneMutationStore, record_store)


def require_preview_destroyed_evidence_store(
    record_store: object,
) -> LaunchplaneDestroyPreviewStore:
    required_methods = (
        "list_preview_records",
        "write_preview_record",
    )
    missing_methods = [
        method_name
        for method_name in required_methods
        if not callable(getattr(record_store, method_name, None))
    ]
    if missing_methods:
        missing_summary = ", ".join(missing_methods)
        raise TypeError(
            "Launchplane record store does not support preview destroyed evidence writes: "
            f"{missing_summary}"
        )
    return cast(LaunchplaneDestroyPreviewStore, record_store)


def require_backup_gate_evidence_store(record_store: object) -> _BackupGateEvidenceStore:
    required_methods = ("write_backup_gate_record",)
    missing_methods = [
        method_name
        for method_name in required_methods
        if not callable(getattr(record_store, method_name, None))
    ]
    if missing_methods:
        missing_summary = ", ".join(missing_methods)
        raise TypeError(
            "Launchplane record store does not support backup gate evidence writes: "
            f"{missing_summary}"
        )
    return cast(_BackupGateEvidenceStore, record_store)


def require_runner_host_hygiene_audit_evidence_store(
    record_store: object,
) -> _RunnerHostHygieneAuditEvidenceStore:
    required_methods = ("write_runner_host_hygiene_audit_record",)
    missing_methods = [
        method_name
        for method_name in required_methods
        if not callable(getattr(record_store, method_name, None))
    ]
    if missing_methods:
        missing_summary = ", ".join(missing_methods)
        raise TypeError(
            "Launchplane record store does not support runner host hygiene audit "
            f"evidence writes: {missing_summary}"
        )
    return cast(_RunnerHostHygieneAuditEvidenceStore, record_store)


def require_runner_lane_registration_audit_evidence_store(
    record_store: object,
) -> _RunnerLaneRegistrationAuditEvidenceStore:
    required_methods = ("write_runner_lane_registration_audit_record",)
    missing_methods = [
        method_name
        for method_name in required_methods
        if not callable(getattr(record_store, method_name, None))
    ]
    if missing_methods:
        missing_summary = ", ".join(missing_methods)
        raise TypeError(
            "Launchplane record store does not support runner lane registration audit "
            f"evidence writes: {missing_summary}"
        )
    return cast(_RunnerLaneRegistrationAuditEvidenceStore, record_store)


def require_deployment_read_store(record_store: object) -> _DeploymentReadStore:
    read_record = getattr(record_store, "read_deployment_record", None)
    if not callable(read_record):
        raise TypeError(
            "Launchplane record store does not support deployment record reads: "
            "read_deployment_record"
        )
    return cast(_DeploymentReadStore, record_store)


def require_promotion_read_store(record_store: object) -> _PromotionReadStore:
    read_record = getattr(record_store, "read_promotion_record", None)
    if not callable(read_record):
        raise TypeError(
            "Launchplane record store does not support promotion record reads: "
            "read_promotion_record"
        )
    return cast(_PromotionReadStore, record_store)


def require_preview_read_store(record_store: object) -> _PreviewReadStore:
    read_record = getattr(record_store, "read_preview_record", None)
    if not callable(read_record):
        raise TypeError(
            "Launchplane record store does not support preview reads: read_preview_record"
        )
    return cast(_PreviewReadStore, record_store)


def require_preview_history_read_store(record_store: object) -> _PreviewHistoryReadStore:
    required_methods = ("read_preview_record", "list_preview_generation_records")
    missing_methods = [
        method_name
        for method_name in required_methods
        if not callable(getattr(record_store, method_name, None))
    ]
    if missing_methods:
        missing_summary = ", ".join(missing_methods)
        raise TypeError(
            f"Launchplane record store does not support preview history reads: {missing_summary}"
        )
    return cast(_PreviewHistoryReadStore, record_store)


def require_environment_inventory_read_store(
    record_store: object,
) -> _EnvironmentInventoryReadStore:
    read_record = getattr(record_store, "read_environment_inventory", None)
    if not callable(read_record):
        raise TypeError(
            "Launchplane record store does not support environment inventory reads: "
            "read_environment_inventory"
        )
    return cast(_EnvironmentInventoryReadStore, record_store)


def require_recent_operations_read_store(record_store: object) -> _RecentOperationsReadStore:
    required_methods = (
        "list_environment_inventory",
        "list_deployment_records",
        "list_promotion_records",
        "list_preview_records",
    )
    missing_methods = [
        method_name
        for method_name in required_methods
        if not callable(getattr(record_store, method_name, None))
    ]
    if missing_methods:
        missing_summary = ", ".join(missing_methods)
        raise TypeError(
            f"Launchplane record store does not support recent operation reads: {missing_summary}"
        )
    return cast(_RecentOperationsReadStore, record_store)


def require_odoo_stable_bootstrap_operation_read_store(
    record_store: object,
) -> OdooStableBootstrapOperationReadStore:
    read_record = getattr(record_store, "read_odoo_stable_bootstrap_operation_record", None)
    if not callable(read_record):
        raise TypeError(
            "Launchplane record store does not support Odoo stable bootstrap operation "
            "status reads: read_odoo_stable_bootstrap_operation_record"
        )
    return cast(OdooStableBootstrapOperationReadStore, record_store)


def require_odoo_stable_target_replacement_operation_read_store(
    record_store: object,
) -> OdooStableTargetReplacementOperationReadStore:
    read_record = getattr(
        record_store,
        "read_odoo_stable_target_replacement_operation_record",
        None,
    )
    if not callable(read_record):
        raise TypeError(
            "Launchplane record store does not support Odoo target replacement operation "
            "status reads: read_odoo_stable_target_replacement_operation_record"
        )
    return cast(OdooStableTargetReplacementOperationReadStore, record_store)


def odoo_stable_bootstrap_operation_status_payload(
    operation: OdooStableBootstrapOperationRecord,
) -> dict[str, object]:
    payload = operation.model_dump(mode="json")
    payload["poll_url"] = (
        f"/v1/drivers/odoo/stable-bootstrap/operations/{operation.operation_id.strip()}"
    )
    return payload


def odoo_stable_target_replacement_operation_status_payload(
    operation: OdooStableTargetReplacementOperationRecord,
) -> dict[str, object]:
    payload = operation.model_dump(mode="json")
    payload["poll_url"] = (
        f"/v1/drivers/odoo/target-replacement/operations/{operation.operation_id.strip()}"
    )
    return payload


def require_product_profile_list_store(record_store: object) -> ProductReadModelStore:
    list_records = getattr(record_store, "list_product_profile_records", None)
    if not callable(list_records):
        raise TypeError(
            "Launchplane record store does not support product profile list reads: "
            "list_product_profile_records"
        )
    return cast(ProductReadModelStore, record_store)


def require_product_profile_read_store(record_store: object) -> ProductReadModelStore:
    read_record = getattr(record_store, "read_product_profile_record", None)
    if not callable(read_record):
        raise TypeError(
            "Launchplane record store does not support product profile reads: "
            "read_product_profile_record"
        )
    return cast(ProductReadModelStore, record_store)


def require_product_profile_write_store(record_store: object) -> ProductProfileWriteStore:
    write_record = getattr(record_store, "write_product_profile_record", None)
    if not callable(write_record):
        raise TypeError(
            "Launchplane record store does not support product profile writes: "
            "write_product_profile_record"
        )
    return cast(ProductProfileWriteStore, record_store)


def require_secret_status_read_store(record_store: object) -> control_plane_secrets.SecretReadStore:
    required_methods = (
        "read_secret_record",
        "list_secret_records",
        "read_secret_version",
        "list_secret_versions",
        "list_secret_bindings",
        "list_secret_audit_events",
    )
    missing_methods = [
        method_name
        for method_name in required_methods
        if not callable(getattr(record_store, method_name, None))
    ]
    if missing_methods:
        missing_summary = ", ".join(missing_methods)
        raise TypeError(
            f"Launchplane record store does not support secret status reads: {missing_summary}"
        )
    return cast(control_plane_secrets.SecretReadStore, record_store)


def require_public_ingress_monitor_store(
    record_store: object, *, notify: bool
) -> PublicIngressMonitorStore:
    required_methods = [
        "list_product_profile_records",
        "list_public_ingress_observation_records",
        "write_public_ingress_observation_record",
        "list_public_ingress_incident_records",
        "write_public_ingress_incident_record",
    ]
    if notify:
        required_methods.extend(
            [
                "list_public_ingress_notification_policy_records",
                "list_public_ingress_notification_attempt_records",
                "write_public_ingress_notification_attempt_record",
            ]
        )
    missing_methods = [
        method_name
        for method_name in required_methods
        if not callable(getattr(record_store, method_name, None))
    ]
    if missing_methods:
        missing_summary = ", ".join(missing_methods)
        raise TypeError(
            "Launchplane record store does not support public ingress monitor runs: "
            f"{missing_summary}"
        )
    return cast(PublicIngressMonitorStore, record_store)


def require_product_context_audit_store(
    record_store: object,
) -> control_plane_product_context_audit.ProductContextAuditStore:
    required_methods = (
        "read_product_profile_record",
        "list_runtime_environment_records",
        "list_secret_records",
        "list_secret_bindings",
        "list_dokploy_target_records",
        "list_dokploy_target_id_records",
        "list_environment_inventory",
        "list_release_tuple_records",
        "list_backup_gate_records",
        "list_deployment_records",
        "list_promotion_records",
    )
    missing_methods = [
        method_name
        for method_name in required_methods
        if not callable(getattr(record_store, method_name, None))
    ]
    if missing_methods or not isinstance(record_store, PostgresRecordStore):
        missing_summary = ", ".join(missing_methods) or "postgres_storage"
        raise TypeError(
            "Launchplane record store does not support context cutover audit reads: "
            f"{missing_summary}"
        )
    return cast(control_plane_product_context_audit.ProductContextAuditStore, record_store)


def require_tracked_target_logs_store(
    record_store: object,
) -> control_plane_tracked_target_logs.TrackedTargetLogsStore:
    required_methods = (
        "read_dokploy_target_record",
        "read_dokploy_target_id_record",
    )
    missing_methods = [
        method_name
        for method_name in required_methods
        if not callable(getattr(record_store, method_name, None))
    ]
    if missing_methods or not isinstance(record_store, PostgresRecordStore):
        missing_summary = ", ".join(missing_methods) or "postgres_storage"
        raise TypeError(
            f"Tracked target logs require DB-backed Launchplane storage: {missing_summary}"
        )
    return cast(control_plane_tracked_target_logs.TrackedTargetLogsStore, record_store)


def require_dokploy_target_inspect_store(record_store: object) -> DokployTargetInspectStore:
    required_methods = (
        "read_dokploy_target_record",
        "read_dokploy_target_id_record",
        "read_provider_target_record",
    )
    missing_methods = [
        method_name
        for method_name in required_methods
        if not callable(getattr(record_store, method_name, None))
    ]
    if missing_methods or not isinstance(record_store, PostgresRecordStore):
        raise TypeError("Dokploy target inspect requires Launchplane database storage.")
    return cast(DokployTargetInspectStore, record_store)


def require_edge_endpoint_read_store(record_store: object) -> _EdgeEndpointReadStore:
    required_methods = (
        "read_edge_endpoint_record",
        "list_edge_endpoint_records",
    )
    missing_methods = [
        method_name
        for method_name in required_methods
        if not callable(getattr(record_store, method_name, None))
    ]
    if missing_methods:
        missing_summary = ", ".join(missing_methods)
        raise TypeError(
            f"Launchplane record store does not support edge endpoint reads: {missing_summary}"
        )
    return cast(_EdgeEndpointReadStore, record_store)


def require_private_health_endpoint_read_store(
    record_store: object,
) -> _PrivateHealthEndpointReadStore:
    required_methods = (
        "read_private_health_endpoint_record",
        "list_private_health_endpoint_records",
    )
    missing_methods = [
        method_name
        for method_name in required_methods
        if not callable(getattr(record_store, method_name, None))
    ]
    if missing_methods:
        missing_summary = ", ".join(missing_methods)
        raise TypeError(
            "Launchplane record store does not support private health endpoint reads: "
            f"{missing_summary}"
        )
    return cast(_PrivateHealthEndpointReadStore, record_store)


def require_edge_endpoint_apply_store(record_store: object) -> _EdgeEndpointApplyStore:
    required_methods = (
        "write_edge_endpoint_record",
        "read_edge_endpoint_record",
    )
    missing_methods = [
        method_name
        for method_name in required_methods
        if not callable(getattr(record_store, method_name, None))
    ]
    if missing_methods:
        missing_summary = ", ".join(missing_methods)
        raise TypeError(
            f"Launchplane record store does not support edge endpoint applies: {missing_summary}"
        )
    return cast(_EdgeEndpointApplyStore, record_store)


def require_private_health_endpoint_apply_store(
    record_store: object,
) -> _PrivateHealthEndpointApplyStore:
    required_methods = (
        "write_private_health_endpoint_record",
        "read_private_health_endpoint_record",
    )
    missing_methods = [
        method_name
        for method_name in required_methods
        if not callable(getattr(record_store, method_name, None))
    ]
    if missing_methods:
        missing_summary = ", ".join(missing_methods)
        raise TypeError(
            "Launchplane record store does not support private health endpoint applies: "
            f"{missing_summary}"
        )
    return cast(_PrivateHealthEndpointApplyStore, record_store)


def require_ingress_canary_route_record_apply_store(
    record_store: object,
) -> _IngressCanaryRouteRecordApplyStore:
    required_methods = ("write_ingress_canary_route_record",)
    missing_methods = [
        method_name
        for method_name in required_methods
        if not callable(getattr(record_store, method_name, None))
    ]
    if missing_methods:
        missing_summary = ", ".join(missing_methods)
        raise TypeError(
            "Launchplane record store does not support ingress canary route record "
            f"applies: {missing_summary}"
        )
    return cast(_IngressCanaryRouteRecordApplyStore, record_store)


def require_ingress_canary_route_apply_store(
    record_store: object,
) -> _IngressCanaryRouteApplyStore:
    required_methods = (
        "read_ingress_canary_route_record",
        "read_edge_endpoint_record",
        "write_ingress_route_audit_record",
    )
    missing_methods = [
        method_name
        for method_name in required_methods
        if not callable(getattr(record_store, method_name, None))
    ]
    if missing_methods:
        missing_summary = ", ".join(missing_methods)
        raise TypeError(
            "Launchplane record store does not support ingress canary route applies: "
            f"{missing_summary}"
        )
    return cast(_IngressCanaryRouteApplyStore, record_store)


def require_ingress_route_apply_store(record_store: object) -> _IngressRouteApplyStore:
    required_methods = ("write_ingress_route_audit_record",)
    missing_methods = [
        method_name
        for method_name in required_methods
        if not callable(getattr(record_store, method_name, None))
    ]
    if missing_methods:
        missing_summary = ", ".join(missing_methods)
        raise TypeError(
            f"Launchplane record store does not support ingress route applies: {missing_summary}"
        )
    return cast(_IngressRouteApplyStore, record_store)


def require_ingress_edge_endpoint_read_store(record_store: object) -> _IngressEdgeEndpointReadStore:
    required_methods = ("read_edge_endpoint_record",)
    missing_methods = [
        method_name
        for method_name in required_methods
        if not callable(getattr(record_store, method_name, None))
    ]
    if missing_methods:
        missing_summary = ", ".join(missing_methods)
        raise TypeError(
            f"Launchplane record store does not support ingress edge endpoint reads: {missing_summary}"
        )
    return cast(_IngressEdgeEndpointReadStore, record_store)


def require_ingress_canary_route_read_store(
    record_store: object,
) -> _IngressCanaryRouteReadStore:
    required_methods = (
        "read_ingress_canary_route_record",
        "list_ingress_canary_route_records",
    )
    missing_methods = [
        method_name
        for method_name in required_methods
        if not callable(getattr(record_store, method_name, None))
    ]
    if missing_methods:
        missing_summary = ", ".join(missing_methods)
        raise TypeError(
            "Launchplane record store does not support ingress canary route reads: "
            f"{missing_summary}"
        )
    return cast(_IngressCanaryRouteReadStore, record_store)


def require_ingress_route_audit_record_read_store(
    record_store: object,
) -> _IngressRouteAuditRecordReadStore:
    required_methods = (
        "read_ingress_route_audit_record",
        "list_ingress_route_audit_records",
    )
    missing_methods = [
        method_name
        for method_name in required_methods
        if not callable(getattr(record_store, method_name, None))
    ]
    if missing_methods:
        missing_summary = ", ".join(missing_methods)
        raise TypeError(
            "Launchplane record store does not support ingress route audit reads: "
            f"{missing_summary}"
        )
    return cast(_IngressRouteAuditRecordReadStore, record_store)


def filter_ingress_route_audit_records(
    records: tuple[IngressRouteAuditRecord, ...],
    *,
    status: str = "",
    mode: str = "",
    provider_host_id: int | None = None,
    trace_id: str = "",
    idempotency_key: str = "",
) -> tuple[IngressRouteAuditRecord, ...]:
    filtered_records = records
    if status:
        filtered_records = tuple(record for record in filtered_records if record.status == status)
    if mode:
        filtered_records = tuple(record for record in filtered_records if record.mode == mode)
    if provider_host_id is not None:
        filtered_records = tuple(
            record for record in filtered_records if record.provider_host_id == provider_host_id
        )
    if trace_id:
        filtered_records = tuple(
            record for record in filtered_records if record.trace_id == trace_id
        )
    if idempotency_key:
        filtered_records = tuple(
            record for record in filtered_records if record.idempotency_key == idempotency_key
        )
    return filtered_records


def product_profile_context_cutover_allowed_contexts(
    profile: LaunchplaneProductProfileRecord,
) -> frozenset[str]:
    contexts = {profile.product.strip()}
    contexts.update(lane.context.strip() for lane in profile.lanes if lane.context.strip())
    if profile.preview.enabled and profile.preview.context.strip():
        contexts.add(profile.preview.context.strip())
    return frozenset(context for context in contexts if context)


def product_profile_context_cutover_contexts_allowed(
    *,
    profile: LaunchplaneProductProfileRecord,
    source_context: str,
    target_context: str,
    preview_context: str,
) -> bool:
    allowed_contexts = product_profile_context_cutover_allowed_contexts(profile)
    requested_contexts = {source_context.strip(), target_context.strip()}
    if preview_context.strip():
        requested_contexts.add(preview_context.strip())
    requested_contexts.discard("")
    return requested_contexts.issubset(allowed_contexts)


def idempotency_capable_store(record_store: object) -> _IdempotencyCapableStore | None:
    if callable(getattr(record_store, "read_idempotency_record", None)) and callable(
        getattr(record_store, "write_idempotency_record", None)
    ):
        return cast(_IdempotencyCapableStore, record_store)
    return None


def idempotency_scope(identity: LaunchplaneIdentity) -> str:
    if isinstance(identity, GitHubHumanIdentity):
        return "|".join(("github-human", identity.login, str(identity.github_id)))
    if isinstance(identity, LocalOperatorIdentity):
        return "|".join(("local-operator", identity.subject, identity.token_label))
    if isinstance(identity, LocalAdminIdentity):
        return "|".join(("local-admin", identity.subject, identity.token_label))
    if isinstance(identity, TerminalAgentIdentity):
        return "|".join(("terminal-agent", identity.subject, identity.token_label))
    if isinstance(identity, GitHubActionsIdentity):
        workflow_ref = identity.workflow_ref or identity.job_workflow_ref or ""
        return "|".join(
            (
                str(identity.repository).strip(),
                str(workflow_ref).strip(),
                str(identity.subject).strip(),
            )
        )
    raise TypeError(f"Unsupported Launchplane identity type: {type(identity).__name__}")


def request_fingerprint(payload: dict[str, object]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _public_ingress_notification_policy_summary(
    policy: PublicIngressNotificationPolicyRecord,
) -> dict[str, object]:
    return {
        "policy_id": policy.policy_id,
        "product": policy.product,
        "context": policy.context,
        "instance": policy.instance,
        "status": policy.status,
        "destination_count": len(policy.destinations),
        "destination_kinds": sorted({destination.kind for destination in policy.destinations}),
        "created_at": policy.created_at,
        "updated_at": policy.updated_at,
        "source": policy.source,
    }


def _every_code_notification_policy_summary(
    policy: EveryCodeNotificationPolicyRecord,
) -> dict[str, object]:
    return {
        "policy_id": policy.policy_id,
        "repository": policy.repository,
        "status": policy.status,
        "destination_count": len(policy.destinations),
        "destination_kinds": sorted({destination.kind for destination in policy.destinations}),
        "created_at": policy.created_at,
        "updated_at": policy.updated_at,
        "source": policy.source,
    }


def _preview_pr_feedback_notification_policy_summary(
    policy: PreviewPrFeedbackNotificationPolicyRecord,
) -> dict[str, object]:
    return {
        "policy_id": policy.policy_id,
        "product": policy.product,
        "context": policy.context,
        "repository": policy.repository,
        "status": policy.status,
        "destination_count": len(policy.destinations),
        "destination_kinds": sorted({destination.kind for destination in policy.destinations}),
        "created_at": policy.created_at,
        "updated_at": policy.updated_at,
        "source": policy.source,
    }


class LaunchplaneAuthzPolicyRuntime:
    def __init__(
        self,
        policy: LaunchplaneAuthzPolicy,
        *,
        policy_sha256: str = "",
        source: str = "bootstrap",
    ) -> None:
        self._policy = policy
        self._policy_sha256 = policy_sha256 or authz_policy_sha256(policy)
        self._source = source.strip() or "bootstrap"

    @property
    def policy(self) -> LaunchplaneAuthzPolicy:
        return self._policy

    @property
    def policy_sha256(self) -> str:
        return self._policy_sha256

    @property
    def source(self) -> str:
        return self._source

    def update(
        self,
        policy: LaunchplaneAuthzPolicy,
        *,
        policy_sha256: str = "",
        source: str = "",
    ) -> None:
        self._policy = policy
        self._policy_sha256 = policy_sha256 or authz_policy_sha256(policy)
        if source.strip():
            self._source = source.strip()


class ResolvedLaunchplaneAuthzPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    policy: LaunchplaneAuthzPolicy
    policy_sha256: str
    source: str


def resolve_launchplane_authz_policy(
    *,
    record_store: object,
    bootstrap_policy: LaunchplaneAuthzPolicy,
    policy_source: str,
    now_timestamp: str,
) -> ResolvedLaunchplaneAuthzPolicy:
    list_records = getattr(record_store, "list_authz_policy_records", None)
    if callable(list_records):
        records = list_records(status="active", limit=1)
        if records:
            record = records[0]
            return ResolvedLaunchplaneAuthzPolicy(
                policy=record.policy,
                policy_sha256=record.policy_sha256,
                source="db",
            )

    policy_sha256 = authz_policy_sha256(bootstrap_policy)
    write_record = getattr(record_store, "write_authz_policy_record", None)
    if callable(write_record):
        record = LaunchplaneAuthzPolicyRecord(
            record_id=build_authz_policy_record_id(
                updated_at=now_timestamp,
                policy_sha256=policy_sha256,
            ),
            status="active",
            source=policy_source,
            updated_at=now_timestamp,
            policy_sha256=policy_sha256,
            policy=bootstrap_policy,
        )
        write_record(record)
        return ResolvedLaunchplaneAuthzPolicy(
            policy=record.policy,
            policy_sha256=record.policy_sha256,
            source="bootstrap_seeded_store",
        )

    return ResolvedLaunchplaneAuthzPolicy(
        policy=bootstrap_policy,
        policy_sha256=policy_sha256,
        source="bootstrap",
    )


def create_launchplane_fastapi_app(
    *,
    verifier: TokenVerifier,
    authz_policy: LaunchplaneAuthzPolicy,
    authz_policy_runtime: LaunchplaneAuthzPolicyRuntime | None = None,
    database_url: str | None = None,
    record_store_factory: _RecordStoreFactory | None = None,
    ingress_provider_factory: _IngressProviderFactory | None = None,
    npmplus_ingress_client_factory: _NpmplusIngressClientFactory | None = None,
    bearer_identity_config: BearerIdentityConfig | None = None,
    human_session_manager: HumanSessionManager | None = None,
    control_plane_root_path: FilePath | None = None,
    work_graph_planning_facts_provider: WorkGraphPlanningFactsProvider | None = None,
    work_graph_issue_inbox_provider: WorkGraphIssueInboxProvider | None = None,
    work_graph_issue_inbox_reconcile_provider: WorkGraphIssueInboxReconcileProvider | None = None,
) -> FastAPI:
    resolved_authz_policy_runtime = authz_policy_runtime or LaunchplaneAuthzPolicyRuntime(
        authz_policy
    )
    resolved_control_plane_root = (
        control_plane_root_path or FilePath(__file__).resolve().parent.parent
    )
    shared_record_store: object | None = (
        None
        if record_store_factory is not None
        else build_shared_record_store(database_url=database_url)
    )
    resolved_ingress_provider_factory = ingress_provider_factory
    if resolved_ingress_provider_factory is None:
        if npmplus_ingress_client_factory is not None:

            def npmplus_ingress_provider_from_client_factory() -> IngressProvider:
                return NpmplusIngressProvider(client=npmplus_ingress_client_factory())

            resolved_ingress_provider_factory = npmplus_ingress_provider_from_client_factory
        else:
            resolved_ingress_provider_factory = default_ingress_provider

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            if isinstance(shared_record_store, PostgresRecordStore):
                shared_record_store.close()

    app = FastAPI(title="Launchplane API", version="0.1.0", lifespan=lifespan)

    def next_trace_id() -> str:
        return f"launchplane_req_{uuid4().hex}"

    def get_record_store() -> object:
        if record_store_factory is not None:
            return record_store_factory()
        if shared_record_store is None:
            raise RuntimeError("Launchplane record store is not initialized.")
        return shared_record_store

    def read_human_session(
        *,
        cookie_header: str,
    ) -> tuple[LaunchplaneHumanSession, bool] | None:
        if human_session_manager is None:
            return None
        session = human_session_manager.read_cookie(cookie_header)
        if session is None:
            return None
        renewed_session = human_session_manager.renew_if_needed(session)
        if renewed_session is None:
            return None
        return renewed_session, renewed_session.expires_at != session.expires_at

    def read_human_session_identity(
        *,
        cookie_header: str,
        request: Request,
        response: Response,
    ) -> GitHubHumanIdentity | None:
        session_result = read_human_session(cookie_header=cookie_header)
        if session_result is None:
            return None
        session, was_renewed = session_result
        if was_renewed and human_session_manager is not None:
            session_cookie_header = human_session_manager.session_cookie_header(session)
            response.headers.append("Set-Cookie", session_cookie_header)
            request.state.launchplane_renewed_session_cookie = session_cookie_header
        return session.identity

    def read_identity(
        request: Request,
        response: Response,
        authorization: Annotated[str, Header(alias="Authorization")] = "",
        cookie: Annotated[str, Header(alias="Cookie")] = "",
    ) -> LaunchplaneIdentity:
        header = authorization.strip()
        if header:
            scheme, _, token = header.partition(" ")
            bearer_token = token.strip()
            if scheme.lower() == "bearer" and bearer_token:
                try:
                    owner_agent_identity = bearer_identity_from_token(
                        token=bearer_token,
                        config=bearer_identity_config or BearerIdentityConfig(),
                    )
                except PermissionError as error:
                    raise _authentication_required_error(str(error)) from error
                if owner_agent_identity is not None:
                    return owner_agent_identity
                try:
                    return verifier.verify(bearer_token)
                except (InvalidTokenError, ValueError) as error:
                    raise _authentication_required_error(str(error)) from error
        human_identity = read_human_session_identity(
            cookie_header=cookie,
            request=request,
            response=response,
        )
        if human_identity is not None:
            return human_identity
        raise _authentication_required_error("Authorization header is required.")

    def read_work_graph_rank_identity(
        request: Request,
        response: Response,
        authorization: Annotated[str, Header(alias="Authorization")] = "",
        cookie: Annotated[str, Header(alias="Cookie")] = "",
    ) -> GitHubActionsIdentity | GitHubHumanIdentity:
        human_identity = read_human_session_identity(
            cookie_header=cookie,
            request=request,
            response=response,
        )
        if human_identity is not None:
            return human_identity
        header = authorization.strip()
        if not header:
            raise _authentication_required_error("Authorization header is required.")
        scheme, _, token = header.partition(" ")
        bearer_token = token.strip()
        if scheme.lower() != "bearer" or not bearer_token:
            raise _authentication_required_error("Bearer token is required.")
        try:
            owner_agent_identity = bearer_identity_from_token(
                token=bearer_token,
                config=bearer_identity_config or BearerIdentityConfig(),
            )
        except PermissionError as error:
            raise _authentication_required_error(str(error)) from error
        if owner_agent_identity is not None:
            raise _launchplane_http_error(
                status_code=403,
                trace_id=next_trace_id(),
                code="authorization_denied",
                message=("Work graph rank requires GitHub Actions OIDC or a GitHub human session."),
            )
        try:
            return verifier.verify(bearer_token)
        except (InvalidTokenError, ValueError) as error:
            raise _authentication_required_error(str(error)) from error

    def every_code_worker_token_authorized(authorization: str) -> bool:
        if bearer_identity_config is None:
            return False
        expected_token = bearer_identity_config.every_code_worker_token.strip()
        if not expected_token:
            return False
        header = authorization.strip()
        scheme, _, token = header.partition(" ")
        bearer_token = token.strip()
        if scheme.lower() != "bearer" or not bearer_token:
            return False
        return secrets.compare_digest(bearer_token, expected_token)

    def read_product_profile_list_identity(
        request: Request,
        response: Response,
        authorization: Annotated[str, Header(alias="Authorization")] = "",
        cookie: Annotated[str, Header(alias="Cookie")] = "",
    ) -> LaunchplaneIdentity | None:
        if every_code_worker_token_authorized(authorization):
            return None
        return read_identity(
            request=request,
            response=response,
            authorization=authorization,
            cookie=cookie,
        )

    def read_every_code_worker_read_identity(
        request: Request,
        response: Response,
        authorization: Annotated[str, Header(alias="Authorization")] = "",
        cookie: Annotated[str, Header(alias="Cookie")] = "",
    ) -> LaunchplaneIdentity | None:
        if every_code_worker_token_authorized(authorization):
            return None
        return read_identity(
            request=request,
            response=response,
            authorization=authorization,
            cookie=cookie,
        )

    def require_every_code_worker_write_token(
        authorization: Annotated[str, Header(alias="Authorization")] = "",
    ) -> None:
        if every_code_worker_token_authorized(authorization):
            return
        raise _authentication_required_error("Every Code worker token is required.")

    def read_write_identity(
        authorization: Annotated[str, Header(alias="Authorization")] = "",
    ) -> LaunchplaneIdentity:
        header = authorization.strip()
        if not header:
            raise _authentication_required_error("Authorization header is required.")
        scheme, _, token = header.partition(" ")
        bearer_token = token.strip()
        if scheme.lower() != "bearer" or not bearer_token:
            raise _authentication_required_error("Bearer token is required.")
        try:
            owner_agent_identity = bearer_identity_from_token(
                token=bearer_token,
                config=bearer_identity_config or BearerIdentityConfig(),
            )
        except PermissionError as error:
            raise _authentication_required_error(str(error)) from error
        if isinstance(owner_agent_identity, LocalAdminIdentity | LocalOperatorIdentity):
            return owner_agent_identity
        if owner_agent_identity is not None:
            raise _launchplane_http_error(
                status_code=403,
                trace_id=next_trace_id(),
                code="authorization_denied",
                message=("Terminal agent credentials can only read redacted Launchplane context."),
            )
        try:
            oidc_identity = verifier.verify(bearer_token)
        except (InvalidTokenError, ValueError) as error:
            raise _authentication_required_error(str(error)) from error
        if not isinstance(oidc_identity, GitHubActionsIdentity):
            raise _authentication_required_error("Mutation routes require GitHub Actions OIDC.")
        return oidc_identity

    def require_launchplane_service_read_authorization(
        *, identity: LaunchplaneIdentity, trace_id: str
    ) -> None:
        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action="launchplane_service.read",
            product="launchplane",
            context=_LAUNCHPLANE_SERVICE_CONTEXT,
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Workflow cannot read Launchplane service runtime state.",
            )

    def require_launchplane_service_reconcile_authorization(
        *, identity: LaunchplaneIdentity, trace_id: str
    ) -> None:
        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action="launchplane_service.reconcile_odoo_workers",
            product="launchplane",
            context=_LAUNCHPLANE_SERVICE_CONTEXT,
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Workflow cannot reconcile Launchplane Odoo workers.",
            )

    def read_launchplane_runtime(
        identity: Annotated[LaunchplaneIdentity, Depends(read_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
    ) -> LaunchplaneRuntimeResponse:
        trace_id = next_trace_id()
        require_launchplane_service_read_authorization(identity=identity, trace_id=trace_id)
        runtime = LaunchplaneRuntimeStatus.model_validate(
            control_plane_service_status.launchplane_runtime_payload(
                storage_backend=storage_backend_name(record_store),
                authz_policy_sha256_value=resolved_authz_policy_runtime.policy_sha256,
                authz_policy_source=resolved_authz_policy_runtime.source,
            )
        )
        return LaunchplaneRuntimeResponse(trace_id=trace_id, runtime=runtime)

    def read_odoo_stable_operation_worker_status(
        identity: Annotated[LaunchplaneIdentity, Depends(read_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
        recent_terminal_limit: Annotated[str, Query()] = "10",
    ) -> OdooStableOperationWorkerStatusResponse:
        trace_id = next_trace_id()
        require_launchplane_service_read_authorization(identity=identity, trace_id=trace_id)
        try:
            parsed_recent_terminal_limit = control_plane_service_status.query_int_value(
                recent_terminal_limit,
                "recent_terminal_limit",
                default=10,
                minimum=0,
                maximum=100,
            )
            assert parsed_recent_terminal_limit is not None
            worker_status = OdooStableOperationWorkerStatusResponseModel.model_validate(
                control_plane_service_status.odoo_stable_operation_worker_status_payload(
                    record_store=record_store,
                    recent_terminal_limit=parsed_recent_terminal_limit,
                )
            )
        except click.ClickException as error:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="operation_record_storage_required",
                message=str(error),
            ) from error
        except ValueError as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_query",
                message=str(error),
            ) from error
        return OdooStableOperationWorkerStatusResponse(
            trace_id=trace_id,
            worker_status=worker_status,
        )

    def reconcile_odoo_stable_operation_workers(
        identity: Annotated[LaunchplaneIdentity, Depends(read_write_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
        max_attempts: Annotated[str, Query()] = str(DEFAULT_ODOO_STABLE_WORKER_MAX_ATTEMPTS),
    ) -> OdooStableOperationWorkerReconcileResponse:
        trace_id = next_trace_id()
        require_launchplane_service_reconcile_authorization(identity=identity, trace_id=trace_id)
        try:
            parsed_max_attempts = control_plane_service_status.query_int_value(
                max_attempts,
                "max_attempts",
                default=DEFAULT_ODOO_STABLE_WORKER_MAX_ATTEMPTS,
                minimum=1,
                maximum=100,
            )
            assert parsed_max_attempts is not None
            reconcile_result = reconcile_stale_odoo_stable_operation_records(
                record_store=control_plane_service_status.require_odoo_stable_operation_worker_store(
                    record_store
                ),
                max_attempts=parsed_max_attempts,
            )
        except click.ClickException as error:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="operation_record_storage_required",
                message=str(error),
            ) from error
        except ValueError as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_query",
                message=str(error),
            ) from error
        reconciled_bootstrap_ids = tuple(reconcile_result.reconciled_bootstrap_ids)
        reconciled_replacement_ids = tuple(reconcile_result.reconciled_replacement_ids)
        return OdooStableOperationWorkerReconcileResponse(
            trace_id=trace_id,
            reconcile_result=OdooStableOperationWorkerReconcileResultResponse(
                reconciled_bootstrap_ids=reconciled_bootstrap_ids,
                reconciled_replacement_ids=reconciled_replacement_ids,
                reconciled_count=len(reconciled_bootstrap_ids) + len(reconciled_replacement_ids),
            ),
        )

    def read_odoo_stable_bootstrap_operation_status(
        operation_id: Annotated[str, Path(min_length=1, pattern=r"^\S+$")],
        identity: Annotated[LaunchplaneIdentity, Depends(read_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
    ) -> OdooStableBootstrapOperationStatusResponse:
        trace_id = next_trace_id()
        try:
            operation_store = require_odoo_stable_bootstrap_operation_read_store(record_store)
            operation = operation_store.read_odoo_stable_bootstrap_operation_record(operation_id)
        except TypeError as error:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message=str(error),
            ) from error
        except FileNotFoundError as error:
            raise _launchplane_http_error(
                status_code=404,
                trace_id=trace_id,
                code="not_found",
                message=str(error),
            ) from error
        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action="odoo_stable_bootstrap.execute",
            product=operation.product,
            context=operation.context,
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message=(
                    "Workflow cannot read Odoo stable bootstrap operation status "
                    "for the requested product/context."
                ),
            )
        result = operation.result.model_dump(mode="json") if operation.result else None
        return OdooStableBootstrapOperationStatusResponse(
            trace_id=trace_id,
            operation=odoo_stable_bootstrap_operation_status_payload(operation),
            result=result,
        )

    def read_odoo_stable_target_replacement_operation_status(
        operation_id: Annotated[str, Path(min_length=1, pattern=r"^\S+$")],
        identity: Annotated[LaunchplaneIdentity, Depends(read_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
    ) -> OdooStableTargetReplacementOperationStatusResponse:
        trace_id = next_trace_id()
        try:
            operation_store = require_odoo_stable_target_replacement_operation_read_store(
                record_store
            )
            operation = operation_store.read_odoo_stable_target_replacement_operation_record(
                operation_id
            )
        except TypeError as error:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message=str(error),
            ) from error
        except FileNotFoundError as error:
            raise _launchplane_http_error(
                status_code=404,
                trace_id=trace_id,
                code="not_found",
                message=str(error),
            ) from error
        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action="odoo_target_replacement_apply.execute",
            product=operation.product,
            context=operation.context,
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message=(
                    "Workflow cannot read Odoo target replacement operation status "
                    "for the requested product/context."
                ),
            )
        result = operation.result.model_dump(mode="json") if operation.result else None
        return OdooStableTargetReplacementOperationStatusResponse(
            trace_id=trace_id,
            operation=odoo_stable_target_replacement_operation_status_payload(operation),
            result=result,
        )

    def read_product_environment_config_status(
        product: Annotated[str, Path(min_length=1, pattern=r"^\S+$")],
        environment: Annotated[str, Path(min_length=1, pattern=r"^\S+$")],
        identity: Annotated[LaunchplaneIdentity, Depends(read_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
    ) -> ProductEnvironmentConfigStatusResponse:
        trace_id = next_trace_id()

        def product_action_allowed(
            requested_action: str, requested_product: str, requested_context: str
        ) -> bool:
            return resolved_authz_policy_runtime.policy.allows(
                identity=identity,
                action=requested_action,
                product=requested_product,
                context=requested_context,
            )

        try:
            product_read_store = (
                control_plane_product_read_service.require_product_environment_read_model_store(
                    record_store
                )
            )
            product_read_result = (
                control_plane_product_read_service.build_product_environment_read_service_result(
                    record_store=product_read_store,
                    params={
                        "product": product,
                        "environment": environment,
                        "config_status": "true",
                    },
                    action_allowed=product_action_allowed,
                )
            )
        except control_plane_product_read_service.ProductReadModelStoreCapabilityError as error:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message=str(error),
            ) from error
        except FileNotFoundError as error:
            raise _launchplane_http_error(
                status_code=404,
                trace_id=trace_id,
                code="not_found",
                message=str(error),
            ) from error
        except ValueError as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message=str(error),
            ) from error

        if not product_action_allowed(
            "product_environment.read",
            product_read_result.authorization_product,
            product_read_result.authorization_context,
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message=product_read_result.denial_message,
            )

        config_status = ProductEnvironmentConfigStatus.model_validate(
            product_read_result.payload["config_status"]
        )
        return ProductEnvironmentConfigStatusResponse(
            trace_id=trace_id,
            config_status=config_status,
        )

    def product_environment_action_allowed(
        identity: LaunchplaneIdentity,
        requested_action: str,
        requested_product: str,
        requested_context: str,
    ) -> bool:
        return resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action=requested_action,
            product=requested_product,
            context=requested_context,
        )

    def build_product_environment_read_result(
        *,
        identity: LaunchplaneIdentity,
        record_store: object,
        trace_id: str,
        params: dict[str, str],
    ) -> control_plane_product_read_service.ProductEnvironmentReadServiceResult:
        def action_allowed(
            requested_action: str, requested_product: str, requested_context: str
        ) -> bool:
            return product_environment_action_allowed(
                identity,
                requested_action,
                requested_product,
                requested_context,
            )

        try:
            product_read_store = (
                control_plane_product_read_service.require_product_environment_read_model_store(
                    record_store
                )
            )
            return control_plane_product_read_service.build_product_environment_read_service_result(
                record_store=product_read_store,
                params=params,
                action_allowed=action_allowed,
            )
        except control_plane_product_read_service.ProductReadModelStoreCapabilityError as error:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message=str(error),
            ) from error
        except FileNotFoundError as error:
            raise _launchplane_http_error(
                status_code=404,
                trace_id=trace_id,
                code="not_found",
                message=str(error),
            ) from error
        except ValueError as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message=str(error),
            ) from error

    def require_product_environment_result_authorization(
        *,
        identity: LaunchplaneIdentity,
        trace_id: str,
        result: control_plane_product_read_service.ProductEnvironmentReadServiceResult,
    ) -> None:
        if product_environment_action_allowed(
            identity,
            "product_environment.read",
            result.authorization_product,
            result.authorization_context,
        ):
            return
        raise _launchplane_http_error(
            status_code=403,
            trace_id=trace_id,
            code="authorization_denied",
            message=result.denial_message,
        )

    def require_agent_context_read_authorization(
        *, identity: LaunchplaneIdentity, trace_id: str, message: str
    ) -> None:
        if agent_context_allowed(
            authz_policy=resolved_authz_policy_runtime.policy,
            identity=identity,
        ):
            return
        raise _launchplane_http_error(
            status_code=403,
            trace_id=trace_id,
            code="authorization_denied",
            message=message,
        )

    def require_work_graph_rank_authorization(
        *, identity: LaunchplaneIdentity, trace_id: str, message: str
    ) -> None:
        if resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action="work_graph.rank",
            product="launchplane",
            context=_LAUNCHPLANE_SERVICE_CONTEXT,
        ):
            return
        raise _launchplane_http_error(
            status_code=403,
            trace_id=trace_id,
            code="authorization_denied",
            message=message,
        )

    def require_work_graph_issue_inbox_reconcile_authorization(
        *, identity: LaunchplaneIdentity, trace_id: str, action: str
    ) -> None:
        if resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action=action,
            product="launchplane",
            context=_LAUNCHPLANE_SERVICE_CONTEXT,
        ):
            return
        raise _launchplane_http_error(
            status_code=403,
            trace_id=trace_id,
            code="authorization_denied",
            message="Workflow cannot reconcile the Launchplane GitHub issue inbox.",
        )

    def require_read_store_methods(
        record_store: object,
        *,
        trace_id: str,
        method_names: tuple[str, ...],
        message: str,
    ) -> None:
        missing_methods = tuple(
            method_name
            for method_name in method_names
            if not callable(getattr(record_store, method_name, None))
        )
        if not missing_methods:
            if isinstance(record_store, PostgresRecordStore):
                return
            missing_methods = ("postgres_storage",)
        missing_method_list = ", ".join(missing_methods)
        raise _launchplane_http_error(
            status_code=503,
            trace_id=trace_id,
            code="database_storage_required",
            message=f"{message} Missing store method(s): {missing_method_list}.",
        )

    def require_repo_product_mapping_read_store(
        record_store: object, *, trace_id: str
    ) -> RepoProductMappingReadStore:
        require_read_store_methods(
            record_store,
            trace_id=trace_id,
            method_names=(
                "list_product_profile_records",
                "list_every_code_work_request_records",
            ),
            message="Repo product mapping reads require a database-backed record store.",
        )
        return cast(RepoProductMappingReadStore, record_store)

    def require_agent_context_read_store(
        record_store: object, *, trace_id: str
    ) -> AgentContextReadStore:
        require_read_store_methods(
            record_store,
            trace_id=trace_id,
            method_names=(
                "list_product_profile_records",
                "read_product_profile_record",
                "list_every_code_work_request_records",
                "list_every_code_preview_gate_records",
            ),
            message="Agent context reads require a database-backed record store.",
        )
        return cast(AgentContextReadStore, record_store)

    def require_work_graph_snapshot_read_store(
        record_store: object, *, trace_id: str
    ) -> WorkGraphSnapshotReadStore:
        require_read_store_methods(
            record_store,
            trace_id=trace_id,
            method_names=(
                "list_product_profile_records",
                "list_every_code_work_request_records",
            ),
            message="Work graph snapshot reads require a database-backed record store.",
        )
        return cast(WorkGraphSnapshotReadStore, record_store)

    def require_every_code_read_methods(
        record_store: object,
        *,
        required_methods: tuple[str, ...],
        capability: str,
    ) -> None:
        missing_methods = [
            method_name
            for method_name in required_methods
            if not callable(getattr(record_store, method_name, None))
        ]
        if missing_methods:
            missing_summary = ", ".join(missing_methods)
            raise TypeError(f"record store does not support {capability}: {missing_summary}")

    def require_every_code_work_request_list_store(
        record_store: object,
    ) -> _EveryCodeWorkRequestListStore:
        require_every_code_read_methods(
            record_store,
            required_methods=("list_every_code_work_request_records",),
            capability="Every Code work request list reads",
        )
        return cast(_EveryCodeWorkRequestListStore, record_store)

    def require_every_code_work_request_record_store(
        record_store: object,
    ) -> _EveryCodeWorkRequestRecordStore:
        require_every_code_read_methods(
            record_store,
            required_methods=("read_every_code_work_request_record",),
            capability="Every Code work request record reads",
        )
        return cast(_EveryCodeWorkRequestRecordStore, record_store)

    def require_every_code_work_request_write_store(
        record_store: object,
    ) -> _EveryCodeWorkRequestWriteStore:
        require_every_code_read_methods(
            record_store,
            required_methods=("write_every_code_work_request_record",),
            capability="Every Code work request writes",
        )
        return cast(_EveryCodeWorkRequestWriteStore, record_store)

    def require_every_code_pr_feedback_read_store(
        record_store: object,
    ) -> _EveryCodePrFeedbackReadStore:
        require_every_code_read_methods(
            record_store,
            required_methods=("list_every_code_pr_feedback_records",),
            capability="Every Code PR feedback reads",
        )
        return cast(_EveryCodePrFeedbackReadStore, record_store)

    def require_every_code_pr_feedback_write_store(
        record_store: object,
    ) -> _EveryCodePrFeedbackWriteStore:
        require_every_code_read_methods(
            record_store,
            required_methods=("write_every_code_pr_feedback_record",),
            capability="Every Code PR feedback writes",
        )
        return cast(_EveryCodePrFeedbackWriteStore, record_store)

    def require_every_code_pr_feedback_status_store(
        record_store: object,
    ) -> _EveryCodePrFeedbackStatusStore:
        require_every_code_read_methods(
            record_store,
            required_methods=(
                "list_every_code_pr_feedback_records",
                "write_every_code_pr_feedback_record",
            ),
            capability="Every Code PR feedback status writes",
        )
        return cast(_EveryCodePrFeedbackStatusStore, record_store)

    def require_every_code_preview_gate_read_store(
        record_store: object,
    ) -> _EveryCodePreviewGateReadStore:
        require_every_code_read_methods(
            record_store,
            required_methods=("list_every_code_preview_gate_records",),
            capability="Every Code preview gate reads",
        )
        return cast(_EveryCodePreviewGateReadStore, record_store)

    def require_every_code_preview_gate_write_store(
        record_store: object,
    ) -> _EveryCodePreviewGateWriteStore:
        require_every_code_read_methods(
            record_store,
            required_methods=("write_every_code_preview_gate_record",),
            capability="Every Code preview gate writes",
        )
        return cast(_EveryCodePreviewGateWriteStore, record_store)

    def require_every_code_notification_attempt_read_store(
        record_store: object,
    ) -> _EveryCodeNotificationAttemptReadStore:
        list_records = getattr(record_store, "list_every_code_notification_attempt_records", None)
        if not callable(list_records):
            raise TypeError("record store does not support Every Code notification attempt reads")
        return cast(_EveryCodeNotificationAttemptReadStore, record_store)

    def require_preview_pr_feedback_notification_attempt_read_store(
        record_store: object,
    ) -> _PreviewPrFeedbackNotificationAttemptReadStore:
        list_records = getattr(
            record_store,
            "list_preview_pr_feedback_notification_attempt_records",
            None,
        )
        if not callable(list_records):
            raise TypeError(
                "record store does not support preview PR feedback notification attempt reads"
            )
        return cast(_PreviewPrFeedbackNotificationAttemptReadStore, record_store)

    def ensure_every_code_read_allowed(
        *,
        identity: LaunchplaneIdentity | None,
        trace_id: str,
        action: str,
        message: str,
    ) -> None:
        if identity is None:
            return
        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action=action,
            product="launchplane",
            context=_LAUNCHPLANE_SERVICE_CONTEXT,
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message=message,
            )

    def every_code_pagination_value(
        raw_value: str,
        key: str,
        *,
        default: int,
        trace_id: str,
    ) -> int:
        try:
            value = int(raw_value.strip() or str(default))
        except ValueError as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_payload",
                message=f"Every Code pagination {key} must be an integer",
            ) from error
        if value < 0:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_payload",
                message=f"Every Code pagination {key} must be non-negative",
            )
        return value

    def every_code_optional_int(raw_value: str, key: str, *, trace_id: str) -> int | None:
        normalized_value = raw_value.strip()
        if not normalized_value:
            return None
        try:
            return int(normalized_value)
        except ValueError as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_payload",
                message=f"Query parameter {key} must be an integer",
            ) from error

    def every_code_read_store_or_503(
        record_store: object, *, trace_id: str, capability: str
    ) -> object:
        try:
            if capability == "work_request_list":
                return require_every_code_work_request_list_store(record_store)
            if capability == "work_request_record":
                return require_every_code_work_request_record_store(record_store)
            if capability == "pr_feedback":
                return require_every_code_pr_feedback_read_store(record_store)
            if capability == "preview_gate":
                return require_every_code_preview_gate_read_store(record_store)
            raise TypeError(f"unknown Every Code read capability: {capability}")
        except TypeError as error:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message=str(error),
            ) from error

    def every_code_invalid_payload_error(*, trace_id: str, error: ValueError) -> HTTPException:
        return _launchplane_http_error(
            status_code=400,
            trace_id=trace_id,
            code="invalid_payload",
            message=str(error),
        )

    def read_repo_product_mapping(
        identity: Annotated[LaunchplaneIdentity, Depends(read_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
    ) -> RepoProductMappingResponse:
        trace_id = next_trace_id()
        require_agent_context_read_authorization(
            identity=identity,
            trace_id=trace_id,
            message="Workflow cannot read the Launchplane repo product mapping.",
        )
        mapping_store = require_repo_product_mapping_read_store(
            record_store,
            trace_id=trace_id,
        )
        payload = build_repo_product_mapping_service_payload(
            generated_at=utc_now_timestamp(),
            product_store=mapping_store,
            work_request_store=mapping_store,
        )
        return RepoProductMappingResponse(
            trace_id=trace_id,
            mapping=RepoProductMapping.model_validate(payload["mapping"]),
            source=cast(dict[str, object], payload["source"]),
        )

    def read_agent_context(
        identity: Annotated[LaunchplaneIdentity, Depends(read_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
        repository: Annotated[str, Query()] = "",
    ) -> AgentContextResponse:
        trace_id = next_trace_id()
        require_agent_context_read_authorization(
            identity=identity,
            trace_id=trace_id,
            message="Workflow cannot read Launchplane agent context.",
        )
        context_store = require_agent_context_read_store(record_store, trace_id=trace_id)
        context = build_agent_context_service_payload(
            generated_at=utc_now_timestamp(),
            repository=repository,
            product_store=context_store,
            work_request_store=context_store,
            preview_readiness_store=context_store,
            action_allowed=agent_context_action_allowed(
                authz_policy=resolved_authz_policy_runtime.policy,
                identity=identity,
            ),
            planning_facts_provider=work_graph_planning_facts_provider,
        )
        return AgentContextResponse(trace_id=trace_id, context=context)

    def work_graph_product_action_allowed(*, identity: LaunchplaneIdentity) -> ActionAllowed:
        def action_allowed(
            requested_action: str,
            requested_product: str,
            requested_context: str,
        ) -> bool:
            return resolved_authz_policy_runtime.policy.allows(
                identity=identity,
                action=requested_action,
                product=requested_product,
                context=requested_context,
            )

        return action_allowed

    def read_work_graph_snapshot(
        identity: Annotated[LaunchplaneIdentity, Depends(read_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
    ) -> WorkGraphSnapshotResponse:
        trace_id = next_trace_id()
        require_work_graph_rank_authorization(
            identity=identity,
            trace_id=trace_id,
            message="Workflow cannot read the Launchplane work graph snapshot.",
        )
        snapshot_store = require_work_graph_snapshot_read_store(
            record_store,
            trace_id=trace_id,
        )
        payload = build_work_graph_snapshot_service_payload(
            generated_at=utc_now_timestamp(),
            product_store=snapshot_store,
            work_request_store=snapshot_store,
            action_allowed=work_graph_product_action_allowed(identity=identity),
            planning_facts_provider=work_graph_planning_facts_provider,
        )
        return WorkGraphSnapshotResponse(
            trace_id=trace_id,
            snapshot=WorkGraphSnapshot.model_validate(payload["snapshot"]),
            source=cast(dict[str, object], payload["source"]),
        )

    def rank_work_graph_snapshot(
        payload: WorkGraphRankEnvelope,
        identity: Annotated[
            GitHubActionsIdentity | GitHubHumanIdentity,
            Depends(read_work_graph_rank_identity),
        ],
    ) -> AcceptedEvidenceResponse:
        trace_id = next_trace_id()
        require_work_graph_rank_authorization(
            identity=identity,
            trace_id=trace_id,
            message="Workflow cannot rank the Launchplane work graph.",
        )
        _summary, driver_result = build_work_graph_rank_result(payload)
        return AcceptedEvidenceResponse(
            trace_id=trace_id,
            records={},
            result=driver_result,
        )

    def read_work_graph_issue_inbox(
        identity: Annotated[LaunchplaneIdentity, Depends(read_identity)],
    ) -> WorkGraphIssueInboxResponse:
        trace_id = next_trace_id()
        require_work_graph_rank_authorization(
            identity=identity,
            trace_id=trace_id,
            message="Workflow cannot read the Launchplane GitHub issue inbox.",
        )
        if work_graph_issue_inbox_provider is None:
            return WorkGraphIssueInboxResponse(
                trace_id=trace_id,
                configured=False,
                inbox=GitHubIssueInboxReadModel(
                    generated_at="",
                    repository_count=0,
                    issue_count=0,
                ),
            )
        return WorkGraphIssueInboxResponse(
            trace_id=trace_id,
            configured=True,
            inbox=work_graph_issue_inbox_provider(),
        )

    def reconcile_work_graph_issue_inbox(
        payload: GitHubIssueInboxReconcileRequest,
        identity: Annotated[LaunchplaneIdentity, Depends(read_write_identity)],
    ) -> AcceptedEvidenceResponse:
        trace_id = next_trace_id()
        required_action = (
            "work_graph.rank" if payload.mode == "dry_run" else "work_graph.issue_inbox.reconcile"
        )
        require_work_graph_issue_inbox_reconcile_authorization(
            identity=identity,
            trace_id=trace_id,
            action=required_action,
        )
        if work_graph_issue_inbox_reconcile_provider is None:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="GitHub issue inbox reconciliation is not configured.",
            )
        try:
            reconcile_result = work_graph_issue_inbox_reconcile_provider(payload)
        except ValueError as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message=str(error) or "Request could not be completed.",
            ) from error
        return AcceptedEvidenceResponse(
            trace_id=trace_id,
            records={},
            result={"reconcile": reconcile_result.model_dump(mode="json")},
        )

    def merge_train_policy_not_configured_error(
        *, trace_id: str, error: MergeTrainPolicyStoreMissingError
    ) -> HTTPException:
        return _launchplane_http_error(
            status_code=503,
            trace_id=trace_id,
            code="merge_train_policy_not_configured",
            message=str(error) or "No active DB-backed merge train policy record is configured.",
        )

    def merge_train_invalid_request_error(*, trace_id: str, error: ValueError) -> HTTPException:
        return _launchplane_http_error(
            status_code=400,
            trace_id=trace_id,
            code="invalid_request",
            message=str(error),
        )

    def merge_train_admission_query(
        *, repository: str, base_branch: str, trace_id: str
    ) -> MergeTrainAdmissionQuery:
        try:
            return MergeTrainAdmissionQuery.model_validate(
                {"repository": repository, "base_branch": base_branch}
            )
        except ValueError as error:
            raise merge_train_invalid_request_error(trace_id=trace_id, error=error) from error

    def read_merge_train_admission(
        identity: Annotated[LaunchplaneIdentity, Depends(read_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
        repository: Annotated[str, Query()] = "",
        base_branch: Annotated[str, Query()] = "main",
    ) -> MergeTrainAdmissionResponse:
        trace_id = next_trace_id()
        admission_request = merge_train_admission_query(
            repository=repository,
            base_branch=base_branch,
            trace_id=trace_id,
        )
        try:
            policy_record = resolve_merge_train_policy_record(record_store)
        except MergeTrainPolicyStoreMissingError as error:
            raise merge_train_policy_not_configured_error(trace_id=trace_id, error=error) from error
        try:
            repository_policy = policy_record.policy.find_repository_policy(
                repository=admission_request.repository,
                base_branch=admission_request.base_branch,
            )
        except ValueError as error:
            raise merge_train_invalid_request_error(trace_id=trace_id, error=error) from error
        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action=repository_policy.service_authz.action,
            product=repository_policy.service_authz.product,
            context=repository_policy.service_authz.context,
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Workflow cannot read the requested merge train admission decision.",
            )
        merge_train_store = cast(MergeTrainRunHistoryStore, record_store)
        admission_decision = evaluate_merge_train_admission_from_store(
            store=merge_train_store,
            repository=admission_request.repository,
            base_branch=admission_request.base_branch,
            requested_at=utc_now_timestamp(),
            current_policy_key=repository_policy.policy_key,
            current_policy_sha256=policy_record.policy_sha256,
        )
        return MergeTrainAdmissionResponse(
            trace_id=trace_id,
            admission=admission_decision.model_dump(mode="json"),
        )

    def read_merge_train_controller_status(
        identity: Annotated[LaunchplaneIdentity, Depends(read_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
        repository: Annotated[str, Query()] = "",
        base_branch: Annotated[str, Query()] = "main",
    ) -> MergeTrainControllerStatusResponse:
        trace_id = next_trace_id()
        status_request = merge_train_admission_query(
            repository=repository,
            base_branch=base_branch,
            trace_id=trace_id,
        )
        try:
            policy_record = resolve_merge_train_policy_record(record_store)
        except MergeTrainPolicyStoreMissingError as error:
            raise merge_train_policy_not_configured_error(trace_id=trace_id, error=error) from error
        try:
            repository_policy = policy_record.policy.find_repository_policy(
                repository=status_request.repository,
                base_branch=status_request.base_branch,
            )
        except ValueError as error:
            raise merge_train_invalid_request_error(trace_id=trace_id, error=error) from error
        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action=repository_policy.service_authz.action,
            product=repository_policy.service_authz.product,
            context=repository_policy.service_authz.context,
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Workflow cannot read the requested merge train controller status.",
            )
        merge_train_store = cast(MergeTrainRunHistoryStore, record_store)
        read_model = build_merge_train_controller_status_read_model(
            store=merge_train_store,
            repository=status_request.repository,
            base_branch=status_request.base_branch,
            generated_at=utc_now_timestamp(),
            current_policy_key=repository_policy.policy_key,
            current_policy_sha256=policy_record.policy_sha256,
        )
        return MergeTrainControllerStatusResponse(
            trace_id=trace_id,
            controller_status=read_model.model_dump(mode="json"),
        )

    def read_merge_train_policy_targets(
        identity: Annotated[LaunchplaneIdentity, Depends(read_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
    ) -> MergeTrainPolicyTargetsResponse:
        trace_id = next_trace_id()
        try:
            policy_record = resolve_merge_train_policy_record(record_store)
        except MergeTrainPolicyStoreMissingError as error:
            raise merge_train_policy_not_configured_error(trace_id=trace_id, error=error) from error
        targets: list[dict[str, object]] = []
        local_operator_can_read_targets = resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action="merge_train.policy_targets",
            product="launchplane",
            context=_LAUNCHPLANE_SERVICE_CONTEXT,
        )
        for repository_policy in policy_record.policy.policies:
            service_authz_allowed = resolved_authz_policy_runtime.policy.allows(
                identity=identity,
                action=repository_policy.service_authz.action,
                product=repository_policy.service_authz.product,
                context=repository_policy.service_authz.context,
            )
            if not service_authz_allowed and not local_operator_can_read_targets:
                continue
            targets.append(
                {
                    "repository": repository_policy.repository,
                    "base_branch": repository_policy.base_branch,
                    "policy_key": repository_policy.policy_key,
                    "scheduler": repository_policy.scheduler.model_dump(mode="json"),
                    "service_authz": repository_policy.service_authz.model_dump(mode="json"),
                }
            )
        targets.sort(key=lambda target: (str(target["repository"]), str(target["base_branch"])))
        return MergeTrainPolicyTargetsResponse(
            trace_id=trace_id,
            policy={
                "record_id": policy_record.record_id,
                "updated_at": policy_record.updated_at,
                "policy_sha256": policy_record.policy_sha256,
            },
            targets=targets,
        )

    def read_every_code_summary(
        identity: Annotated[
            LaunchplaneIdentity | None, Depends(read_every_code_worker_read_identity)
        ],
        record_store: Annotated[object, Depends(get_record_store)],
        repository: Annotated[str, Query()] = "",
        issue_number: Annotated[str, Query()] = "",
        state: Annotated[str, Query()] = "",
        limit: Annotated[str, Query()] = "50",
        offset: Annotated[str, Query()] = "0",
    ) -> EveryCodeSummaryResponse:
        trace_id = next_trace_id()
        ensure_every_code_read_allowed(
            identity=identity,
            trace_id=trace_id,
            action="every_code_work_request.read",
            message="Workflow cannot read Every Code work requests.",
        )
        every_code_store = cast(
            _EveryCodeWorkRequestListStore,
            every_code_read_store_or_503(
                record_store,
                trace_id=trace_id,
                capability="work_request_list",
            ),
        )
        try:
            summary = build_every_code_summary_read_model(
                generated_at=utc_now_timestamp(),
                record_store=every_code_store,
                repository=repository.strip(),
                issue_number=every_code_optional_int(
                    issue_number,
                    "issue_number",
                    trace_id=trace_id,
                ),
                state=state.strip(),
                limit=every_code_pagination_value(
                    limit,
                    "limit",
                    default=50,
                    trace_id=trace_id,
                ),
                offset=every_code_pagination_value(
                    offset,
                    "offset",
                    default=0,
                    trace_id=trace_id,
                ),
            )
        except ValueError as error:
            raise every_code_invalid_payload_error(trace_id=trace_id, error=error) from error
        return EveryCodeSummaryResponse(trace_id=trace_id, summary=summary)

    def read_preview_readiness(
        identity: Annotated[
            LaunchplaneIdentity | None, Depends(read_every_code_worker_read_identity)
        ],
        record_store: Annotated[object, Depends(get_record_store)],
        repository: Annotated[str, Query()] = "",
        pr_number: Annotated[str, Query()] = "",
        status: Annotated[str, Query()] = "",
        limit: Annotated[str, Query()] = "50",
        offset: Annotated[str, Query()] = "0",
    ) -> PreviewReadinessResponse:
        trace_id = next_trace_id()
        ensure_every_code_read_allowed(
            identity=identity,
            trace_id=trace_id,
            action="every_code_preview_gate.read",
            message="Workflow cannot read Every Code preview readiness.",
        )
        every_code_store = cast(
            _EveryCodePreviewGateReadStore,
            every_code_read_store_or_503(
                record_store,
                trace_id=trace_id,
                capability="preview_gate",
            ),
        )
        try:
            readiness = build_preview_readiness_read_model(
                generated_at=utc_now_timestamp(),
                record_store=every_code_store,
                repository=repository.strip(),
                pr_number=every_code_optional_int(
                    pr_number,
                    "pr_number",
                    trace_id=trace_id,
                ),
                status=status.strip(),
                limit=every_code_pagination_value(
                    limit,
                    "limit",
                    default=50,
                    trace_id=trace_id,
                ),
                offset=every_code_pagination_value(
                    offset,
                    "offset",
                    default=0,
                    trace_id=trace_id,
                ),
            )
        except ValueError as error:
            raise every_code_invalid_payload_error(trace_id=trace_id, error=error) from error
        return PreviewReadinessResponse(trace_id=trace_id, readiness=readiness)

    def list_every_code_work_requests(
        identity: Annotated[
            LaunchplaneIdentity | None, Depends(read_every_code_worker_read_identity)
        ],
        record_store: Annotated[object, Depends(get_record_store)],
        state: Annotated[str, Query()] = "",
        repository: Annotated[str, Query()] = "",
        limit: Annotated[str, Query()] = "50",
        offset: Annotated[str, Query()] = "0",
    ) -> EveryCodeWorkRequestRecordsResponse:
        trace_id = next_trace_id()
        ensure_every_code_read_allowed(
            identity=identity,
            trace_id=trace_id,
            action="every_code_work_request.read",
            message="Workflow cannot read Every Code work requests.",
        )
        every_code_store = cast(
            _EveryCodeWorkRequestListStore,
            every_code_read_store_or_503(
                record_store,
                trace_id=trace_id,
                capability="work_request_list",
            ),
        )
        records = every_code_store.list_every_code_work_request_records(
            state=state.strip(),
            repository=repository.strip(),
            limit=every_code_pagination_value(limit, "limit", default=50, trace_id=trace_id),
            offset=every_code_pagination_value(offset, "offset", default=0, trace_id=trace_id),
        )
        return EveryCodeWorkRequestRecordsResponse(
            trace_id=trace_id,
            state=state.strip(),
            repository=repository.strip(),
            requests=records,
        )

    def read_every_code_work_request(
        request_id: Annotated[str, Path(min_length=1, pattern=r"^\S+$")],
        identity: Annotated[
            LaunchplaneIdentity | None, Depends(read_every_code_worker_read_identity)
        ],
        record_store: Annotated[object, Depends(get_record_store)],
    ) -> EveryCodeWorkRequestRecordResponse:
        trace_id = next_trace_id()
        ensure_every_code_read_allowed(
            identity=identity,
            trace_id=trace_id,
            action="every_code_work_request.read",
            message="Workflow cannot read Every Code work requests.",
        )
        every_code_store = cast(
            _EveryCodeWorkRequestRecordStore,
            every_code_read_store_or_503(
                record_store,
                trace_id=trace_id,
                capability="work_request_record",
            ),
        )
        try:
            record = every_code_store.read_every_code_work_request_record(request_id)
        except FileNotFoundError as error:
            raise _launchplane_http_error(
                status_code=404,
                trace_id=trace_id,
                code="not_found",
                message=str(error),
            ) from error
        return EveryCodeWorkRequestRecordResponse(trace_id=trace_id, request=record)

    async def create_every_code_work_request(
        request: Request,
        every_code_request: EveryCodeWorkRequestCreateEnvelope,
        identity: Annotated[LaunchplaneIdentity, Depends(read_write_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> AcceptedEvidenceResponse:
        trace_id = next_trace_id()
        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action="every_code_work_request.write",
            product="launchplane",
            context=_LAUNCHPLANE_SERVICE_CONTEXT,
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Workflow cannot create Every Code work requests.",
            )
        (
            normalized_idempotency_key,
            payload_fingerprint,
            replayed_response,
        ) = await replay_apply_idempotency(
            request=request,
            record_store=record_store,
            identity=identity,
            route_path="/v1/every-code/work-requests/create",
            idempotency_key=idempotency_key,
            trace_id=trace_id,
            check_replay=True,
        )
        if replayed_response is not None:
            return replayed_response
        try:
            every_code_store = require_every_code_work_request_write_store(record_store)
        except TypeError as error:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message=str(error),
            ) from error
        try:
            record = build_every_code_work_request_record(
                every_code_request,
                queued_at=every_code_request.queued_at.strip() or utc_now_timestamp(),
            )
        except (ValueError, ValidationError) as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message=str(error) or "Request could not be completed.",
            ) from error
        every_code_store.write_every_code_work_request_record(record)
        response = accepted_evidence_response(
            trace_id=trace_id,
            records={"request_id": record.request_id, "state": record.state},
            result={"request": record.model_dump(mode="json")},
        )
        store_apply_idempotency(
            record_store=record_store,
            identity=identity,
            route_path="/v1/every-code/work-requests/create",
            idempotency_key=normalized_idempotency_key,
            request_fingerprint_value=payload_fingerprint,
            trace_id=trace_id,
            response=response,
        )
        return response

    def list_every_code_pr_feedback(
        identity: Annotated[
            LaunchplaneIdentity | None, Depends(read_every_code_worker_read_identity)
        ],
        record_store: Annotated[object, Depends(get_record_store)],
        request_id: Annotated[str, Query()] = "",
        repository: Annotated[str, Query()] = "",
        pr_number: Annotated[str, Query()] = "",
        status: Annotated[str, Query()] = "",
        limit: Annotated[str, Query()] = "50",
        offset: Annotated[str, Query()] = "0",
    ) -> EveryCodePrFeedbackRecordsResponse:
        trace_id = next_trace_id()
        ensure_every_code_read_allowed(
            identity=identity,
            trace_id=trace_id,
            action="every_code_pr_feedback.read",
            message="Workflow cannot read Every Code PR feedback.",
        )
        every_code_store = cast(
            _EveryCodePrFeedbackReadStore,
            every_code_read_store_or_503(
                record_store,
                trace_id=trace_id,
                capability="pr_feedback",
            ),
        )
        records = every_code_store.list_every_code_pr_feedback_records(
            request_id=request_id.strip(),
            repository=repository.strip(),
            pr_number=every_code_optional_int(pr_number, "pr_number", trace_id=trace_id),
            status=status.strip(),
            limit=every_code_pagination_value(limit, "limit", default=50, trace_id=trace_id),
            offset=every_code_pagination_value(offset, "offset", default=0, trace_id=trace_id),
        )
        return EveryCodePrFeedbackRecordsResponse(
            trace_id=trace_id,
            request_id=request_id.strip(),
            repository=repository.strip(),
            status_filter=status.strip(),
            feedback=records,
        )

    def write_every_code_pr_feedback(
        payload: dict[str, object],
        _worker_token: Annotated[None, Depends(require_every_code_worker_write_token)],
        record_store: Annotated[object, Depends(get_record_store)],
    ) -> AcceptedEvidenceResponse:
        trace_id = next_trace_id()
        try:
            every_code_store = require_every_code_pr_feedback_write_store(record_store)
        except TypeError as error:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message=str(error),
            ) from error
        try:
            feedback_record = EveryCodePrFeedbackRecord.model_validate(payload)
        except (ValueError, ValidationError) as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_payload",
                message=str(error),
            ) from error
        every_code_store.write_every_code_pr_feedback_record(feedback_record)
        return accepted_evidence_response(
            trace_id=trace_id,
            records={
                "request_id": feedback_record.request_id,
                "feedback_id": feedback_record.feedback_id,
                "status": feedback_record.status,
            },
            result={"feedback": feedback_record.model_dump(mode="json")},
        )

    def write_every_code_pr_feedback_status(
        payload: dict[str, object],
        _worker_token: Annotated[None, Depends(require_every_code_worker_write_token)],
        record_store: Annotated[object, Depends(get_record_store)],
    ) -> AcceptedEvidenceResponse:
        trace_id = next_trace_id()
        try:
            every_code_store = require_every_code_pr_feedback_status_store(record_store)
        except TypeError as error:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message=str(error),
            ) from error
        try:
            feedback_status_request = EveryCodePrFeedbackStatusEnvelope.model_validate(payload)
        except (ValueError, ValidationError) as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_payload",
                message=str(error),
            ) from error
        feedback_matches = every_code_store.list_every_code_pr_feedback_records(
            request_id=feedback_status_request.request_id.strip(),
            limit=100,
        )
        existing_feedback_record = next(
            (
                record
                for record in feedback_matches
                if record.feedback_id == feedback_status_request.feedback_id.strip()
            ),
            None,
        )
        if existing_feedback_record is None:
            raise _launchplane_http_error(
                status_code=404,
                trace_id=trace_id,
                code="not_found",
                message="Every Code PR feedback record was not found.",
            )
        updated_feedback_record = apply_every_code_pr_feedback_status(
            existing_feedback_record,
            status=feedback_status_request.status,
        )
        if updated_feedback_record is None:
            raise _launchplane_http_error(
                status_code=409,
                trace_id=trace_id,
                code="feedback_already_final",
                message="Every Code PR feedback is already applied or ignored.",
            )
        every_code_store.write_every_code_pr_feedback_record(updated_feedback_record)
        return accepted_evidence_response(
            trace_id=trace_id,
            records={
                "request_id": updated_feedback_record.request_id,
                "feedback_id": updated_feedback_record.feedback_id,
                "status": updated_feedback_record.status,
            },
            result={"feedback": updated_feedback_record.model_dump(mode="json")},
        )

    def list_every_code_preview_gates(
        identity: Annotated[
            LaunchplaneIdentity | None, Depends(read_every_code_worker_read_identity)
        ],
        record_store: Annotated[object, Depends(get_record_store)],
        request_id: Annotated[str, Query()] = "",
        repository: Annotated[str, Query()] = "",
        pr_number: Annotated[str, Query()] = "",
        status: Annotated[str, Query()] = "",
        limit: Annotated[str, Query()] = "50",
        offset: Annotated[str, Query()] = "0",
    ) -> EveryCodePreviewGateRecordsResponse:
        trace_id = next_trace_id()
        ensure_every_code_read_allowed(
            identity=identity,
            trace_id=trace_id,
            action="every_code_preview_gate.read",
            message="Workflow cannot read Every Code preview readiness.",
        )
        every_code_store = cast(
            _EveryCodePreviewGateReadStore,
            every_code_read_store_or_503(
                record_store,
                trace_id=trace_id,
                capability="preview_gate",
            ),
        )
        records = every_code_store.list_every_code_preview_gate_records(
            request_id=request_id.strip(),
            repository=repository.strip(),
            pr_number=every_code_optional_int(pr_number, "pr_number", trace_id=trace_id),
            status=status.strip(),
            limit=every_code_pagination_value(limit, "limit", default=50, trace_id=trace_id),
            offset=every_code_pagination_value(offset, "offset", default=0, trace_id=trace_id),
        )
        return EveryCodePreviewGateRecordsResponse(
            trace_id=trace_id,
            request_id=request_id.strip(),
            repository=repository.strip(),
            status_filter=status.strip(),
            gates=records,
        )

    def write_every_code_preview_gate(
        payload: dict[str, object],
        _worker_token: Annotated[None, Depends(require_every_code_worker_write_token)],
        record_store: Annotated[object, Depends(get_record_store)],
    ) -> AcceptedEvidenceResponse:
        trace_id = next_trace_id()
        try:
            every_code_store = require_every_code_preview_gate_write_store(record_store)
        except TypeError as error:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message=str(error),
            ) from error
        try:
            gate_record = EveryCodePreviewGateRecord.model_validate(payload)
        except (ValueError, ValidationError) as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_payload",
                message=str(error),
            ) from error
        every_code_store.write_every_code_preview_gate_record(gate_record)
        return accepted_evidence_response(
            trace_id=trace_id,
            records={
                "gate_id": gate_record.gate_id,
                "request_id": gate_record.request_id,
                "status": gate_record.status,
            },
            result={"gate": gate_record.model_dump(mode="json")},
        )

    def list_every_code_notification_attempts(
        identity: Annotated[
            LaunchplaneIdentity | None, Depends(read_every_code_worker_read_identity)
        ],
        record_store: Annotated[object, Depends(get_record_store)],
        request_id: Annotated[str, Query()] = "",
        event: Annotated[str, Query()] = "",
        destination_kind: Annotated[str, Query()] = "",
        limit: Annotated[str, Query()] = "50",
    ) -> EveryCodeNotificationAttemptRecordsResponse:
        trace_id = next_trace_id()
        ensure_every_code_read_allowed(
            identity=identity,
            trace_id=trace_id,
            action="every_code_notification_attempt.read",
            message="Workflow cannot read Every Code notification attempts.",
        )
        try:
            notification_store = require_every_code_notification_attempt_read_store(record_store)
        except TypeError as error:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message=str(error),
            ) from error
        records = notification_store.list_every_code_notification_attempt_records(
            request_id=request_id.strip(),
            event=event.strip(),
            destination_kind=destination_kind.strip(),
            limit=every_code_pagination_value(limit, "limit", default=50, trace_id=trace_id),
        )
        return EveryCodeNotificationAttemptRecordsResponse(
            trace_id=trace_id,
            request_id=request_id.strip(),
            event_filter=event.strip(),
            destination_kind_filter=destination_kind.strip(),
            attempts=records,
        )

    def list_preview_pr_feedback_notification_attempts(
        identity: Annotated[LaunchplaneIdentity, Depends(read_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
        feedback_id: Annotated[str, Query()] = "",
        event: Annotated[str, Query()] = "",
        destination_kind: Annotated[str, Query()] = "",
        limit: Annotated[str, Query()] = "50",
    ) -> PreviewPrFeedbackNotificationAttemptRecordsResponse:
        trace_id = next_trace_id()
        ensure_every_code_read_allowed(
            identity=identity,
            trace_id=trace_id,
            action="preview_pr_feedback_notification_attempt.read",
            message="Workflow cannot read preview PR feedback notification attempts.",
        )
        try:
            notification_store = require_preview_pr_feedback_notification_attempt_read_store(
                record_store
            )
        except TypeError as error:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message=str(error),
            ) from error
        records = notification_store.list_preview_pr_feedback_notification_attempt_records(
            feedback_id=feedback_id.strip(),
            event=event.strip(),
            destination_kind=destination_kind.strip(),
            limit=every_code_pagination_value(limit, "limit", default=50, trace_id=trace_id),
        )
        return PreviewPrFeedbackNotificationAttemptRecordsResponse(
            trace_id=trace_id,
            feedback_id=feedback_id.strip(),
            event_filter=event.strip(),
            destination_kind_filter=destination_kind.strip(),
            attempts=records,
        )

    def list_product_environment_overviews(
        identity: Annotated[LaunchplaneIdentity, Depends(read_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
    ) -> ProductEnvironmentListResponse:
        trace_id = next_trace_id()

        def action_allowed(
            requested_action: str, requested_product: str, requested_context: str
        ) -> bool:
            return product_environment_action_allowed(
                identity,
                requested_action,
                requested_product,
                requested_context,
            )

        if not product_environment_action_allowed(
            identity,
            "product_environment.read",
            "launchplane",
            _LAUNCHPLANE_SERVICE_CONTEXT,
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Workflow cannot list product overviews.",
            )
        try:
            product_read_store = (
                control_plane_product_read_service.require_product_environment_read_model_store(
                    record_store
                )
            )
            payload = (
                control_plane_product_read_service.build_product_environment_list_service_payload(
                    record_store=product_read_store,
                    action_allowed=action_allowed,
                )
            )
        except control_plane_product_read_service.ProductReadModelStoreCapabilityError as error:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message=str(error),
            ) from error
        products = tuple(
            ProductSiteOverview.model_validate(product)
            for product in cast(list[object], payload["products"])
        )
        return ProductEnvironmentListResponse(trace_id=trace_id, products=products)

    def read_product_overview(
        product: Annotated[str, Path(min_length=1, pattern=r"^\S+$")],
        identity: Annotated[LaunchplaneIdentity, Depends(read_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
    ) -> ProductOverviewResponse:
        trace_id = next_trace_id()
        result = build_product_environment_read_result(
            identity=identity,
            record_store=record_store,
            trace_id=trace_id,
            params={"product": product},
        )
        require_product_environment_result_authorization(
            identity=identity,
            trace_id=trace_id,
            result=result,
        )
        overview = ProductSiteOverview.model_validate(result.payload["product"])
        return ProductOverviewResponse(trace_id=trace_id, product=overview)

    def read_product_activity(
        product: Annotated[str, Path(min_length=1, pattern=r"^\S+$")],
        identity: Annotated[LaunchplaneIdentity, Depends(read_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
    ) -> ProductActivityResponse:
        trace_id = next_trace_id()
        result = build_product_environment_read_result(
            identity=identity,
            record_store=record_store,
            trace_id=trace_id,
            params={"product": product, "activity": "true"},
        )
        require_product_environment_result_authorization(
            identity=identity,
            trace_id=trace_id,
            result=result,
        )
        activity = ProductActivityReadModel.model_validate(result.payload["activity"])
        return ProductActivityResponse(trace_id=trace_id, activity=activity)

    def list_product_environments(
        product: Annotated[str, Path(min_length=1, pattern=r"^\S+$")],
        identity: Annotated[LaunchplaneIdentity, Depends(read_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
    ) -> ProductEnvironmentsResponse:
        trace_id = next_trace_id()
        result = build_product_environment_read_result(
            identity=identity,
            record_store=record_store,
            trace_id=trace_id,
            params={"product": product, "environments": "true"},
        )
        require_product_environment_result_authorization(
            identity=identity,
            trace_id=trace_id,
            result=result,
        )
        payload = result.payload
        environments = tuple(
            ProductEnvironmentSummary.model_validate(environment)
            for environment in cast(list[object], payload["environments"])
        )
        provenance = DataProvenance.model_validate(payload["provenance"])
        return ProductEnvironmentsResponse(
            trace_id=trace_id,
            product=str(payload["product"]),
            display_name=str(payload["display_name"]),
            repository=str(payload["repository"]),
            driver_id=str(payload["driver_id"]),
            base_driver_id=str(payload.get("base_driver_id", "")),
            environments=environments,
            trust_state=cast(FreshnessStatus, payload["trust_state"]),
            provenance=provenance,
        )

    def read_product_environment(
        product: Annotated[str, Path(min_length=1, pattern=r"^\S+$")],
        environment: Annotated[str, Path(min_length=1, pattern=r"^\S+$")],
        identity: Annotated[LaunchplaneIdentity, Depends(read_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
    ) -> ProductEnvironmentResponse:
        trace_id = next_trace_id()
        result = build_product_environment_read_result(
            identity=identity,
            record_store=record_store,
            trace_id=trace_id,
            params={"product": product, "environment": environment},
        )
        require_product_environment_result_authorization(
            identity=identity,
            trace_id=trace_id,
            result=result,
        )
        environment_detail = ProductEnvironmentDetail.model_validate(result.payload["environment"])
        return ProductEnvironmentResponse(
            trace_id=trace_id,
            environment=environment_detail,
        )

    def read_protected_artifacts(
        identity: Annotated[LaunchplaneIdentity, Depends(read_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
        product: Annotated[str, Query()] = "",
        context: Annotated[str, Query()] = "",
    ) -> ProtectedArtifactsResponse:
        trace_id = next_trace_id()
        requested_product = product.strip()
        requested_context = context.strip()
        if not requested_product:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_query",
                message="Protected artifact inventory requires a product query parameter.",
            )
        authz_context = requested_context or "*"
        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action="artifact_protection.read",
            product=requested_product,
            context=authz_context,
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Workflow cannot read protected artifact inventory.",
            )
        try:
            protected_artifact_store = require_protected_artifact_store(record_store)
            protected_artifacts = build_protected_artifact_set(
                protected_artifact_store,
                product=requested_product,
                context_name=requested_context,
            )
        except TypeError as error:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message=str(error),
            ) from error
        return ProtectedArtifactsResponse(
            trace_id=trace_id,
            protected_artifacts=protected_artifacts,
        )

    def ensure_driver_read_allowed(
        *,
        identity: LaunchplaneIdentity,
        trace_id: str,
        context: str = _LAUNCHPLANE_DRIVER_READ_CONTEXT,
    ) -> None:
        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action="driver.read",
            product=_LAUNCHPLANE_DRIVER_READ_PRODUCT,
            context=context,
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Workflow cannot read driver metadata for the requested context.",
            )

    def read_driver_descriptors(
        identity: Annotated[LaunchplaneIdentity, Depends(read_identity)],
    ) -> DriverDescriptorsResponse:
        trace_id = next_trace_id()
        ensure_driver_read_allowed(identity=identity, trace_id=trace_id)
        return DriverDescriptorsResponse(
            trace_id=trace_id,
            drivers=list_driver_descriptors(),
        )

    def read_driver_descriptor(
        driver_id: Annotated[str, Path(min_length=1, pattern=r"^\S+$")],
        identity: Annotated[LaunchplaneIdentity, Depends(read_identity)],
    ) -> DriverDescriptorResponse:
        trace_id = next_trace_id()
        ensure_driver_read_allowed(identity=identity, trace_id=trace_id)
        try:
            descriptor = read_driver_descriptor_record(driver_id)
        except FileNotFoundError as error:
            raise _launchplane_http_error(
                status_code=404,
                trace_id=trace_id,
                code="not_found",
                message=str(error),
            ) from error
        return DriverDescriptorResponse(trace_id=trace_id, driver=descriptor)

    def read_driver_context_view(
        context: Annotated[str, Path(min_length=1, pattern=r"^\S+$")],
        identity: Annotated[LaunchplaneIdentity, Depends(read_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
    ) -> DriverContextViewResponse:
        trace_id = next_trace_id()
        ensure_driver_read_allowed(identity=identity, trace_id=trace_id, context=context)
        view = build_driver_context_view(
            record_store=record_store,
            context_name=context,
        )
        return DriverContextViewResponse(trace_id=trace_id, view=view)

    def read_driver_instance_view(
        context: Annotated[str, Path(min_length=1, pattern=r"^\S+$")],
        instance: Annotated[str, Path(min_length=1, pattern=r"^\S+$")],
        identity: Annotated[LaunchplaneIdentity, Depends(read_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
    ) -> DriverContextViewResponse:
        trace_id = next_trace_id()
        ensure_driver_read_allowed(identity=identity, trace_id=trace_id, context=context)
        view = build_driver_context_view(
            record_store=record_store,
            context_name=context,
            instance_name=instance,
        )
        return DriverContextViewResponse(trace_id=trace_id, view=view)

    def read_dokploy_target_inspect(
        identity: Annotated[LaunchplaneIdentity, Depends(read_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
        context: Annotated[str, Query()] = "",
        instance: Annotated[str, Query()] = "",
        target_type: Annotated[str, Query()] = "",
        target_id: Annotated[str, Query()] = "",
    ) -> DokployTargetInspectResponse:
        trace_id = next_trace_id()
        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action="dokploy_target.inspect",
            product="launchplane",
            context=_LAUNCHPLANE_SERVICE_CONTEXT,
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Workflow cannot inspect Launchplane Dokploy targets.",
            )
        try:
            inspect_store = require_dokploy_target_inspect_store(record_store)
        except TypeError as error:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_required",
                message=str(error),
            ) from error
        try:
            inspect_request = DokployTargetInspectRequest.model_validate(
                {
                    "schema_version": 1,
                    "product": "launchplane",
                    "context": context,
                    "instance": instance,
                    "target_type": target_type,
                    "target_id": target_id,
                }
            )
            host, token = control_plane_dokploy.read_dokploy_config(
                control_plane_root=resolved_control_plane_root,
                database_url=database_url,
            )
            inspect_result = inspect_dokploy_target(
                record_store=inspect_store,
                host=host,
                token=token,
                request=inspect_request,
            )
        except ValueError as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_dokploy_target_inspect",
                message=str(error),
            ) from error
        except FileNotFoundError as error:
            raise _launchplane_http_error(
                status_code=404,
                trace_id=trace_id,
                code="not_found",
                message=str(error),
            ) from error
        return DokployTargetInspectResponse(trace_id=trace_id, inspect=inspect_result)

    def read_tracked_target_logs(
        context: Annotated[str, Path(min_length=1, pattern=r"^\S+$")],
        instance: Annotated[str, Path(min_length=1, pattern=r"^\S+$")],
        identity: Annotated[LaunchplaneIdentity, Depends(read_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
        lines: Annotated[str, Query()] = str(control_plane_dokploy.DEFAULT_DOKPLOY_LOG_LINE_COUNT),
        since: Annotated[str, Query()] = "all",
        search: Annotated[str, Query()] = "",
    ) -> TrackedTargetLogsResponse:
        trace_id = next_trace_id()
        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action="target_logs.read",
            product="launchplane",
            context=context,
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Workflow cannot read tracked target logs for the requested context.",
            )
        try:
            line_count = control_plane_service_status.query_int_value(
                lines,
                "lines",
                default=control_plane_dokploy.DEFAULT_DOKPLOY_LOG_LINE_COUNT,
                minimum=1,
                maximum=control_plane_dokploy.MAX_DOKPLOY_LOG_LINE_COUNT,
            )
            assert line_count is not None
        except ValueError as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_query",
                message=str(error),
            ) from error
        try:
            normalized_since = control_plane_dokploy.normalize_dokploy_log_since(since)
            normalized_search = control_plane_dokploy.normalize_dokploy_log_search(search)
        except click.ClickException as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_query",
                message=str(error),
            ) from error
        try:
            target_logs_store = require_tracked_target_logs_store(record_store)
            logs_payload = control_plane_tracked_target_logs.build_tracked_target_logs_payload(
                record_store=target_logs_store,
                control_plane_root=resolved_control_plane_root,
                context_name=context,
                instance_name=instance,
                line_count=line_count,
                since=normalized_since,
                search=normalized_search,
            )
        except TypeError as error:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_required",
                message=str(error),
            ) from error
        except ValueError as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message=str(error),
            ) from error
        except click.ClickException as error:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="target_logs_unavailable",
                message=str(error),
            ) from error
        return TrackedTargetLogsResponse.model_validate(
            {"status": "ok", "trace_id": trace_id, **logs_payload}
        )

    def ensure_edge_endpoint_read_allowed(
        *,
        identity: LaunchplaneIdentity,
        trace_id: str,
    ) -> None:
        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action="edge_endpoint.read",
            product="launchplane",
            context=_LAUNCHPLANE_SERVICE_CONTEXT,
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Workflow cannot read Launchplane edge endpoint records.",
            )

    def list_edge_endpoint_records(
        identity: Annotated[LaunchplaneIdentity, Depends(read_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
        limit: Annotated[str, Query()] = "25",
        provider: Annotated[str, Query()] = "",
        status: Annotated[str, Query()] = "",
    ) -> EdgeEndpointRecordsResponse:
        trace_id = next_trace_id()
        ensure_edge_endpoint_read_allowed(identity=identity, trace_id=trace_id)
        try:
            normalized_limit = control_plane_service_status.query_int_value(
                limit,
                "limit",
                default=25,
                minimum=1,
                maximum=100,
            )
            assert normalized_limit is not None
        except ValueError as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_query",
                message=str(error),
            ) from error
        try:
            edge_endpoint_store = require_edge_endpoint_read_store(record_store)
            records = edge_endpoint_store.list_edge_endpoint_records(
                provider=provider.strip(),
                status=status.strip(),
                limit=normalized_limit,
            )
        except TypeError as error:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message=str(error),
            ) from error
        return EdgeEndpointRecordsResponse(
            trace_id=trace_id,
            limit=normalized_limit,
            count=len(records),
            records=records,
        )

    def read_edge_endpoint_record(
        endpoint_key: Annotated[str, Path(min_length=1, pattern=r"^\S+$")],
        identity: Annotated[LaunchplaneIdentity, Depends(read_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
    ) -> EdgeEndpointRecordResponse:
        trace_id = next_trace_id()
        ensure_edge_endpoint_read_allowed(identity=identity, trace_id=trace_id)
        try:
            edge_endpoint_store = require_edge_endpoint_read_store(record_store)
            record = edge_endpoint_store.read_edge_endpoint_record(endpoint_key)
        except TypeError as error:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message=str(error),
            ) from error
        except FileNotFoundError as error:
            raise _launchplane_http_error(
                status_code=404,
                trace_id=trace_id,
                code="not_found",
                message=str(error),
            ) from error
        return EdgeEndpointRecordResponse(trace_id=trace_id, record=record)

    def ensure_private_health_endpoint_read_allowed(
        *,
        identity: LaunchplaneIdentity,
        trace_id: str,
        product: str,
        context_name: str,
    ) -> None:
        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action="private_health_endpoint.read",
            product=product,
            context=context_name,
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message=(
                    "Workflow cannot read private health endpoints for the requested "
                    "product/context."
                ),
            )

    def list_private_health_endpoint_records(
        identity: Annotated[LaunchplaneIdentity, Depends(read_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
        product: Annotated[str, Query()] = "",
        context: Annotated[str, Query()] = "",
        instance: Annotated[str, Query()] = "",
        status: Annotated[str, Query()] = "",
        limit: Annotated[str, Query()] = "25",
    ) -> PrivateHealthEndpointRecordsResponse:
        trace_id = next_trace_id()
        normalized_product = product.strip()
        context_name = context.strip()
        instance_name = instance.strip()
        if not normalized_product or not context_name:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_query",
                message=(
                    "Private health endpoint reads require product and context query parameters."
                ),
            )
        ensure_private_health_endpoint_read_allowed(
            identity=identity,
            trace_id=trace_id,
            product=normalized_product,
            context_name=context_name,
        )
        try:
            normalized_limit = control_plane_service_status.query_int_value(
                limit,
                "limit",
                default=25,
                minimum=1,
                maximum=100,
            )
            assert normalized_limit is not None
        except ValueError as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_query",
                message=str(error),
            ) from error
        try:
            private_endpoint_store = require_private_health_endpoint_read_store(record_store)
            records = private_endpoint_store.list_private_health_endpoint_records(
                product=normalized_product,
                context_name=context_name,
                instance_name=instance_name,
                status=status.strip(),
                limit=normalized_limit,
            )
        except TypeError as error:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message=str(error),
            ) from error
        return PrivateHealthEndpointRecordsResponse(
            trace_id=trace_id,
            product=normalized_product,
            context=context_name,
            instance=instance_name,
            limit=normalized_limit,
            count=len(records),
            records=records,
        )

    def read_private_health_endpoint_record(
        endpoint_key: Annotated[str, Path(min_length=1, pattern=r"^\S+$")],
        identity: Annotated[LaunchplaneIdentity, Depends(read_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
        product: Annotated[str, Query()] = "",
        context: Annotated[str, Query()] = "",
        instance: Annotated[str, Query()] = "",
    ) -> PrivateHealthEndpointRecordResponse:
        trace_id = next_trace_id()
        normalized_product = product.strip()
        context_name = context.strip()
        instance_name = instance.strip()
        if not normalized_product or not context_name:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_query",
                message=(
                    "Private health endpoint reads require product and context query parameters."
                ),
            )
        ensure_private_health_endpoint_read_allowed(
            identity=identity,
            trace_id=trace_id,
            product=normalized_product,
            context_name=context_name,
        )
        try:
            private_endpoint_store = require_private_health_endpoint_read_store(record_store)
            record = private_endpoint_store.read_private_health_endpoint_record(endpoint_key)
        except TypeError as error:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message=str(error),
            ) from error
        except FileNotFoundError as error:
            raise _launchplane_http_error(
                status_code=404,
                trace_id=trace_id,
                code="not_found",
                message=str(error),
            ) from error
        if (
            record.product != normalized_product
            or record.context != context_name
            or (instance_name and record.instance != instance_name)
        ):
            raise _launchplane_http_error(
                status_code=404,
                trace_id=trace_id,
                code="not_found",
                message=f"Record not found: {endpoint_key}",
            )
        return PrivateHealthEndpointRecordResponse(trace_id=trace_id, record=record)

    def ensure_ingress_canary_route_read_allowed(
        *,
        identity: LaunchplaneIdentity,
        trace_id: str,
    ) -> None:
        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action="ingress_canary_route.read",
            product="launchplane",
            context=_LAUNCHPLANE_SERVICE_CONTEXT,
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Workflow cannot read Launchplane ingress canary route records.",
            )

    def list_ingress_canary_route_records(
        identity: Annotated[LaunchplaneIdentity, Depends(read_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
        limit: Annotated[str, Query()] = "25",
        product: Annotated[str, Query()] = "",
        context: Annotated[str, Query()] = "",
        status: Annotated[str, Query()] = "",
    ) -> IngressCanaryRouteRecordsResponse:
        trace_id = next_trace_id()
        ensure_ingress_canary_route_read_allowed(identity=identity, trace_id=trace_id)
        try:
            normalized_limit = control_plane_service_status.query_int_value(
                limit,
                "limit",
                default=25,
                minimum=1,
                maximum=100,
            )
            assert normalized_limit is not None
        except ValueError as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_query",
                message=str(error),
            ) from error
        try:
            canary_store = require_ingress_canary_route_read_store(record_store)
            records = canary_store.list_ingress_canary_route_records(
                product=product.strip(),
                context_name=context.strip(),
                status=status.strip(),
                limit=normalized_limit,
            )
        except TypeError as error:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message=str(error),
            ) from error
        return IngressCanaryRouteRecordsResponse(
            trace_id=trace_id,
            limit=normalized_limit,
            count=len(records),
            records=records,
        )

    def read_ingress_canary_route_record(
        canary_key: Annotated[str, Path(min_length=1, pattern=r"^\S+$")],
        identity: Annotated[LaunchplaneIdentity, Depends(read_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
    ) -> IngressCanaryRouteRecordResponse:
        trace_id = next_trace_id()
        ensure_ingress_canary_route_read_allowed(identity=identity, trace_id=trace_id)
        try:
            canary_store = require_ingress_canary_route_read_store(record_store)
            record = canary_store.read_ingress_canary_route_record(canary_key)
        except TypeError as error:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message=str(error),
            ) from error
        except FileNotFoundError as error:
            raise _launchplane_http_error(
                status_code=404,
                trace_id=trace_id,
                code="not_found",
                message=str(error),
            ) from error
        return IngressCanaryRouteRecordResponse(trace_id=trace_id, record=record)

    def ensure_ingress_route_audit_read_allowed(
        *,
        identity: LaunchplaneIdentity,
        trace_id: str,
        product: str,
        context_name: str,
    ) -> None:
        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action="ingress_route.plan",
            product=product,
            context=context_name,
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Workflow cannot read ingress route audit records for the requested product/context.",
            )

    def list_ingress_route_audit_records(
        identity: Annotated[LaunchplaneIdentity, Depends(read_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
        product: Annotated[str, Query()] = "",
        context: Annotated[str, Query()] = "",
        status: Annotated[str, Query()] = "",
        mode: Annotated[str, Query()] = "",
        provider_host_id: Annotated[str, Query()] = "",
        trace_id: Annotated[str, Query()] = "",
        idempotency_key: Annotated[str, Query()] = "",
        limit: Annotated[str, Query()] = "25",
    ) -> IngressRouteAuditRecordsResponse:
        request_trace_id = next_trace_id()
        normalized_product = product.strip()
        context_name = context.strip()
        if not normalized_product or not context_name:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=request_trace_id,
                code="invalid_query",
                message="Ingress route audit list requires product and context query parameters.",
            )
        ensure_ingress_route_audit_read_allowed(
            identity=identity,
            trace_id=request_trace_id,
            product=normalized_product,
            context_name=context_name,
        )
        try:
            normalized_limit = control_plane_service_status.query_int_value(
                limit,
                "limit",
                default=25,
                minimum=1,
                maximum=100,
            )
            assert normalized_limit is not None
            normalized_provider_host_id = control_plane_service_status.query_int_value(
                provider_host_id,
                "provider_host_id",
                minimum=1,
            )
        except ValueError as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=request_trace_id,
                code="invalid_query",
                message=str(error),
            ) from error
        try:
            audit_store = require_ingress_route_audit_record_read_store(record_store)
            records = audit_store.list_ingress_route_audit_records(
                product=normalized_product,
                context_name=context_name,
                limit=None,
            )
        except TypeError as error:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=request_trace_id,
                code="database_storage_required",
                message=str(error),
            ) from error
        records = filter_ingress_route_audit_records(
            records,
            status=status.strip(),
            mode=mode.strip(),
            provider_host_id=normalized_provider_host_id,
            trace_id=trace_id.strip(),
            idempotency_key=idempotency_key.strip(),
        )
        limited_records = records[:normalized_limit]
        return IngressRouteAuditRecordsResponse(
            trace_id=request_trace_id,
            product=normalized_product,
            context=context_name,
            limit=normalized_limit,
            count=len(limited_records),
            records=limited_records,
        )

    def read_ingress_route_audit_record(
        record_id: Annotated[str, Path(min_length=1, pattern=r"^\S+$")],
        identity: Annotated[LaunchplaneIdentity, Depends(read_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
        product: Annotated[str, Query()] = "",
        context: Annotated[str, Query()] = "",
    ) -> IngressRouteAuditRecordResponse:
        request_trace_id = next_trace_id()
        normalized_product = product.strip()
        context_name = context.strip()
        if not normalized_product or not context_name:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=request_trace_id,
                code="invalid_query",
                message="Ingress route audit record reads require product and context query parameters.",
            )
        ensure_ingress_route_audit_read_allowed(
            identity=identity,
            trace_id=request_trace_id,
            product=normalized_product,
            context_name=context_name,
        )
        try:
            audit_store = require_ingress_route_audit_record_read_store(record_store)
            record = audit_store.read_ingress_route_audit_record(record_id)
        except TypeError as error:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=request_trace_id,
                code="database_storage_required",
                message=str(error),
            ) from error
        except FileNotFoundError as error:
            raise _launchplane_http_error(
                status_code=404,
                trace_id=request_trace_id,
                code="not_found",
                message=str(error),
            ) from error
        if record.product != normalized_product or record.context != context_name:
            raise _launchplane_http_error(
                status_code=404,
                trace_id=request_trace_id,
                code="not_found",
                message=f"Record not found: {record_id}",
            )
        return IngressRouteAuditRecordResponse(trace_id=request_trace_id, record=record)

    def read_deployment_record(
        record_id: Annotated[str, Path(min_length=1, pattern=r"^\S+$")],
        identity: Annotated[LaunchplaneIdentity, Depends(read_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
    ) -> DeploymentRecordResponse:
        trace_id = next_trace_id()
        try:
            deployment_store = require_deployment_read_store(record_store)
            deployment = deployment_store.read_deployment_record(record_id)
        except TypeError as error:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message=str(error),
            ) from error
        except FileNotFoundError as error:
            raise _launchplane_http_error(
                status_code=404,
                trace_id=trace_id,
                code="not_found",
                message=str(error),
            ) from error
        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action="deployment.read",
            product="launchplane",
            context=deployment.context,
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Workflow cannot read deployment records for the requested context.",
            )
        return DeploymentRecordResponse(trace_id=trace_id, record=deployment)

    def read_promotion_record(
        record_id: Annotated[str, Path(min_length=1, pattern=r"^\S+$")],
        identity: Annotated[LaunchplaneIdentity, Depends(read_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
    ) -> PromotionRecordResponse:
        trace_id = next_trace_id()
        try:
            promotion_store = require_promotion_read_store(record_store)
            promotion = promotion_store.read_promotion_record(record_id)
        except TypeError as error:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message=str(error),
            ) from error
        except FileNotFoundError as error:
            raise _launchplane_http_error(
                status_code=404,
                trace_id=trace_id,
                code="not_found",
                message=str(error),
            ) from error
        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action="promotion.read",
            product="launchplane",
            context=promotion.context,
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Workflow cannot read promotion records for the requested context.",
            )
        return PromotionRecordResponse(trace_id=trace_id, record=promotion)

    def read_preview_record(
        preview_id: Annotated[str, Path(min_length=1, pattern=r"^\S+$")],
        identity: Annotated[LaunchplaneIdentity, Depends(read_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
    ) -> PreviewRecordResponse:
        trace_id = next_trace_id()
        try:
            preview_store = require_preview_read_store(record_store)
            preview = preview_store.read_preview_record(preview_id)
        except TypeError as error:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message=str(error),
            ) from error
        except FileNotFoundError as error:
            raise _launchplane_http_error(
                status_code=404,
                trace_id=trace_id,
                code="not_found",
                message=str(error),
            ) from error
        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action="preview.read",
            product="launchplane",
            context=preview.context,
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Workflow cannot read previews for the requested context.",
            )
        return PreviewRecordResponse(trace_id=trace_id, record=preview)

    def read_preview_history(
        preview_id: Annotated[str, Path(min_length=1, pattern=r"^\S+$")],
        identity: Annotated[LaunchplaneIdentity, Depends(read_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
    ) -> PreviewHistoryResponse:
        trace_id = next_trace_id()
        try:
            preview_store = require_preview_history_read_store(record_store)
            preview = preview_store.read_preview_record(preview_id)
        except TypeError as error:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message=str(error),
            ) from error
        except FileNotFoundError as error:
            raise _launchplane_http_error(
                status_code=404,
                trace_id=trace_id,
                code="not_found",
                message=str(error),
            ) from error
        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action="preview.read",
            product="launchplane",
            context=preview.context,
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Workflow cannot read previews for the requested context.",
            )
        try:
            generations = preview_store.list_preview_generation_records(
                preview_id=preview.preview_id
            )
        except TypeError as error:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message=str(error),
            ) from error
        except FileNotFoundError as error:
            raise _launchplane_http_error(
                status_code=404,
                trace_id=trace_id,
                code="not_found",
                message=str(error),
            ) from error
        return PreviewHistoryResponse(
            trace_id=trace_id,
            preview=preview,
            generations=generations,
        )

    def read_environment_inventory(
        context: Annotated[str, Path(min_length=1, pattern=r"^\S+$")],
        instance: Annotated[str, Path(min_length=1, pattern=r"^\S+$")],
        identity: Annotated[LaunchplaneIdentity, Depends(read_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
    ) -> EnvironmentInventoryResponse:
        trace_id = next_trace_id()
        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action="inventory.read",
            product="launchplane",
            context=context,
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Workflow cannot read inventory for the requested context.",
            )
        try:
            inventory_store = require_environment_inventory_read_store(record_store)
            inventory = inventory_store.read_environment_inventory(
                context_name=context,
                instance_name=instance,
            )
        except TypeError as error:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message=str(error),
            ) from error
        except FileNotFoundError as error:
            raise _launchplane_http_error(
                status_code=404,
                trace_id=trace_id,
                code="not_found",
                message=str(error),
            ) from error
        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action="inventory.read",
            product="launchplane",
            context=inventory.context,
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Workflow cannot read inventory for the requested context.",
            )
        return EnvironmentInventoryResponse(trace_id=trace_id, record=inventory)

    def read_recent_operations(
        context: Annotated[str, Path(min_length=1, pattern=r"^\S+$")],
        identity: Annotated[LaunchplaneIdentity, Depends(read_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
    ) -> RecentOperationsResponse:
        trace_id = next_trace_id()
        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action="operations.read",
            product="launchplane",
            context=context,
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Workflow cannot read recent operations for the requested context.",
            )
        try:
            operations_store = require_recent_operations_read_store(record_store)
            inventory = tuple(
                record
                for record in operations_store.list_environment_inventory()
                if record.context == context
            )
            recent_deployments = operations_store.list_deployment_records(
                context_name=context,
                limit=10,
            )
            recent_promotions = operations_store.list_promotion_records(
                context_name=context,
                limit=10,
            )
            recent_previews = operations_store.list_preview_records(
                context_name=context,
                limit=10,
            )
        except TypeError as error:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message=str(error),
            ) from error
        return RecentOperationsResponse(
            trace_id=trace_id,
            context=context,
            storage_backend=storage_backend_name(record_store),
            inventory=inventory,
            recent_deployments=recent_deployments,
            recent_promotions=recent_promotions,
            recent_previews=recent_previews,
        )

    def list_product_profiles(
        identity: Annotated[
            LaunchplaneIdentity | None, Depends(read_product_profile_list_identity)
        ],
        record_store: Annotated[object, Depends(get_record_store)],
        driver_id: Annotated[str, Query()] = "",
    ) -> ProductProfileListResponse:
        trace_id = next_trace_id()
        if identity is not None and not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action="product_profile.read",
            product="launchplane",
            context=_LAUNCHPLANE_SERVICE_CONTEXT,
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Workflow cannot list Launchplane product profiles.",
            )
        normalized_driver_id = driver_id.strip()
        try:
            profile_store = require_product_profile_list_store(record_store)
            product_profile_payload = (
                control_plane_product_read_service.build_product_profile_list_service_payload(
                    record_store=profile_store,
                    driver_id=normalized_driver_id,
                )
            )
        except TypeError as error:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message=str(error),
            ) from error
        payload_profiles = cast(list[object], product_profile_payload["profiles"])
        profiles = tuple(
            LaunchplaneProductProfileRecord.model_validate(profile) for profile in payload_profiles
        )
        return ProductProfileListResponse(
            trace_id=trace_id,
            driver_id=str(product_profile_payload["driver_id"]),
            profiles=profiles,
        )

    def read_product_profile(
        product: Annotated[str, Path(min_length=1, pattern=r"^\S+$")],
        identity: Annotated[LaunchplaneIdentity, Depends(read_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
    ) -> ProductProfileResponse:
        trace_id = next_trace_id()
        try:
            profile_store = require_product_profile_read_store(record_store)
            profile = profile_store.read_product_profile_record(product)
        except TypeError as error:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message=str(error),
            ) from error
        except FileNotFoundError as error:
            raise _launchplane_http_error(
                status_code=404,
                trace_id=trace_id,
                code="not_found",
                message=str(error),
            ) from error
        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action="product_profile.read",
            product=profile.product,
            context=_LAUNCHPLANE_SERVICE_CONTEXT,
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Workflow cannot read the requested product profile.",
            )
        return ProductProfileResponse(trace_id=trace_id, profile=profile)

    async def write_product_profile(
        request: Request,
        identity: Annotated[LaunchplaneIdentity, Depends(read_write_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> AcceptedEvidenceResponse:
        trace_id = next_trace_id()
        try:
            raw_payload = await request.json()
        except ValueError as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request could not be completed.",
            ) from error
        if not isinstance(raw_payload, dict):
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request could not be completed.",
            )
        try:
            profile = LaunchplaneProductProfileRecord.model_validate(raw_payload)
        except ValidationError as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request payload failed validation.",
            ) from error
        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action="product_profile.write",
            product=profile.product,
            context=_LAUNCHPLANE_SERVICE_CONTEXT,
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Workflow cannot write the requested product profile.",
            )
        try:
            profile.validate_write_contract()
        except ValueError as error:
            message = str(error).strip() or "Product profile request failed validation."
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message=message,
            ) from error
        (
            normalized_idempotency_key,
            payload_fingerprint,
            replay_response,
        ) = await replay_apply_idempotency(
            request=request,
            record_store=record_store,
            identity=identity,
            route_path=_PRODUCT_PROFILES_ROUTE,
            idempotency_key=idempotency_key,
            trace_id=trace_id,
            check_replay=bool(idempotency_key.strip()),
        )
        if replay_response is not None:
            return replay_response
        try:
            profile_store = require_product_profile_write_store(record_store)
        except TypeError as error:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message=str(error),
            ) from error
        profile_store.write_product_profile_record(profile)
        response = accepted_evidence_response(
            trace_id=trace_id,
            records={"product_profile": profile.product},
        )
        store_apply_idempotency(
            record_store=record_store,
            identity=identity,
            route_path=_PRODUCT_PROFILES_ROUTE,
            idempotency_key=normalized_idempotency_key,
            request_fingerprint_value=payload_fingerprint,
            trace_id=trace_id,
            response=response,
        )
        return response

    def read_product_context_cutover_audit(
        product: Annotated[str, Path(min_length=1, pattern=r"^\S+$")],
        identity: Annotated[LaunchplaneIdentity, Depends(read_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
        source_context: Annotated[str, Query()] = "",
        target_context: Annotated[str, Query()] = "",
        preview_context: Annotated[str, Query()] = "",
    ) -> ProductContextCutoverAuditResponse:
        trace_id = next_trace_id()
        try:
            audit_store = require_product_context_audit_store(record_store)
            profile = audit_store.read_product_profile_record(product)
        except TypeError as error:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message=str(error),
            ) from error
        except FileNotFoundError as error:
            raise _launchplane_http_error(
                status_code=404,
                trace_id=trace_id,
                code="not_found",
                message=str(error),
            ) from error
        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action="product_profile.read",
            product=profile.product,
            context=_LAUNCHPLANE_SERVICE_CONTEXT,
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Workflow cannot read the requested product profile.",
            )
        normalized_source_context = source_context.strip()
        normalized_target_context = target_context.strip()
        normalized_preview_context = preview_context.strip()
        if not product_profile_context_cutover_contexts_allowed(
            profile=profile,
            source_context=normalized_source_context,
            target_context=normalized_target_context,
            preview_context=normalized_preview_context,
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="context_not_in_product_boundary",
                message="Requested audit contexts are not owned by the product profile.",
            )
        try:
            audit_payload = control_plane_product_context_audit.build_product_context_cutover_audit(
                record_store=audit_store,
                product=profile.product,
                source_context=normalized_source_context,
                target_context=normalized_target_context,
                preview_context=normalized_preview_context,
            )
        except ValueError as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_context_cutover_audit_request",
                message="Context cutover audit request is invalid.",
            ) from error
        return ProductContextCutoverAuditResponse(trace_id=trace_id, audit=audit_payload)

    def read_secret_status(
        secret_id: Annotated[str, Path(min_length=1, pattern=r"^\S+$")],
        identity: Annotated[LaunchplaneIdentity, Depends(read_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
    ) -> SecretStatusResponse:
        trace_id = next_trace_id()
        try:
            secret_store = require_secret_status_read_store(record_store)
            secret_status = SecretStatusReadModel.model_validate(
                control_plane_secrets.build_secret_status(
                    secret_store,
                    secret_id=secret_id,
                )
            )
        except TypeError as error:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message=str(error),
            ) from error
        except FileNotFoundError as error:
            raise _launchplane_http_error(
                status_code=404,
                trace_id=trace_id,
                code="not_found",
                message=str(error),
            ) from error
        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action="secret.read",
            product="launchplane",
            context=secret_status.context,
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Workflow cannot read Launchplane managed secret status for the requested context.",
            )
        return SecretStatusResponse(trace_id=trace_id, secret=secret_status)

    def list_context_secret_statuses(
        context: Annotated[str, Path(min_length=1, pattern=r"^\S+$")],
        identity: Annotated[LaunchplaneIdentity, Depends(read_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
    ) -> SecretStatusListResponse:
        return list_secret_statuses_for_context(
            context=context,
            instance="",
            identity=identity,
            record_store=record_store,
        )

    def list_instance_secret_statuses(
        context: Annotated[str, Path(min_length=1, pattern=r"^\S+$")],
        instance: Annotated[str, Path(min_length=1, pattern=r"^\S+$")],
        identity: Annotated[LaunchplaneIdentity, Depends(read_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
    ) -> SecretStatusListResponse:
        return list_secret_statuses_for_context(
            context=context,
            instance=instance,
            identity=identity,
            record_store=record_store,
        )

    def list_secret_statuses_for_context(
        *,
        context: str,
        instance: str,
        identity: LaunchplaneIdentity,
        record_store: object,
    ) -> SecretStatusListResponse:
        trace_id = next_trace_id()
        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action="secret.list",
            product="launchplane",
            context=context,
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Workflow cannot list Launchplane managed secret status for the requested context.",
            )
        try:
            secret_store = require_secret_status_read_store(record_store)
            statuses = tuple(
                SecretStatusReadModel.model_validate(status)
                for status in control_plane_secrets.list_secret_statuses(
                    secret_store,
                    context_name=context,
                    instance_name=instance,
                )
            )
        except TypeError as error:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message=str(error),
            ) from error
        except FileNotFoundError as error:
            raise _launchplane_http_error(
                status_code=404,
                trace_id=trace_id,
                code="not_found",
                message=str(error),
            ) from error
        return SecretStatusListResponse(
            trace_id=trace_id,
            context=context,
            instance=instance,
            secrets=statuses,
        )

    def accepted_evidence_response(
        *,
        trace_id: str,
        records: dict[str, str],
        result: dict[str, object] | None = None,
        replayed: bool = False,
        original_trace_id: str = "",
    ) -> AcceptedEvidenceResponse:
        return AcceptedEvidenceResponse(
            trace_id=trace_id,
            records=records,
            result=result,
            replayed=True if replayed else None,
            original_trace_id=original_trace_id or None,
        )

    def replay_idempotent_response(
        *, trace_id: str, stored_record: LaunchplaneIdempotencyRecord
    ) -> AcceptedEvidenceResponse:
        stored_records = {
            str(key): str(value)
            for key, value in dict(stored_record.response_payload.get("records") or {}).items()
        }
        stored_result = stored_record.response_payload.get("result")
        return accepted_evidence_response(
            trace_id=trace_id,
            records=stored_records,
            result=stored_result if isinstance(stored_result, dict) else None,
            replayed=True,
            original_trace_id=stored_record.response_trace_id,
        )

    def require_notification_policy_database_store(
        *, record_store: object, trace_id: str, label: str
    ) -> PostgresRecordStore:
        if not isinstance(record_store, PostgresRecordStore):
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_required",
                message=f"{label} notification policy apply requires DB-backed Launchplane storage.",
            )
        return record_store

    def require_runtime_key_safety_policy_database_store(
        *, record_store: object, trace_id: str
    ) -> PostgresRecordStore:
        if not isinstance(record_store, PostgresRecordStore):
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_required",
                message="Runtime key-safety policy writes require Launchplane database storage.",
            )
        return record_store

    def require_dokploy_target_setup_database_store(
        *, record_store: object, trace_id: str
    ) -> PostgresRecordStore:
        if not isinstance(record_store, PostgresRecordStore):
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_required",
                message="Dokploy target setup requires Launchplane database storage.",
            )
        return record_store

    def require_product_context_apply_database_store(
        *, record_store: object, trace_id: str, label: str
    ) -> PostgresRecordStore:
        if not isinstance(record_store, PostgresRecordStore):
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_required",
                message=f"{label} requires Launchplane database storage.",
            )
        return record_store

    def require_local_operator_notification_policy_reason(
        *, identity: LaunchplaneIdentity, reason: str, trace_id: str, message: str
    ) -> None:
        if isinstance(identity, LocalOperatorIdentity | LocalAdminIdentity) and not reason:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="reason_required",
                message=message,
            )

    async def replay_notification_policy_idempotency(
        *,
        request: Request,
        record_store: PostgresRecordStore,
        identity: LaunchplaneIdentity,
        route_path: str,
        idempotency_key: str,
        trace_id: str,
    ) -> tuple[str, str, AcceptedEvidenceResponse | None]:
        normalized_idempotency_key = idempotency_key.strip()
        normalized_scope = idempotency_scope(identity)
        raw_payload = await request.json()
        payload_fingerprint = request_fingerprint(cast(dict[str, object], raw_payload))
        idempotency_store = idempotency_capable_store(record_store)
        if idempotency_store is not None and normalized_idempotency_key:
            stored_record = idempotency_store.read_idempotency_record(
                scope=normalized_scope,
                route_path=route_path,
                idempotency_key=normalized_idempotency_key,
            )
            if stored_record is not None:
                if stored_record.request_fingerprint != payload_fingerprint:
                    raise _launchplane_http_error(
                        status_code=409,
                        trace_id=trace_id,
                        code="idempotency_key_reused",
                        message=(
                            "Idempotency-Key was already used for a different "
                            "Launchplane request payload on this route."
                        ),
                    )
                return (
                    normalized_idempotency_key,
                    payload_fingerprint,
                    replay_idempotent_response(
                        trace_id=trace_id,
                        stored_record=stored_record,
                    ),
                )
        return normalized_idempotency_key, payload_fingerprint, None

    def store_notification_policy_idempotency(
        *,
        record_store: PostgresRecordStore,
        identity: LaunchplaneIdentity,
        route_path: str,
        idempotency_key: str,
        request_fingerprint_value: str,
        trace_id: str,
        response: AcceptedEvidenceResponse,
    ) -> None:
        idempotency_store = idempotency_capable_store(record_store)
        if idempotency_store is None or not idempotency_key:
            return
        idempotency_store.write_idempotency_record(
            LaunchplaneIdempotencyRecord(
                record_id=build_launchplane_idempotency_record_id(response_trace_id=trace_id),
                scope=idempotency_scope(identity),
                route_path=route_path,
                idempotency_key=idempotency_key,
                request_fingerprint=request_fingerprint_value,
                response_status_code=202,
                response_trace_id=trace_id,
                recorded_at=utc_now_timestamp(),
                response_payload=response.model_dump(mode="json", exclude_none=True),
            )
        )

    async def replay_apply_idempotency(
        *,
        request: Request,
        record_store: object,
        identity: LaunchplaneIdentity,
        route_path: str,
        idempotency_key: str,
        trace_id: str,
        check_replay: bool,
    ) -> tuple[str, str, AcceptedEvidenceResponse | None]:
        normalized_idempotency_key = idempotency_key.strip()
        normalized_scope = idempotency_scope(identity)
        raw_payload = await request.json()
        payload_fingerprint = request_fingerprint(cast(dict[str, object], raw_payload))
        idempotency_store = idempotency_capable_store(record_store)
        if idempotency_store is not None and normalized_idempotency_key and check_replay:
            stored_record = idempotency_store.read_idempotency_record(
                scope=normalized_scope,
                route_path=route_path,
                idempotency_key=normalized_idempotency_key,
            )
            if stored_record is not None:
                if stored_record.request_fingerprint != payload_fingerprint:
                    raise _launchplane_http_error(
                        status_code=409,
                        trace_id=trace_id,
                        code="idempotency_key_reused",
                        message=(
                            "Idempotency-Key was already used for a different "
                            "Launchplane request payload on this route."
                        ),
                    )
                return (
                    normalized_idempotency_key,
                    payload_fingerprint,
                    replay_idempotent_response(
                        trace_id=trace_id,
                        stored_record=stored_record,
                    ),
                )
        return normalized_idempotency_key, payload_fingerprint, None

    def store_apply_idempotency(
        *,
        record_store: object,
        identity: LaunchplaneIdentity,
        route_path: str,
        idempotency_key: str,
        request_fingerprint_value: str,
        trace_id: str,
        response: AcceptedEvidenceResponse,
    ) -> None:
        idempotency_store = idempotency_capable_store(record_store)
        if idempotency_store is None or not idempotency_key:
            return
        idempotency_store.write_idempotency_record(
            LaunchplaneIdempotencyRecord(
                record_id=build_launchplane_idempotency_record_id(response_trace_id=trace_id),
                scope=idempotency_scope(identity),
                route_path=route_path,
                idempotency_key=idempotency_key,
                request_fingerprint=request_fingerprint_value,
                response_status_code=202,
                response_trace_id=trace_id,
                recorded_at=utc_now_timestamp(),
                response_payload=response.model_dump(mode="json", exclude_none=True),
            )
        )

    async def apply_product_context_cutover(
        request: Request,
        identity: Annotated[LaunchplaneIdentity, Depends(read_write_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> AcceptedEvidenceResponse:
        trace_id = next_trace_id()
        try:
            raw_payload = await request.json()
        except ValueError as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request could not be completed.",
            ) from error
        if not isinstance(raw_payload, dict):
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request could not be completed.",
            )
        try:
            context_cutover_request = (
                control_plane_product_context_cutover.ProductContextCutoverRequest.model_validate(
                    raw_payload
                )
            )
        except ValidationError as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request payload failed validation.",
            ) from error
        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action="product_profile.write",
            product=context_cutover_request.product,
            context=_LAUNCHPLANE_SERVICE_CONTEXT,
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Workflow cannot cut over the requested product profile context.",
            )
        database_store = require_product_context_apply_database_store(
            record_store=record_store,
            trace_id=trace_id,
            label="Product context cutover",
        )
        (
            normalized_idempotency_key,
            payload_fingerprint,
            replay_response,
        ) = await replay_apply_idempotency(
            request=request,
            record_store=database_store,
            identity=identity,
            route_path=_PRODUCT_CONTEXT_CUTOVER_APPLY_ROUTE,
            idempotency_key=idempotency_key,
            trace_id=trace_id,
            check_replay=bool(idempotency_key.strip()),
        )
        if replay_response is not None:
            return replay_response
        try:
            profile = database_store.read_product_profile_record(context_cutover_request.product)
        except FileNotFoundError as error:
            raise _launchplane_http_error(
                status_code=404,
                trace_id=trace_id,
                code="not_found",
                message=str(error),
            ) from error
        if not product_profile_context_cutover_contexts_allowed(
            profile=profile,
            source_context=context_cutover_request.source_context,
            target_context=context_cutover_request.target_context,
            preview_context="",
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="context_not_in_product_boundary",
                message="Requested cutover contexts are not owned by the product profile.",
            )
        try:
            result = control_plane_product_context_cutover.apply_product_context_cutover(
                record_store=database_store,
                request=context_cutover_request,
            )
        except ValueError as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_context_cutover_request",
                message="Product context cutover context_cutover_request is invalid.",
            ) from error
        response = accepted_evidence_response(
            trace_id=trace_id,
            records={"product_profile": context_cutover_request.product},
            result=result,
        )
        store_apply_idempotency(
            record_store=database_store,
            identity=identity,
            route_path=_PRODUCT_CONTEXT_CUTOVER_APPLY_ROUTE,
            idempotency_key=normalized_idempotency_key,
            request_fingerprint_value=payload_fingerprint,
            trace_id=trace_id,
            response=response,
        )
        return response

    async def apply_product_legacy_context_cleanup(
        request: Request,
        identity: Annotated[LaunchplaneIdentity, Depends(read_write_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> AcceptedEvidenceResponse:
        trace_id = next_trace_id()
        try:
            raw_payload = await request.json()
        except ValueError as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request could not be completed.",
            ) from error
        if not isinstance(raw_payload, dict):
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request could not be completed.",
            )
        try:
            legacy_cleanup_request = (
                control_plane_product_context_cutover.LegacyContextCleanupRequest.model_validate(
                    raw_payload
                )
            )
        except ValidationError as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request payload failed validation.",
            ) from error
        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action="product_profile.write",
            product=legacy_cleanup_request.product,
            context=_LAUNCHPLANE_SERVICE_CONTEXT,
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Workflow cannot clean up the requested legacy product context.",
            )
        database_store = require_product_context_apply_database_store(
            record_store=record_store,
            trace_id=trace_id,
            label="Legacy context cleanup",
        )
        (
            normalized_idempotency_key,
            payload_fingerprint,
            replay_response,
        ) = await replay_apply_idempotency(
            request=request,
            record_store=database_store,
            identity=identity,
            route_path=_PRODUCT_LEGACY_CONTEXT_CLEANUP_APPLY_ROUTE,
            idempotency_key=idempotency_key,
            trace_id=trace_id,
            check_replay=bool(idempotency_key.strip()),
        )
        if replay_response is not None:
            return replay_response
        try:
            result = control_plane_product_context_cutover.apply_legacy_context_cleanup(
                record_store=database_store,
                request=legacy_cleanup_request,
            )
        except control_plane_product_context_cutover.LegacyContextCleanupBoundaryError as error:
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="context_not_in_product_boundary",
                message="Requested cleanup contexts are not in the product cleanup boundary.",
            ) from error
        except FileNotFoundError as error:
            raise _launchplane_http_error(
                status_code=404,
                trace_id=trace_id,
                code="not_found",
                message=str(error),
            ) from error
        except ValueError as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_legacy_context_cleanup_request",
                message="Legacy context cleanup legacy_cleanup_request is invalid.",
            ) from error
        response = accepted_evidence_response(
            trace_id=trace_id,
            records={"product_profile": legacy_cleanup_request.product},
            result=result,
        )
        store_apply_idempotency(
            record_store=database_store,
            identity=identity,
            route_path=_PRODUCT_LEGACY_CONTEXT_CLEANUP_APPLY_ROUTE,
            idempotency_key=normalized_idempotency_key,
            request_fingerprint_value=payload_fingerprint,
            trace_id=trace_id,
            response=response,
        )
        return response

    def resolve_ingress_edge_endpoint(
        *,
        edge_endpoint_store: _IngressEdgeEndpointReadStore,
        request: NpmplusIngressApplyRequest,
        trace_id: str,
    ) -> NpmplusIngressApplyRequest:
        endpoint_key = request.route.edge_endpoint_key.strip()
        if not endpoint_key:
            return request
        try:
            endpoint = edge_endpoint_store.read_edge_endpoint_record(endpoint_key)
        except FileNotFoundError as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_edge_endpoint",
                message=f"Ingress edge endpoint {endpoint_key!r} was not found.",
            ) from error
        if endpoint.status != "active":
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_edge_endpoint",
                message=f"Ingress edge endpoint {endpoint_key!r} is {endpoint.status}, not active.",
            )
        resolved_route = request.route.model_copy(
            update={
                "forward_scheme": endpoint.upstream_scheme,
                "forward_host": endpoint.upstream_host,
                "forward_port": endpoint.upstream_port,
            }
        )
        return request.model_copy(update={"route": resolved_route})

    def active_ingress_canary_route_record(
        *,
        canary_store: _IngressCanaryRouteApplyStore,
        canary_key: str,
        product: str,
        context: str,
        trace_id: str,
    ) -> IngressCanaryRouteRecord:
        try:
            record = canary_store.read_ingress_canary_route_record(canary_key)
        except FileNotFoundError as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_ingress_canary_route",
                message=f"Ingress canary route {canary_key!r} was not found.",
            ) from error
        if record.product != product or record.context != context:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_ingress_canary_route",
                message=f"Ingress canary route {canary_key!r} is not scoped to {product}/{context}.",
            )
        if record.status != "active":
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_ingress_canary_route",
                message=f"Ingress canary route {canary_key!r} is {record.status}, not active.",
            )
        return record

    def ingress_request_from_canary_route_record(
        *,
        record: IngressCanaryRouteRecord,
        reason: str,
    ) -> NpmplusIngressApplyRequest:
        return NpmplusIngressApplyRequest.model_validate(
            {
                "schema_version": 1,
                "mode": "apply",
                "expected_host_id": record.expected_host_id,
                "require_exact_expected_host_domains": True,
                "reason": reason,
                "route": {
                    "domain_names": [record.domain_name],
                    "edge_endpoint_key": record.edge_endpoint_key,
                    "certificate_id": record.certificate_id,
                    "enabled": True,
                    "ssl_forced": True,
                    "http2_support": True,
                    "npmplus_http3_support": True,
                    "npmplus_noindex": True,
                    "access_list_id": 0,
                },
            }
        )

    def write_ingress_route_audit_record(
        *,
        ingress_store: _IngressRouteApplyStore,
        trace_id: str,
        product: str,
        context: str,
        provider: str,
        request: NpmplusIngressApplyRequest,
        result: NpmplusIngressApplyResult,
        idempotency_key: str,
    ) -> IngressRouteAuditRecord:
        provider_host_id = result.proxy_host.id if result.proxy_host is not None else None
        record = IngressRouteAuditRecord(
            record_id=build_ingress_route_audit_record_id(
                trace_id=trace_id,
                product=product,
                context=context,
                domains=request.route.domain_names,
            ),
            product=product,
            context=context,
            provider=provider,
            mode=request.mode,
            status=result.status,
            dry_run=result.dry_run,
            requested_domains=request.route.domain_names,
            edge_endpoint_key=request.route.edge_endpoint_key,
            expected_host_id=request.expected_host_id,
            provider_host_id=provider_host_id,
            operations=tuple(
                IngressRouteAuditOperation.model_validate(operation.model_dump(mode="json"))
                for operation in result.operations
            ),
            trace_id=trace_id,
            idempotency_key=idempotency_key,
            reason=request.reason,
            recorded_at=utc_now_timestamp(),
        )
        ingress_store.write_ingress_route_audit_record(record)
        return record

    def write_ingress_route_pending_audit_record(
        *,
        ingress_store: _IngressRouteApplyStore,
        trace_id: str,
        product: str,
        context: str,
        provider: str,
        request: NpmplusIngressApplyRequest,
        idempotency_key: str,
    ) -> IngressRouteAuditRecord:
        record = IngressRouteAuditRecord(
            record_id=build_ingress_route_audit_record_id(
                trace_id=trace_id,
                product=product,
                context=context,
                domains=request.route.domain_names,
            ),
            product=product,
            context=context,
            provider=provider,
            mode=request.mode,
            status="pending",
            dry_run=request.mode == "dry-run",
            requested_domains=request.route.domain_names,
            edge_endpoint_key=request.route.edge_endpoint_key,
            expected_host_id=request.expected_host_id,
            provider_host_id=None,
            operations=(
                IngressRouteAuditOperation(
                    action="pending",
                    host_id=None,
                    domain_names=request.route.domain_names,
                    requires_apply=request.mode == "apply",
                ),
            ),
            trace_id=trace_id,
            idempotency_key=idempotency_key,
            reason=request.reason,
            recorded_at=utc_now_timestamp(),
        )
        ingress_store.write_ingress_route_audit_record(record)
        return record

    async def apply_edge_endpoint(
        request: Request,
        endpoint_request: EdgeEndpointApplyEnvelope,
        identity: Annotated[LaunchplaneIdentity, Depends(read_write_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> AcceptedEvidenceResponse:
        trace_id = next_trace_id()
        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action="edge_endpoint.apply",
            product="launchplane",
            context=_LAUNCHPLANE_SERVICE_CONTEXT,
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Workflow cannot apply Launchplane edge endpoint records.",
            )
        if endpoint_request.mode == "apply" and not idempotency_key.strip():
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="idempotency_key_required",
                message="Edge endpoint apply requests require an Idempotency-Key header.",
            )
        (
            normalized_key,
            payload_fingerprint,
            replayed_response,
        ) = await replay_apply_idempotency(
            request=request,
            record_store=record_store,
            identity=identity,
            route_path=_EDGE_ENDPOINT_APPLY_ROUTE,
            idempotency_key=idempotency_key,
            trace_id=trace_id,
            check_replay=endpoint_request.mode == "apply",
        )
        if replayed_response is not None:
            return replayed_response
        try:
            endpoint_store = require_edge_endpoint_apply_store(record_store)
        except TypeError as error:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message=str(error),
            ) from error
        endpoint_status = "applied" if endpoint_request.mode == "apply" else "planned"
        if endpoint_request.mode == "apply":
            endpoint_store.write_edge_endpoint_record(endpoint_request.endpoint)
        response = accepted_evidence_response(
            trace_id=trace_id,
            records={
                "edge_endpoint_key": endpoint_request.endpoint.endpoint_key,
                "edge_endpoint_status": endpoint_status,
            },
            result={
                "mode": endpoint_request.mode,
                "endpoint_key": endpoint_request.endpoint.endpoint_key,
                "endpoint_status": endpoint_status,
                "record": endpoint_request.endpoint.model_dump(mode="json"),
            },
        )
        store_apply_idempotency(
            record_store=record_store,
            identity=identity,
            route_path=_EDGE_ENDPOINT_APPLY_ROUTE,
            idempotency_key=normalized_key,
            request_fingerprint_value=payload_fingerprint,
            trace_id=trace_id,
            response=response,
        )
        return response

    async def apply_private_health_endpoint(
        request: Request,
        endpoint_request: PrivateHealthEndpointApplyEnvelope,
        identity: Annotated[LaunchplaneIdentity, Depends(read_write_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> AcceptedEvidenceResponse:
        trace_id = next_trace_id()
        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action="private_health_endpoint.apply",
            product=endpoint_request.endpoint.product,
            context=endpoint_request.endpoint.context,
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message=(
                    "Workflow cannot apply private health endpoint records "
                    "for the requested product/context."
                ),
            )
        if endpoint_request.mode == "apply" and not idempotency_key.strip():
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="idempotency_key_required",
                message="Private health endpoint apply requests require an Idempotency-Key header.",
            )
        (
            normalized_key,
            payload_fingerprint,
            replayed_response,
        ) = await replay_apply_idempotency(
            request=request,
            record_store=record_store,
            identity=identity,
            route_path=_PRIVATE_HEALTH_ENDPOINT_APPLY_ROUTE,
            idempotency_key=idempotency_key,
            trace_id=trace_id,
            check_replay=endpoint_request.mode == "apply",
        )
        if replayed_response is not None:
            return replayed_response
        try:
            endpoint_store = require_private_health_endpoint_apply_store(record_store)
        except TypeError as error:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message=str(error),
            ) from error
        try:
            existing_endpoint = endpoint_store.read_private_health_endpoint_record(
                endpoint_request.endpoint.endpoint_key
            )
        except FileNotFoundError:
            existing_endpoint = None
        if existing_endpoint is not None and (
            existing_endpoint.product != endpoint_request.endpoint.product
            or existing_endpoint.context != endpoint_request.endpoint.context
            or existing_endpoint.instance != endpoint_request.endpoint.instance
        ):
            raise _launchplane_http_error(
                status_code=409,
                trace_id=trace_id,
                code="conflicting_private_health_endpoint",
                message=(
                    "Private health endpoint key already belongs to another "
                    "product/context/instance."
                ),
            )
        endpoint_status = "applied" if endpoint_request.mode == "apply" else "planned"
        if endpoint_request.mode == "apply":
            endpoint_store.write_private_health_endpoint_record(endpoint_request.endpoint)
        response = accepted_evidence_response(
            trace_id=trace_id,
            records={},
            result={
                "mode": endpoint_request.mode,
                "endpoint_key": endpoint_request.endpoint.endpoint_key,
                "endpoint_status": endpoint_status,
                "record": endpoint_request.endpoint.model_dump(mode="json"),
            },
        )
        store_apply_idempotency(
            record_store=record_store,
            identity=identity,
            route_path=_PRIVATE_HEALTH_ENDPOINT_APPLY_ROUTE,
            idempotency_key=normalized_key,
            request_fingerprint_value=payload_fingerprint,
            trace_id=trace_id,
            response=response,
        )
        return response

    async def apply_ingress_canary_route_record(
        request: Request,
        canary_request: IngressCanaryRouteRecordApplyEnvelope,
        identity: Annotated[LaunchplaneIdentity, Depends(read_write_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> AcceptedEvidenceResponse:
        trace_id = next_trace_id()
        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action="ingress_canary_route.apply",
            product="launchplane",
            context=_LAUNCHPLANE_SERVICE_CONTEXT,
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Workflow cannot apply Launchplane ingress canary route records.",
            )
        if canary_request.mode == "apply" and not idempotency_key.strip():
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="idempotency_key_required",
                message=(
                    "Ingress canary route record apply requests require an Idempotency-Key header."
                ),
            )
        (
            normalized_key,
            payload_fingerprint,
            replayed_response,
        ) = await replay_apply_idempotency(
            request=request,
            record_store=record_store,
            identity=identity,
            route_path=_INGRESS_CANARY_ROUTE_RECORD_APPLY_ROUTE,
            idempotency_key=idempotency_key,
            trace_id=trace_id,
            check_replay=canary_request.mode == "apply",
        )
        if replayed_response is not None:
            return replayed_response
        try:
            canary_store = require_ingress_canary_route_record_apply_store(record_store)
        except TypeError as error:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message=str(error),
            ) from error
        route_status = "applied" if canary_request.mode == "apply" else "planned"
        if canary_request.mode == "apply":
            canary_store.write_ingress_canary_route_record(canary_request.route)
        response = accepted_evidence_response(
            trace_id=trace_id,
            records={
                "ingress_canary_route_key": canary_request.route.canary_key,
                "ingress_canary_route_status": route_status,
            },
            result={
                "mode": canary_request.mode,
                "canary_key": canary_request.route.canary_key,
                "route_status": route_status,
                "record": canary_request.route.model_dump(mode="json"),
            },
        )
        if canary_request.mode == "apply":
            store_apply_idempotency(
                record_store=record_store,
                identity=identity,
                route_path=_INGRESS_CANARY_ROUTE_RECORD_APPLY_ROUTE,
                idempotency_key=normalized_key,
                request_fingerprint_value=payload_fingerprint,
                trace_id=trace_id,
                response=response,
            )
        return response

    async def apply_ingress_route(
        request: Request,
        route_request: NpmplusIngressApplyEnvelope,
        identity: Annotated[LaunchplaneIdentity, Depends(read_write_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> AcceptedEvidenceResponse:
        trace_id = next_trace_id()
        authz_action = (
            "ingress_route.apply" if route_request.ingress.mode == "apply" else "ingress_route.plan"
        )
        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action=authz_action,
            product=route_request.product,
            context=route_request.context,
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message=(
                    "Workflow cannot plan or apply the ingress route for the requested "
                    "product/context."
                ),
            )
        if route_request.ingress.mode == "apply" and not idempotency_key.strip():
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="idempotency_key_required",
                message="NPMplus ingress apply requests require an Idempotency-Key header.",
            )
        (
            normalized_key,
            payload_fingerprint,
            replayed_response,
        ) = await replay_apply_idempotency(
            request=request,
            record_store=record_store,
            identity=identity,
            route_path=_INGRESS_ROUTE_APPLY_ROUTE,
            idempotency_key=idempotency_key,
            trace_id=trace_id,
            check_replay=route_request.ingress.mode == "apply",
        )
        if replayed_response is not None:
            return replayed_response
        try:
            ingress_store = require_ingress_route_apply_store(record_store)
            resolved_ingress_request = route_request.ingress
            if route_request.ingress.route.edge_endpoint_key.strip():
                edge_endpoint_store = require_ingress_edge_endpoint_read_store(record_store)
                resolved_ingress_request = resolve_ingress_edge_endpoint(
                    edge_endpoint_store=edge_endpoint_store,
                    request=route_request.ingress,
                    trace_id=trace_id,
                )
        except TypeError as error:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message=str(error),
            ) from error

        try:
            ingress_provider = resolved_ingress_provider_factory()
            if resolved_ingress_request.mode == "apply":
                write_ingress_route_pending_audit_record(
                    ingress_store=ingress_store,
                    trace_id=trace_id,
                    product=route_request.product,
                    context=route_request.context,
                    provider=ingress_provider.provider_id,
                    request=resolved_ingress_request,
                    idempotency_key=normalized_key,
                )
            ingress_result = ingress_provider.apply_route(request=resolved_ingress_request)
        except (ValueError, click.ClickException) as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request could not be completed.",
            ) from error
        ingress_audit_record = write_ingress_route_audit_record(
            ingress_store=ingress_store,
            trace_id=trace_id,
            product=route_request.product,
            context=route_request.context,
            provider=ingress_provider.provider_id,
            request=resolved_ingress_request,
            result=ingress_result,
            idempotency_key=normalized_key,
        )
        response = accepted_evidence_response(
            trace_id=trace_id,
            records={
                "ingress_provider": ingress_provider.provider_id,
                "ingress_route_audit_record_id": ingress_audit_record.record_id,
            },
            result=ingress_result.model_dump(mode="json"),
        )
        if resolved_ingress_request.mode == "apply":
            store_apply_idempotency(
                record_store=record_store,
                identity=identity,
                route_path=_INGRESS_ROUTE_APPLY_ROUTE,
                idempotency_key=normalized_key,
                request_fingerprint_value=payload_fingerprint,
                trace_id=trace_id,
                response=response,
            )
        return response

    async def apply_ingress_canary_route(
        request: Request,
        canary_request: IngressCanaryRouteApplyEnvelope,
        identity: Annotated[LaunchplaneIdentity, Depends(read_write_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> AcceptedEvidenceResponse:
        trace_id = next_trace_id()
        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action="ingress_route.apply",
            product=canary_request.product,
            context=canary_request.context,
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message=(
                    "Workflow cannot apply the ingress canary route for the requested "
                    "product/context."
                ),
            )
        if not idempotency_key.strip():
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="idempotency_key_required",
                message="Ingress canary route apply requests require an Idempotency-Key header.",
            )
        (
            normalized_key,
            payload_fingerprint,
            replayed_response,
        ) = await replay_apply_idempotency(
            request=request,
            record_store=record_store,
            identity=identity,
            route_path=_INGRESS_CANARY_ROUTE_APPLY_ROUTE,
            idempotency_key=idempotency_key,
            trace_id=trace_id,
            check_replay=True,
        )
        if replayed_response is not None:
            return replayed_response
        try:
            canary_store = require_ingress_canary_route_apply_store(record_store)
        except TypeError as error:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message=str(error),
            ) from error
        canary_record = active_ingress_canary_route_record(
            canary_store=canary_store,
            canary_key=canary_request.canary_key,
            product=canary_request.product,
            context=canary_request.context,
            trace_id=trace_id,
        )
        ingress_request = ingress_request_from_canary_route_record(
            record=canary_record,
            reason=canary_request.reason,
        )
        resolved_ingress_request = resolve_ingress_edge_endpoint(
            edge_endpoint_store=canary_store,
            request=ingress_request,
            trace_id=trace_id,
        )
        try:
            ingress_provider = resolved_ingress_provider_factory()
            write_ingress_route_pending_audit_record(
                ingress_store=canary_store,
                trace_id=trace_id,
                product=canary_record.product,
                context=canary_record.context,
                provider=ingress_provider.provider_id,
                request=resolved_ingress_request,
                idempotency_key=normalized_key,
            )
            ingress_result = ingress_provider.apply_route(request=resolved_ingress_request)
        except (ValueError, click.ClickException) as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request could not be completed.",
            ) from error
        ingress_audit_record = write_ingress_route_audit_record(
            ingress_store=canary_store,
            trace_id=trace_id,
            product=canary_record.product,
            context=canary_record.context,
            provider=ingress_provider.provider_id,
            request=resolved_ingress_request,
            result=ingress_result,
            idempotency_key=normalized_key,
        )
        response = accepted_evidence_response(
            trace_id=trace_id,
            records={
                "ingress_provider": ingress_provider.provider_id,
                "ingress_route_audit_record_id": ingress_audit_record.record_id,
                "ingress_canary_route_key": canary_record.canary_key,
            },
            result=ingress_result.model_dump(mode="json"),
        )
        store_apply_idempotency(
            record_store=record_store,
            identity=identity,
            route_path=_INGRESS_CANARY_ROUTE_APPLY_ROUTE,
            idempotency_key=normalized_key,
            request_fingerprint_value=payload_fingerprint,
            trace_id=trace_id,
            response=response,
        )
        return response

    async def apply_public_ingress_notification_policy(
        request: Request,
        policy_request: PublicIngressNotificationPolicyApplyEnvelope,
        identity: Annotated[LaunchplaneIdentity, Depends(read_write_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> AcceptedEvidenceResponse:
        trace_id = next_trace_id()
        require_local_operator_notification_policy_reason(
            identity=identity,
            reason=policy_request.reason,
            trace_id=trace_id,
            message="Local operator public ingress notification policy apply requires a reason.",
        )
        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action="public_ingress_notification_policy.apply",
            product=policy_request.policy.product or "launchplane",
            context=policy_request.policy.context or _LAUNCHPLANE_SERVICE_CONTEXT,
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Workflow cannot apply public ingress notification policy.",
            )
        database_store = require_notification_policy_database_store(
            record_store=record_store,
            trace_id=trace_id,
            label="Public ingress",
        )
        (
            normalized_key,
            payload_fingerprint,
            replayed_response,
        ) = await replay_notification_policy_idempotency(
            request=request,
            record_store=database_store,
            identity=identity,
            route_path=_PUBLIC_INGRESS_NOTIFICATION_POLICY_APPLY_ROUTE,
            idempotency_key=idempotency_key,
            trace_id=trace_id,
        )
        if replayed_response is not None:
            return replayed_response
        if policy_request.mode == "apply":
            cast(
                _PublicIngressNotificationPolicyApplyStore,
                database_store,
            ).write_public_ingress_notification_policy_record(policy_request.policy)
        response = accepted_evidence_response(
            trace_id=trace_id,
            records={"public_ingress_notification_policy_id": policy_request.policy.policy_id},
            result={
                "mode": policy_request.mode,
                "changed": policy_request.mode == "apply",
                "policy": _public_ingress_notification_policy_summary(policy_request.policy),
            },
        )
        store_notification_policy_idempotency(
            record_store=database_store,
            identity=identity,
            route_path=_PUBLIC_INGRESS_NOTIFICATION_POLICY_APPLY_ROUTE,
            idempotency_key=normalized_key,
            request_fingerprint_value=payload_fingerprint,
            trace_id=trace_id,
            response=response,
        )
        return response

    async def apply_every_code_notification_policy(
        request: Request,
        policy_request: EveryCodeNotificationPolicyApplyEnvelope,
        identity: Annotated[LaunchplaneIdentity, Depends(read_write_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> AcceptedEvidenceResponse:
        trace_id = next_trace_id()
        require_local_operator_notification_policy_reason(
            identity=identity,
            reason=policy_request.reason,
            trace_id=trace_id,
            message="Local operator Every Code notification policy apply requires a reason.",
        )
        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action="every_code_notification_policy.apply",
            product="launchplane",
            context=_LAUNCHPLANE_SERVICE_CONTEXT,
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Workflow cannot apply Every Code notification policy.",
            )
        database_store = require_notification_policy_database_store(
            record_store=record_store,
            trace_id=trace_id,
            label="Every Code",
        )
        (
            normalized_key,
            payload_fingerprint,
            replayed_response,
        ) = await replay_notification_policy_idempotency(
            request=request,
            record_store=database_store,
            identity=identity,
            route_path=_EVERY_CODE_NOTIFICATION_POLICY_APPLY_ROUTE,
            idempotency_key=idempotency_key,
            trace_id=trace_id,
        )
        if replayed_response is not None:
            return replayed_response
        if policy_request.mode == "apply":
            cast(
                _EveryCodeNotificationPolicyApplyStore,
                database_store,
            ).write_every_code_notification_policy_record(policy_request.policy)
        response = accepted_evidence_response(
            trace_id=trace_id,
            records={"every_code_notification_policy_id": policy_request.policy.policy_id},
            result={
                "mode": policy_request.mode,
                "changed": policy_request.mode == "apply",
                "policy": _every_code_notification_policy_summary(policy_request.policy),
            },
        )
        store_notification_policy_idempotency(
            record_store=database_store,
            identity=identity,
            route_path=_EVERY_CODE_NOTIFICATION_POLICY_APPLY_ROUTE,
            idempotency_key=normalized_key,
            request_fingerprint_value=payload_fingerprint,
            trace_id=trace_id,
            response=response,
        )
        return response

    async def apply_runtime_key_safety_policy(
        request: Request,
        policy_request: RuntimeKeySafetyPolicyApplyEnvelope,
        identity: Annotated[LaunchplaneIdentity, Depends(read_write_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> AcceptedEvidenceResponse:
        trace_id = next_trace_id()
        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action="runtime_key_safety.write",
            product=policy_request.product,
            context=_LAUNCHPLANE_SERVICE_CONTEXT,
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Workflow cannot write Launchplane runtime key-safety policy records.",
            )
        database_store = require_runtime_key_safety_policy_database_store(
            record_store=record_store,
            trace_id=trace_id,
        )
        (
            normalized_key,
            payload_fingerprint,
            replayed_response,
        ) = await replay_apply_idempotency(
            request=request,
            record_store=database_store,
            identity=identity,
            route_path=_RUNTIME_KEY_SAFETY_POLICY_APPLY_ROUTE,
            idempotency_key=idempotency_key,
            trace_id=trace_id,
            check_replay=True,
        )
        if replayed_response is not None:
            return replayed_response
        route_result = apply_runtime_key_safety_policy_route(
            record_store=database_store,
            request=policy_request,
            now_timestamp=utc_now_timestamp,
            record_slug=_record_slug,
        )
        response = accepted_evidence_response(
            trace_id=trace_id,
            records={
                "runtime_key_safety_policy_record_id": str(
                    route_result.result["runtime_key_safety_policy_record_id"]
                ),
            },
            result=route_result.driver_result,
        )
        store_apply_idempotency(
            record_store=database_store,
            identity=identity,
            route_path=_RUNTIME_KEY_SAFETY_POLICY_APPLY_ROUTE,
            idempotency_key=normalized_key,
            request_fingerprint_value=payload_fingerprint,
            trace_id=trace_id,
            response=response,
        )
        return response

    async def setup_dokploy_target(
        request: Request,
        setup_request: DokployTargetSetupEnvelope,
        identity: Annotated[LaunchplaneIdentity, Depends(read_write_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> AcceptedEvidenceResponse:
        trace_id = next_trace_id()
        database_store = require_dokploy_target_setup_database_store(
            record_store=record_store,
            trace_id=trace_id,
        )
        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action="dokploy_target.setup",
            product=setup_request.product,
            context=_LAUNCHPLANE_SERVICE_CONTEXT,
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Workflow cannot run Launchplane Dokploy target setup.",
            )
        if setup_request.mode == "apply":
            if setup_request.confirmation != "APPLY DOKPLOY TARGET SETUP":
                raise _launchplane_http_error(
                    status_code=400,
                    trace_id=trace_id,
                    code="confirmation_required",
                    message="Dokploy target setup apply requires exact confirmation text.",
                )
            if not setup_request.reason:
                raise _launchplane_http_error(
                    status_code=400,
                    trace_id=trace_id,
                    code="reason_required",
                    message="Dokploy target setup apply requires a reason.",
                )
            if not idempotency_key.strip():
                raise _launchplane_http_error(
                    status_code=400,
                    trace_id=trace_id,
                    code="idempotency_key_required",
                    message="Dokploy target setup apply requires an Idempotency-Key header.",
                )
        (
            normalized_key,
            payload_fingerprint,
            replayed_response,
        ) = await replay_apply_idempotency(
            request=request,
            record_store=database_store,
            identity=identity,
            route_path=_DOKPLOY_TARGET_SETUP_ROUTE,
            idempotency_key=idempotency_key,
            trace_id=trace_id,
            check_replay=setup_request.mode == "apply",
        )
        if replayed_response is not None:
            return replayed_response
        try:
            result = execute_dokploy_target_setup(
                control_plane_root_path=resolved_control_plane_root,
                record_store=database_store,
                request=setup_request,
            )
        except (ValueError, click.ClickException) as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_dokploy_target_setup",
                message=str(error),
            ) from error
        response = accepted_evidence_response(
            trace_id=trace_id,
            records={},
            result={**result, "reason": setup_request.reason},
        )
        if setup_request.mode == "apply":
            store_apply_idempotency(
                record_store=database_store,
                identity=identity,
                route_path=_DOKPLOY_TARGET_SETUP_ROUTE,
                idempotency_key=normalized_key,
                request_fingerprint_value=payload_fingerprint,
                trace_id=trace_id,
                response=response,
            )
        return response

    async def apply_preview_pr_feedback_notification_policy(
        request: Request,
        policy_request: PreviewPrFeedbackNotificationPolicyApplyEnvelope,
        identity: Annotated[LaunchplaneIdentity, Depends(read_write_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> AcceptedEvidenceResponse:
        trace_id = next_trace_id()
        require_local_operator_notification_policy_reason(
            identity=identity,
            reason=policy_request.reason,
            trace_id=trace_id,
            message="Local operator preview PR feedback notification policy apply requires a reason.",
        )
        if not policy_request.policy.product or not policy_request.policy.context:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_policy_scope",
                message=(
                    "Preview PR feedback notification policy apply requires explicit product and context."
                ),
            )
        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action="preview_pr_feedback_notification_policy.apply",
            product=policy_request.policy.product,
            context=policy_request.policy.context,
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Workflow cannot apply preview PR feedback notification policy.",
            )
        database_store = require_notification_policy_database_store(
            record_store=record_store,
            trace_id=trace_id,
            label="Preview PR feedback",
        )
        (
            normalized_key,
            payload_fingerprint,
            replayed_response,
        ) = await replay_notification_policy_idempotency(
            request=request,
            record_store=database_store,
            identity=identity,
            route_path=_PREVIEW_PR_FEEDBACK_NOTIFICATION_POLICY_APPLY_ROUTE,
            idempotency_key=idempotency_key,
            trace_id=trace_id,
        )
        if replayed_response is not None:
            return replayed_response
        if policy_request.mode == "apply":
            cast(
                _PreviewPrFeedbackNotificationPolicyApplyStore,
                database_store,
            ).write_preview_pr_feedback_notification_policy_record(policy_request.policy)
        response = accepted_evidence_response(
            trace_id=trace_id,
            records={"preview_pr_feedback_notification_policy_id": policy_request.policy.policy_id},
            result={
                "mode": policy_request.mode,
                "changed": policy_request.mode == "apply",
                "policy": _preview_pr_feedback_notification_policy_summary(policy_request.policy),
            },
        )
        store_notification_policy_idempotency(
            record_store=database_store,
            identity=identity,
            route_path=_PREVIEW_PR_FEEDBACK_NOTIFICATION_POLICY_APPLY_ROUTE,
            idempotency_key=normalized_key,
            request_fingerprint_value=payload_fingerprint,
            trace_id=trace_id,
            response=response,
        )
        return response

    async def run_public_ingress_monitor(
        request: Request,
        monitor_request: PublicIngressMonitorRunOnceRequest,
        identity: Annotated[LaunchplaneIdentity, Depends(read_write_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> AcceptedEvidenceResponse:
        trace_id = next_trace_id()
        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action="public_ingress_monitor.run_once",
            product=monitor_request.product,
            context=_LAUNCHPLANE_SERVICE_CONTEXT,
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Workflow cannot run public ingress monitoring.",
            )

        idempotency_store = idempotency_capable_store(record_store)
        normalized_idempotency_key = idempotency_key.strip()
        normalized_scope = idempotency_scope(identity)
        raw_payload = await request.json()
        payload_fingerprint = request_fingerprint(cast(dict[str, object], raw_payload))
        if idempotency_store is not None and normalized_idempotency_key:
            stored_record = idempotency_store.read_idempotency_record(
                scope=normalized_scope,
                route_path=_PUBLIC_INGRESS_MONITOR_RUN_ONCE_ROUTE,
                idempotency_key=normalized_idempotency_key,
            )
            if stored_record is not None:
                if stored_record.request_fingerprint != payload_fingerprint:
                    raise _launchplane_http_error(
                        status_code=409,
                        trace_id=trace_id,
                        code="idempotency_key_reused",
                        message=(
                            "Idempotency-Key was already used for a different "
                            "Launchplane request payload on this route."
                        ),
                    )
                return replay_idempotent_response(
                    trace_id=trace_id,
                    stored_record=stored_record,
                )

        try:
            monitor_store = require_public_ingress_monitor_store(
                record_store,
                notify=monitor_request.notify,
            )
        except TypeError as error:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message=str(error),
            ) from error

        recorded_at = utc_now_timestamp()
        monitor_result = run_public_ingress_monitor_once(
            record_store=monitor_store,
            checked_at=recorded_at,
            timeout_seconds=monitor_request.timeout_seconds,
            notify=monitor_request.notify,
            notification_drivers=(
                public_ingress_notification_drivers(record_store=record_store)
                if monitor_request.notify
                else None
            ),
        )
        result = monitor_result.model_dump(mode="json")
        response = accepted_evidence_response(trace_id=trace_id, records={}, result=result)
        if idempotency_store is not None and normalized_idempotency_key:
            idempotency_store.write_idempotency_record(
                LaunchplaneIdempotencyRecord(
                    record_id=build_launchplane_idempotency_record_id(
                        response_trace_id=trace_id,
                    ),
                    scope=normalized_scope,
                    route_path=_PUBLIC_INGRESS_MONITOR_RUN_ONCE_ROUTE,
                    idempotency_key=normalized_idempotency_key,
                    request_fingerprint=payload_fingerprint,
                    response_status_code=202,
                    response_trace_id=trace_id,
                    recorded_at=recorded_at,
                    response_payload=response.model_dump(mode="json", exclude_none=True),
                )
            )
        return response

    async def write_deployment_evidence(
        request: Request,
        deployment_request: DeploymentEvidenceRequest,
        identity: Annotated[LaunchplaneIdentity, Depends(read_write_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> AcceptedEvidenceResponse:
        trace_id = next_trace_id()
        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action="deployment.write",
            product=deployment_request.product,
            context=deployment_request.deployment.context,
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message=(
                    "Workflow cannot write deployment evidence for the requested product/context."
                ),
            )

        idempotency_store = idempotency_capable_store(record_store)
        normalized_idempotency_key = idempotency_key.strip()
        normalized_scope = idempotency_scope(identity)
        raw_payload = await request.json()
        payload_fingerprint = request_fingerprint(cast(dict[str, object], raw_payload))
        if idempotency_store is not None and normalized_idempotency_key:
            stored_record = idempotency_store.read_idempotency_record(
                scope=normalized_scope,
                route_path=_DEPLOYMENT_EVIDENCE_ROUTE,
                idempotency_key=normalized_idempotency_key,
            )
            if stored_record is not None:
                if stored_record.request_fingerprint != payload_fingerprint:
                    raise _launchplane_http_error(
                        status_code=409,
                        trace_id=trace_id,
                        code="idempotency_key_reused",
                        message=(
                            "Idempotency-Key was already used for a different "
                            "Launchplane request payload on this route."
                        ),
                    )
                return replay_idempotent_response(
                    trace_id=trace_id,
                    stored_record=stored_record,
                )

        try:
            evidence_store = require_deployment_evidence_store(record_store)
        except TypeError as error:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message=str(error),
            ) from error

        records = {
            str(key): str(value)
            for key, value in apply_deployment_evidence(
                record_store=evidence_store,
                deployment_record=deployment_request.deployment,
            ).items()
        }
        response = accepted_evidence_response(trace_id=trace_id, records=records)
        if idempotency_store is not None and normalized_idempotency_key:
            idempotency_store.write_idempotency_record(
                LaunchplaneIdempotencyRecord(
                    record_id=build_launchplane_idempotency_record_id(
                        response_trace_id=trace_id,
                    ),
                    scope=normalized_scope,
                    route_path=_DEPLOYMENT_EVIDENCE_ROUTE,
                    idempotency_key=normalized_idempotency_key,
                    request_fingerprint=payload_fingerprint,
                    response_status_code=202,
                    response_trace_id=trace_id,
                    recorded_at=utc_now_timestamp(),
                    response_payload=response.model_dump(mode="json", exclude_none=True),
                )
            )
        return response

    async def write_backup_gate_evidence(
        request: Request,
        backup_gate_request: BackupGateEvidenceRequest,
        identity: Annotated[LaunchplaneIdentity, Depends(read_write_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> AcceptedEvidenceResponse:
        trace_id = next_trace_id()
        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action="backup_gate.write",
            product=backup_gate_request.product,
            context=backup_gate_request.backup_gate.context,
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message=(
                    "Workflow cannot write backup gate evidence for the requested product/context."
                ),
            )

        idempotency_store = idempotency_capable_store(record_store)
        normalized_idempotency_key = idempotency_key.strip()
        normalized_scope = idempotency_scope(identity)
        raw_payload = await request.json()
        payload_fingerprint = request_fingerprint(cast(dict[str, object], raw_payload))
        if idempotency_store is not None and normalized_idempotency_key:
            stored_record = idempotency_store.read_idempotency_record(
                scope=normalized_scope,
                route_path=_BACKUP_GATE_EVIDENCE_ROUTE,
                idempotency_key=normalized_idempotency_key,
            )
            if stored_record is not None:
                if stored_record.request_fingerprint != payload_fingerprint:
                    raise _launchplane_http_error(
                        status_code=409,
                        trace_id=trace_id,
                        code="idempotency_key_reused",
                        message=(
                            "Idempotency-Key was already used for a different "
                            "Launchplane request payload on this route."
                        ),
                    )
                return replay_idempotent_response(
                    trace_id=trace_id,
                    stored_record=stored_record,
                )

        try:
            evidence_store = require_backup_gate_evidence_store(record_store)
        except TypeError as error:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message=str(error),
            ) from error

        evidence_store.write_backup_gate_record(backup_gate_request.backup_gate)
        response = accepted_evidence_response(
            trace_id=trace_id,
            records={"backup_gate_record_id": backup_gate_request.backup_gate.record_id},
        )
        if idempotency_store is not None and normalized_idempotency_key:
            idempotency_store.write_idempotency_record(
                LaunchplaneIdempotencyRecord(
                    record_id=build_launchplane_idempotency_record_id(
                        response_trace_id=trace_id,
                    ),
                    scope=normalized_scope,
                    route_path=_BACKUP_GATE_EVIDENCE_ROUTE,
                    idempotency_key=normalized_idempotency_key,
                    request_fingerprint=payload_fingerprint,
                    response_status_code=202,
                    response_trace_id=trace_id,
                    recorded_at=utc_now_timestamp(),
                    response_payload=response.model_dump(mode="json", exclude_none=True),
                )
            )
        return response

    async def write_promotion_evidence(
        request: Request,
        promotion_request: PromotionEvidenceRequest,
        identity: Annotated[LaunchplaneIdentity, Depends(read_write_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> AcceptedEvidenceResponse:
        trace_id = next_trace_id()
        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action="promotion.write",
            product=promotion_request.product,
            context=promotion_request.promotion.context,
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message=(
                    "Workflow cannot write promotion evidence for the requested product/context."
                ),
            )

        idempotency_store = idempotency_capable_store(record_store)
        normalized_idempotency_key = idempotency_key.strip()
        normalized_scope = idempotency_scope(identity)
        raw_payload = await request.json()
        payload_fingerprint = request_fingerprint(cast(dict[str, object], raw_payload))
        if idempotency_store is not None and normalized_idempotency_key:
            stored_record = idempotency_store.read_idempotency_record(
                scope=normalized_scope,
                route_path=_PROMOTION_EVIDENCE_ROUTE,
                idempotency_key=normalized_idempotency_key,
            )
            if stored_record is not None:
                if stored_record.request_fingerprint != payload_fingerprint:
                    raise _launchplane_http_error(
                        status_code=409,
                        trace_id=trace_id,
                        code="idempotency_key_reused",
                        message=(
                            "Idempotency-Key was already used for a different "
                            "Launchplane request payload on this route."
                        ),
                    )
                return replay_idempotent_response(
                    trace_id=trace_id,
                    stored_record=stored_record,
                )

        try:
            evidence_store = require_promotion_evidence_store(
                record_store,
                promotion_request.promotion,
            )
        except TypeError as error:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message=str(error),
            ) from error

        try:
            records = {
                str(key): str(value)
                for key, value in apply_promotion_evidence(
                    record_store=evidence_store,
                    promotion_record=promotion_request.promotion,
                ).items()
            }
        except PromotionEvidenceValidationError as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message=str(error).strip() or "Request could not be completed.",
            ) from error

        response = accepted_evidence_response(trace_id=trace_id, records=records)
        if idempotency_store is not None and normalized_idempotency_key:
            idempotency_store.write_idempotency_record(
                LaunchplaneIdempotencyRecord(
                    record_id=build_launchplane_idempotency_record_id(
                        response_trace_id=trace_id,
                    ),
                    scope=normalized_scope,
                    route_path=_PROMOTION_EVIDENCE_ROUTE,
                    idempotency_key=normalized_idempotency_key,
                    request_fingerprint=payload_fingerprint,
                    response_status_code=202,
                    response_trace_id=trace_id,
                    recorded_at=utc_now_timestamp(),
                    response_payload=response.model_dump(mode="json", exclude_none=True),
                )
            )
        return response

    async def write_preview_generation_evidence(
        request: Request,
        preview_generation_request: PreviewGenerationEvidenceEnvelope,
        identity: Annotated[LaunchplaneIdentity, Depends(read_write_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> AcceptedEvidenceResponse:
        trace_id = next_trace_id()
        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action="preview_generation.write",
            product=preview_generation_request.product,
            context=preview_generation_request.preview.context,
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message=(
                    "Workflow cannot write preview generation evidence for the "
                    "requested product/context."
                ),
            )

        idempotency_store = idempotency_capable_store(record_store)
        normalized_idempotency_key = idempotency_key.strip()
        normalized_scope = idempotency_scope(identity)
        raw_payload = await request.json()
        payload_fingerprint = request_fingerprint(cast(dict[str, object], raw_payload))
        if idempotency_store is not None and normalized_idempotency_key:
            stored_record = idempotency_store.read_idempotency_record(
                scope=normalized_scope,
                route_path=_PREVIEW_GENERATION_EVIDENCE_ROUTE,
                idempotency_key=normalized_idempotency_key,
            )
            if stored_record is not None:
                if stored_record.request_fingerprint != payload_fingerprint:
                    raise _launchplane_http_error(
                        status_code=409,
                        trace_id=trace_id,
                        code="idempotency_key_reused",
                        message=(
                            "Idempotency-Key was already used for a different "
                            "Launchplane request payload on this route."
                        ),
                    )
                return replay_idempotent_response(
                    trace_id=trace_id,
                    stored_record=stored_record,
                )

        try:
            evidence_store = require_preview_generation_evidence_store(record_store)
        except TypeError as error:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message=str(error),
            ) from error

        try:
            records = {
                str(key): str(value)
                for key, value in apply_launchplane_generation_evidence(
                    control_plane_root_path=resolved_control_plane_root,
                    record_store=evidence_store,
                    preview_request=preview_generation_request.preview,
                    generation_request=preview_generation_request.generation,
                ).items()
            }
        except click.ClickException as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message=str(error).strip() or "Request could not be completed.",
            ) from error

        response = accepted_evidence_response(trace_id=trace_id, records=records)
        if idempotency_store is not None and normalized_idempotency_key:
            idempotency_store.write_idempotency_record(
                LaunchplaneIdempotencyRecord(
                    record_id=build_launchplane_idempotency_record_id(
                        response_trace_id=trace_id,
                    ),
                    scope=normalized_scope,
                    route_path=_PREVIEW_GENERATION_EVIDENCE_ROUTE,
                    idempotency_key=normalized_idempotency_key,
                    request_fingerprint=payload_fingerprint,
                    response_status_code=202,
                    response_trace_id=trace_id,
                    recorded_at=utc_now_timestamp(),
                    response_payload=response.model_dump(mode="json", exclude_none=True),
                )
            )
        return response

    async def write_preview_destroyed_evidence(
        request: Request,
        preview_destroyed_request: PreviewDestroyedEvidenceEnvelope,
        identity: Annotated[LaunchplaneIdentity, Depends(read_write_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> AcceptedEvidenceResponse:
        trace_id = next_trace_id()
        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action="preview_destroyed.write",
            product=preview_destroyed_request.product,
            context=preview_destroyed_request.destroy.context,
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message=(
                    "Workflow cannot write preview destroyed evidence for the "
                    "requested product/context."
                ),
            )

        idempotency_store = idempotency_capable_store(record_store)
        normalized_idempotency_key = idempotency_key.strip()
        normalized_scope = idempotency_scope(identity)
        raw_payload = await request.json()
        payload_fingerprint = request_fingerprint(cast(dict[str, object], raw_payload))
        if idempotency_store is not None and normalized_idempotency_key:
            stored_record = idempotency_store.read_idempotency_record(
                scope=normalized_scope,
                route_path=_PREVIEW_DESTROYED_EVIDENCE_ROUTE,
                idempotency_key=normalized_idempotency_key,
            )
            if stored_record is not None:
                if stored_record.request_fingerprint != payload_fingerprint:
                    raise _launchplane_http_error(
                        status_code=409,
                        trace_id=trace_id,
                        code="idempotency_key_reused",
                        message=(
                            "Idempotency-Key was already used for a different "
                            "Launchplane request payload on this route."
                        ),
                    )
                return replay_idempotent_response(
                    trace_id=trace_id,
                    stored_record=stored_record,
                )

        try:
            evidence_store = require_preview_destroyed_evidence_store(record_store)
        except TypeError as error:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message=str(error),
            ) from error

        try:
            records = {
                str(key): str(value)
                for key, value in apply_launchplane_destroy_preview(
                    record_store=evidence_store,
                    request=preview_destroyed_request.destroy,
                ).items()
            }
        except click.ClickException as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message=str(error).strip() or "Request could not be completed.",
            ) from error

        response = accepted_evidence_response(trace_id=trace_id, records=records)
        if idempotency_store is not None and normalized_idempotency_key:
            idempotency_store.write_idempotency_record(
                LaunchplaneIdempotencyRecord(
                    record_id=build_launchplane_idempotency_record_id(
                        response_trace_id=trace_id,
                    ),
                    scope=normalized_scope,
                    route_path=_PREVIEW_DESTROYED_EVIDENCE_ROUTE,
                    idempotency_key=normalized_idempotency_key,
                    request_fingerprint=payload_fingerprint,
                    response_status_code=202,
                    response_trace_id=trace_id,
                    recorded_at=utc_now_timestamp(),
                    response_payload=response.model_dump(mode="json", exclude_none=True),
                )
            )
        return response

    async def write_runner_host_hygiene_audit_evidence(
        request: Request,
        runner_host_hygiene_request: RunnerHostHygieneAuditEvidenceEnvelope,
        identity: Annotated[LaunchplaneIdentity, Depends(read_write_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> AcceptedEvidenceResponse:
        trace_id = next_trace_id()
        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action="runner_host_hygiene_audit.write",
            product=runner_host_hygiene_request.product,
            context=_LAUNCHPLANE_SERVICE_CONTEXT,
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Workflow cannot write runner host hygiene audit evidence.",
            )

        idempotency_store = idempotency_capable_store(record_store)
        normalized_idempotency_key = idempotency_key.strip()
        normalized_scope = idempotency_scope(identity)
        raw_payload = await request.json()
        payload_fingerprint = request_fingerprint(cast(dict[str, object], raw_payload))
        if idempotency_store is not None and normalized_idempotency_key:
            stored_record = idempotency_store.read_idempotency_record(
                scope=normalized_scope,
                route_path=_RUNNER_HOST_HYGIENE_AUDIT_EVIDENCE_ROUTE,
                idempotency_key=normalized_idempotency_key,
            )
            if stored_record is not None:
                if stored_record.request_fingerprint != payload_fingerprint:
                    raise _launchplane_http_error(
                        status_code=409,
                        trace_id=trace_id,
                        code="idempotency_key_reused",
                        message=(
                            "Idempotency-Key was already used for a different "
                            "Launchplane request payload on this route."
                        ),
                    )
                return replay_idempotent_response(
                    trace_id=trace_id,
                    stored_record=stored_record,
                )

        try:
            evidence_store = require_runner_host_hygiene_audit_evidence_store(record_store)
        except TypeError as error:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message=str(error),
            ) from error

        evidence_store.write_runner_host_hygiene_audit_record(runner_host_hygiene_request.audit)
        records = {
            "runner_host_hygiene_audit_record_key": (
                runner_host_hygiene_request.audit.audit_record_key
            ),
        }
        result: dict[str, object] = {
            "runner_host_hygiene_audit_record_key": (
                runner_host_hygiene_request.audit.audit_record_key
            ),
            "host_name": runner_host_hygiene_request.audit.request.host_name,
            "audit_status": runner_host_hygiene_request.audit.status,
            "mutate": runner_host_hygiene_request.audit.request.mutate,
            "audit": runner_host_hygiene_request.audit.model_dump(mode="json"),
        }
        response = accepted_evidence_response(
            trace_id=trace_id,
            records=records,
            result=result,
        )
        if idempotency_store is not None and normalized_idempotency_key:
            idempotency_store.write_idempotency_record(
                LaunchplaneIdempotencyRecord(
                    record_id=build_launchplane_idempotency_record_id(
                        response_trace_id=trace_id,
                    ),
                    scope=normalized_scope,
                    route_path=_RUNNER_HOST_HYGIENE_AUDIT_EVIDENCE_ROUTE,
                    idempotency_key=normalized_idempotency_key,
                    request_fingerprint=payload_fingerprint,
                    response_status_code=202,
                    response_trace_id=trace_id,
                    recorded_at=utc_now_timestamp(),
                    response_payload=response.model_dump(mode="json", exclude_none=True),
                )
            )
        return response

    async def write_runner_lane_registration_audit_evidence(
        request: Request,
        runner_lane_registration_request: RunnerLaneRegistrationAuditEvidenceEnvelope,
        identity: Annotated[LaunchplaneIdentity, Depends(read_write_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> AcceptedEvidenceResponse:
        trace_id = next_trace_id()
        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action="runner_lane_registration_audit.write",
            product=runner_lane_registration_request.product,
            context=_LAUNCHPLANE_SERVICE_CONTEXT,
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Workflow cannot write runner lane registration audit evidence.",
            )

        idempotency_store = idempotency_capable_store(record_store)
        normalized_idempotency_key = idempotency_key.strip()
        normalized_scope = idempotency_scope(identity)
        raw_payload = await request.json()
        payload_fingerprint = request_fingerprint(cast(dict[str, object], raw_payload))
        if idempotency_store is not None and normalized_idempotency_key:
            stored_record = idempotency_store.read_idempotency_record(
                scope=normalized_scope,
                route_path=_RUNNER_LANE_REGISTRATION_AUDIT_EVIDENCE_ROUTE,
                idempotency_key=normalized_idempotency_key,
            )
            if stored_record is not None:
                if stored_record.request_fingerprint != payload_fingerprint:
                    raise _launchplane_http_error(
                        status_code=409,
                        trace_id=trace_id,
                        code="idempotency_key_reused",
                        message=(
                            "Idempotency-Key was already used for a different "
                            "Launchplane request payload on this route."
                        ),
                    )
                return replay_idempotent_response(
                    trace_id=trace_id,
                    stored_record=stored_record,
                )

        try:
            evidence_store = require_runner_lane_registration_audit_evidence_store(record_store)
        except TypeError as error:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message=str(error),
            ) from error

        evidence_store.write_runner_lane_registration_audit_record(
            runner_lane_registration_request.audit
        )
        records = {
            "runner_lane_registration_audit_record_key": (
                runner_lane_registration_request.audit.audit_record_key
            ),
        }
        result: dict[str, object] = {
            "runner_lane_registration_audit_record_key": (
                runner_lane_registration_request.audit.audit_record_key
            ),
            "repository": runner_lane_registration_request.audit.request.repository,
            "host_name": runner_lane_registration_request.audit.request.host_name,
            "lane_name": runner_lane_registration_request.audit.request.lane_name,
            "audit_status": runner_lane_registration_request.audit.status,
            "mutate": runner_lane_registration_request.audit.request.mutate,
            "audit": runner_lane_registration_request.audit.model_dump(mode="json"),
        }
        response = accepted_evidence_response(
            trace_id=trace_id,
            records=records,
            result=result,
        )
        if idempotency_store is not None and normalized_idempotency_key:
            idempotency_store.write_idempotency_record(
                LaunchplaneIdempotencyRecord(
                    record_id=build_launchplane_idempotency_record_id(
                        response_trace_id=trace_id,
                    ),
                    scope=normalized_scope,
                    route_path=_RUNNER_LANE_REGISTRATION_AUDIT_EVIDENCE_ROUTE,
                    idempotency_key=normalized_idempotency_key,
                    request_fingerprint=payload_fingerprint,
                    response_status_code=202,
                    response_trace_id=trace_id,
                    recorded_at=utc_now_timestamp(),
                    response_payload=response.model_dump(mode="json", exclude_none=True),
                )
            )
        return response

    def read_health(record_store: Annotated[object, Depends(get_record_store)]) -> HealthResponse:
        return HealthResponse(
            trace_id=next_trace_id(),
            storage_backend=storage_backend_name(record_store),
        )

    def preserve_renewed_session_cookie(request: Request, response: JSONResponse) -> None:
        renewed_session_cookie = getattr(
            request.state,
            "launchplane_renewed_session_cookie",
            "",
        )
        if renewed_session_cookie:
            response.headers.append("Set-Cookie", str(renewed_session_cookie))

    app.add_api_route(
        "/v1/health",
        read_health,
        methods=["GET"],
        response_model=HealthResponse,
        operation_id="read_launchplane_health",
        summary="Read Launchplane service health",
    )

    app.add_api_route(
        "/v1/service/runtime",
        read_launchplane_runtime,
        methods=["GET"],
        response_model=LaunchplaneRuntimeResponse,
        operation_id="read_launchplane_runtime",
        summary="Read Launchplane service runtime",
        responses={
            401: {"model": LaunchplaneErrorResponse},
            403: {"model": LaunchplaneErrorResponse},
        },
    )

    app.add_api_route(
        "/v1/service/odoo-workers/status",
        read_odoo_stable_operation_worker_status,
        methods=["GET"],
        response_model=OdooStableOperationWorkerStatusResponse,
        operation_id="read_odoo_stable_operation_worker_status",
        summary="Read Odoo stable operation worker status",
        responses={
            400: {"model": LaunchplaneErrorResponse},
            401: {"model": LaunchplaneErrorResponse},
            403: {"model": LaunchplaneErrorResponse},
            503: {"model": LaunchplaneErrorResponse},
        },
    )

    app.add_api_route(
        "/v1/service/odoo-workers/reconcile",
        reconcile_odoo_stable_operation_workers,
        methods=["POST"],
        response_model=OdooStableOperationWorkerReconcileResponse,
        operation_id="reconcile_odoo_stable_operation_workers",
        summary="Reconcile stale Odoo stable operation workers",
        responses={
            400: {"model": LaunchplaneErrorResponse},
            401: {"model": LaunchplaneErrorResponse},
            403: {"model": LaunchplaneErrorResponse},
            503: {"model": LaunchplaneErrorResponse},
        },
    )

    app.add_api_route(
        _PUBLIC_INGRESS_NOTIFICATION_POLICY_APPLY_ROUTE,
        apply_public_ingress_notification_policy,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        operation_id="apply_public_ingress_notification_policy",
        summary="Apply public ingress notification policy",
        responses={
            400: {"model": LaunchplaneErrorResponse},
            401: {"model": LaunchplaneErrorResponse},
            403: {"model": LaunchplaneErrorResponse},
            409: {"model": LaunchplaneErrorResponse},
            503: {"model": LaunchplaneErrorResponse},
        },
    )

    app.add_api_route(
        _EVERY_CODE_NOTIFICATION_POLICY_APPLY_ROUTE,
        apply_every_code_notification_policy,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        operation_id="apply_every_code_notification_policy",
        summary="Apply Every Code notification policy",
        responses={
            400: {"model": LaunchplaneErrorResponse},
            401: {"model": LaunchplaneErrorResponse},
            403: {"model": LaunchplaneErrorResponse},
            409: {"model": LaunchplaneErrorResponse},
            503: {"model": LaunchplaneErrorResponse},
        },
    )

    app.add_api_route(
        _PREVIEW_PR_FEEDBACK_NOTIFICATION_POLICY_APPLY_ROUTE,
        apply_preview_pr_feedback_notification_policy,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        operation_id="apply_preview_pr_feedback_notification_policy",
        summary="Apply preview PR feedback notification policy",
        responses={
            400: {"model": LaunchplaneErrorResponse},
            401: {"model": LaunchplaneErrorResponse},
            403: {"model": LaunchplaneErrorResponse},
            409: {"model": LaunchplaneErrorResponse},
            503: {"model": LaunchplaneErrorResponse},
        },
    )

    app.add_api_route(
        _RUNTIME_KEY_SAFETY_POLICY_APPLY_ROUTE,
        apply_runtime_key_safety_policy,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        operation_id="apply_runtime_key_safety_policy",
        summary="Apply runtime key-safety policy records",
        responses={
            400: {"model": LaunchplaneErrorResponse},
            401: {"model": LaunchplaneErrorResponse},
            403: {"model": LaunchplaneErrorResponse},
            409: {"model": LaunchplaneErrorResponse},
            503: {"model": LaunchplaneErrorResponse},
        },
    )

    app.add_api_route(
        "/v1/drivers/odoo/stable-bootstrap/operations/{operation_id}",
        read_odoo_stable_bootstrap_operation_status,
        methods=["GET"],
        response_model=OdooStableBootstrapOperationStatusResponse,
        response_model_exclude_none=True,
        operation_id="read_odoo_stable_bootstrap_operation_status",
        summary="Read Odoo stable bootstrap operation status",
        responses={
            401: {"model": LaunchplaneErrorResponse},
            403: {"model": LaunchplaneErrorResponse},
            404: {"model": LaunchplaneErrorResponse},
            503: {"model": LaunchplaneErrorResponse},
        },
    )

    app.add_api_route(
        "/v1/drivers/odoo/target-replacement/operations/{operation_id}",
        read_odoo_stable_target_replacement_operation_status,
        methods=["GET"],
        response_model=OdooStableTargetReplacementOperationStatusResponse,
        response_model_exclude_none=True,
        operation_id="read_odoo_target_replacement_operation_status",
        summary="Read Odoo target replacement operation status",
        responses={
            401: {"model": LaunchplaneErrorResponse},
            403: {"model": LaunchplaneErrorResponse},
            404: {"model": LaunchplaneErrorResponse},
            503: {"model": LaunchplaneErrorResponse},
        },
    )

    app.add_api_route(
        "/v1/artifacts/protected",
        read_protected_artifacts,
        methods=["GET"],
        response_model=ProtectedArtifactsResponse,
        operation_id="read_protected_artifacts",
        summary="Read protected artifact inventory",
        responses={
            400: {"model": LaunchplaneErrorResponse},
            401: {"model": LaunchplaneErrorResponse},
            403: {"model": LaunchplaneErrorResponse},
            503: {"model": LaunchplaneErrorResponse},
        },
    )

    app.add_api_route(
        "/v1/drivers",
        read_driver_descriptors,
        methods=["GET"],
        response_model=DriverDescriptorsResponse,
        operation_id="read_driver_descriptors",
        summary="Read Launchplane driver descriptors",
        responses={
            401: {"model": LaunchplaneErrorResponse},
            403: {"model": LaunchplaneErrorResponse},
        },
    )

    app.add_api_route(
        "/v1/drivers/{driver_id}",
        read_driver_descriptor,
        methods=["GET"],
        response_model=DriverDescriptorResponse,
        operation_id="read_driver_descriptor",
        summary="Read one Launchplane driver descriptor",
        responses={
            400: {"model": LaunchplaneErrorResponse},
            401: {"model": LaunchplaneErrorResponse},
            403: {"model": LaunchplaneErrorResponse},
            404: {"model": LaunchplaneErrorResponse},
        },
    )

    app.add_api_route(
        "/v1/contexts/{context}/driver-view",
        read_driver_context_view,
        methods=["GET"],
        response_model=DriverContextViewResponse,
        operation_id="read_driver_context_view",
        summary="Read Launchplane driver view for a context",
        responses={
            401: {"model": LaunchplaneErrorResponse},
            403: {"model": LaunchplaneErrorResponse},
        },
    )

    app.add_api_route(
        "/v1/contexts/{context}/instances/{instance}/driver-view",
        read_driver_instance_view,
        methods=["GET"],
        response_model=DriverContextViewResponse,
        operation_id="read_driver_instance_view",
        summary="Read Launchplane driver view for one context instance",
        responses={
            401: {"model": LaunchplaneErrorResponse},
            403: {"model": LaunchplaneErrorResponse},
        },
    )

    app.add_api_route(
        "/v1/dokploy-targets/inspect",
        read_dokploy_target_inspect,
        methods=["GET"],
        response_model=DokployTargetInspectResponse,
        operation_id="read_dokploy_target_inspect",
        summary="Read redacted Dokploy target identity",
        responses={
            400: {"model": LaunchplaneErrorResponse},
            401: {"model": LaunchplaneErrorResponse},
            403: {"model": LaunchplaneErrorResponse},
            404: {"model": LaunchplaneErrorResponse},
            503: {"model": LaunchplaneErrorResponse},
        },
    )

    app.add_api_route(
        _DOKPLOY_TARGET_SETUP_ROUTE,
        setup_dokploy_target,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        operation_id="setup_dokploy_target",
        summary="Plan or apply Dokploy target setup",
        responses={
            400: {"model": LaunchplaneErrorResponse},
            401: {"model": LaunchplaneErrorResponse},
            403: {"model": LaunchplaneErrorResponse},
            409: {"model": LaunchplaneErrorResponse},
            503: {"model": LaunchplaneErrorResponse},
        },
    )

    app.add_api_route(
        "/v1/contexts/{context}/instances/{instance}/logs",
        read_tracked_target_logs,
        methods=["GET"],
        response_model=TrackedTargetLogsResponse,
        operation_id="read_tracked_target_logs",
        summary="Read tracked target logs",
        responses={
            400: {"model": LaunchplaneErrorResponse},
            401: {"model": LaunchplaneErrorResponse},
            403: {"model": LaunchplaneErrorResponse},
            503: {"model": LaunchplaneErrorResponse},
        },
    )

    app.add_api_route(
        _EDGE_ENDPOINT_APPLY_ROUTE,
        apply_edge_endpoint,
        methods=["POST"],
        response_model=AcceptedEvidenceResponse,
        status_code=202,
        operation_id="apply_edge_endpoint",
        summary="Plan or apply one Launchplane edge endpoint record",
        responses={
            400: {"model": LaunchplaneErrorResponse},
            401: {"model": LaunchplaneErrorResponse},
            403: {"model": LaunchplaneErrorResponse},
            409: {"model": LaunchplaneErrorResponse},
            503: {"model": LaunchplaneErrorResponse},
        },
    )

    app.add_api_route(
        _PRIVATE_HEALTH_ENDPOINT_APPLY_ROUTE,
        apply_private_health_endpoint,
        methods=["POST"],
        response_model=AcceptedEvidenceResponse,
        status_code=202,
        operation_id="apply_private_health_endpoint",
        summary="Plan or apply one private health endpoint record",
        responses={
            400: {"model": LaunchplaneErrorResponse},
            401: {"model": LaunchplaneErrorResponse},
            403: {"model": LaunchplaneErrorResponse},
            409: {"model": LaunchplaneErrorResponse},
            503: {"model": LaunchplaneErrorResponse},
        },
    )

    app.add_api_route(
        _INGRESS_ROUTE_APPLY_ROUTE,
        apply_ingress_route,
        methods=["POST"],
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        status_code=202,
        operation_id="apply_ingress_route",
        summary="Plan or apply an ingress route through the ingress provider",
        responses={
            400: {"model": LaunchplaneErrorResponse},
            401: {"model": LaunchplaneErrorResponse},
            403: {"model": LaunchplaneErrorResponse},
            409: {"model": LaunchplaneErrorResponse},
            503: {"model": LaunchplaneErrorResponse},
        },
    )

    app.add_api_route(
        _INGRESS_CANARY_ROUTE_RECORD_APPLY_ROUTE,
        apply_ingress_canary_route_record,
        methods=["POST"],
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        status_code=202,
        operation_id="apply_ingress_canary_route_record",
        summary="Plan or apply one Launchplane ingress canary route record",
        responses={
            400: {"model": LaunchplaneErrorResponse},
            401: {"model": LaunchplaneErrorResponse},
            403: {"model": LaunchplaneErrorResponse},
            409: {"model": LaunchplaneErrorResponse},
            503: {"model": LaunchplaneErrorResponse},
        },
    )

    app.add_api_route(
        _INGRESS_CANARY_ROUTE_APPLY_ROUTE,
        apply_ingress_canary_route,
        methods=["POST"],
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        status_code=202,
        operation_id="apply_ingress_canary_route",
        summary="Apply an active ingress canary route through the ingress provider",
        responses={
            400: {"model": LaunchplaneErrorResponse},
            401: {"model": LaunchplaneErrorResponse},
            403: {"model": LaunchplaneErrorResponse},
            409: {"model": LaunchplaneErrorResponse},
            503: {"model": LaunchplaneErrorResponse},
        },
    )

    app.add_api_route(
        "/v1/edge-endpoints/records",
        list_edge_endpoint_records,
        methods=["GET"],
        response_model=EdgeEndpointRecordsResponse,
        operation_id="list_edge_endpoint_records",
        summary="List edge endpoint records",
        responses={
            400: {"model": LaunchplaneErrorResponse},
            401: {"model": LaunchplaneErrorResponse},
            403: {"model": LaunchplaneErrorResponse},
            503: {"model": LaunchplaneErrorResponse},
        },
    )

    app.add_api_route(
        "/v1/edge-endpoints/records/{endpoint_key}",
        read_edge_endpoint_record,
        methods=["GET"],
        response_model=EdgeEndpointRecordResponse,
        operation_id="read_edge_endpoint_record",
        summary="Read one edge endpoint record",
        responses={
            401: {"model": LaunchplaneErrorResponse},
            403: {"model": LaunchplaneErrorResponse},
            404: {"model": LaunchplaneErrorResponse},
            503: {"model": LaunchplaneErrorResponse},
        },
    )

    app.add_api_route(
        "/v1/private-health-endpoints/records",
        list_private_health_endpoint_records,
        methods=["GET"],
        response_model=PrivateHealthEndpointRecordsResponse,
        operation_id="list_private_health_endpoint_records",
        summary="List private health endpoint records",
        responses={
            400: {"model": LaunchplaneErrorResponse},
            401: {"model": LaunchplaneErrorResponse},
            403: {"model": LaunchplaneErrorResponse},
            503: {"model": LaunchplaneErrorResponse},
        },
    )

    app.add_api_route(
        "/v1/private-health-endpoints/records/{endpoint_key}",
        read_private_health_endpoint_record,
        methods=["GET"],
        response_model=PrivateHealthEndpointRecordResponse,
        operation_id="read_private_health_endpoint_record",
        summary="Read one private health endpoint record",
        responses={
            400: {"model": LaunchplaneErrorResponse},
            401: {"model": LaunchplaneErrorResponse},
            403: {"model": LaunchplaneErrorResponse},
            404: {"model": LaunchplaneErrorResponse},
            503: {"model": LaunchplaneErrorResponse},
        },
    )

    app.add_api_route(
        "/v1/ingress/canary-routes/records",
        list_ingress_canary_route_records,
        methods=["GET"],
        response_model=IngressCanaryRouteRecordsResponse,
        operation_id="list_ingress_canary_route_records",
        summary="List ingress canary route records",
        responses={
            400: {"model": LaunchplaneErrorResponse},
            401: {"model": LaunchplaneErrorResponse},
            403: {"model": LaunchplaneErrorResponse},
            503: {"model": LaunchplaneErrorResponse},
        },
    )

    app.add_api_route(
        "/v1/ingress/canary-routes/records/{canary_key}",
        read_ingress_canary_route_record,
        methods=["GET"],
        response_model=IngressCanaryRouteRecordResponse,
        operation_id="read_ingress_canary_route_record",
        summary="Read one ingress canary route record",
        responses={
            401: {"model": LaunchplaneErrorResponse},
            403: {"model": LaunchplaneErrorResponse},
            404: {"model": LaunchplaneErrorResponse},
            503: {"model": LaunchplaneErrorResponse},
        },
    )

    app.add_api_route(
        "/v1/ingress/route-audits/records",
        list_ingress_route_audit_records,
        methods=["GET"],
        response_model=IngressRouteAuditRecordsResponse,
        operation_id="list_ingress_route_audit_records",
        summary="List ingress route audit records",
        responses={
            400: {"model": LaunchplaneErrorResponse},
            401: {"model": LaunchplaneErrorResponse},
            403: {"model": LaunchplaneErrorResponse},
            503: {"model": LaunchplaneErrorResponse},
        },
    )

    app.add_api_route(
        "/v1/ingress/route-audits/records/{record_id}",
        read_ingress_route_audit_record,
        methods=["GET"],
        response_model=IngressRouteAuditRecordResponse,
        operation_id="read_ingress_route_audit_record",
        summary="Read one ingress route audit record",
        responses={
            400: {"model": LaunchplaneErrorResponse},
            401: {"model": LaunchplaneErrorResponse},
            403: {"model": LaunchplaneErrorResponse},
            404: {"model": LaunchplaneErrorResponse},
            503: {"model": LaunchplaneErrorResponse},
        },
    )

    app.add_api_route(
        "/v1/deployments/{record_id}",
        read_deployment_record,
        methods=["GET"],
        response_model=DeploymentRecordResponse,
        operation_id="read_deployment_record",
        summary="Read one deployment record",
        responses={
            400: {"model": LaunchplaneErrorResponse},
            401: {"model": LaunchplaneErrorResponse},
            403: {"model": LaunchplaneErrorResponse},
            404: {"model": LaunchplaneErrorResponse},
            503: {"model": LaunchplaneErrorResponse},
        },
    )

    app.add_api_route(
        "/v1/promotions/{record_id}",
        read_promotion_record,
        methods=["GET"],
        response_model=PromotionRecordResponse,
        operation_id="read_promotion_record",
        summary="Read one promotion record",
        responses={
            400: {"model": LaunchplaneErrorResponse},
            401: {"model": LaunchplaneErrorResponse},
            403: {"model": LaunchplaneErrorResponse},
            404: {"model": LaunchplaneErrorResponse},
            503: {"model": LaunchplaneErrorResponse},
        },
    )

    every_code_read_error_responses: dict[int | str, dict[str, object]] = {
        400: {"model": LaunchplaneErrorResponse},
        401: {"model": LaunchplaneErrorResponse},
        403: {"model": LaunchplaneErrorResponse},
        404: {"model": LaunchplaneErrorResponse},
        503: {"model": LaunchplaneErrorResponse},
    }
    every_code_work_request_write_error_responses: dict[int | str, dict[str, object]] = {
        400: {"model": LaunchplaneErrorResponse},
        401: {"model": LaunchplaneErrorResponse},
        403: {"model": LaunchplaneErrorResponse},
        409: {"model": LaunchplaneErrorResponse},
        503: {"model": LaunchplaneErrorResponse},
    }
    every_code_worker_write_error_responses: dict[int | str, dict[str, object]] = {
        400: {"model": LaunchplaneErrorResponse},
        401: {"model": LaunchplaneErrorResponse},
        403: {"model": LaunchplaneErrorResponse},
        503: {"model": LaunchplaneErrorResponse},
    }
    every_code_worker_status_error_responses: dict[int | str, dict[str, object]] = {
        400: {"model": LaunchplaneErrorResponse},
        401: {"model": LaunchplaneErrorResponse},
        403: {"model": LaunchplaneErrorResponse},
        404: {"model": LaunchplaneErrorResponse},
        409: {"model": LaunchplaneErrorResponse},
        503: {"model": LaunchplaneErrorResponse},
    }

    app.add_api_route(
        "/v1/previews/readiness",
        read_preview_readiness,
        methods=["GET"],
        response_model=PreviewReadinessResponse,
        operation_id="read_preview_readiness",
        summary="Read preview readiness",
        responses=every_code_read_error_responses,
    )

    app.add_api_route(
        "/v1/previews/{preview_id}",
        read_preview_record,
        methods=["GET"],
        response_model=PreviewRecordResponse,
        operation_id="read_preview_record",
        summary="Read one preview record",
        responses={
            400: {"model": LaunchplaneErrorResponse},
            401: {"model": LaunchplaneErrorResponse},
            403: {"model": LaunchplaneErrorResponse},
            404: {"model": LaunchplaneErrorResponse},
            503: {"model": LaunchplaneErrorResponse},
        },
    )

    app.add_api_route(
        "/v1/previews/{preview_id}/history",
        read_preview_history,
        methods=["GET"],
        response_model=PreviewHistoryResponse,
        operation_id="read_preview_history",
        summary="Read one preview record with generation history",
        responses={
            400: {"model": LaunchplaneErrorResponse},
            401: {"model": LaunchplaneErrorResponse},
            403: {"model": LaunchplaneErrorResponse},
            404: {"model": LaunchplaneErrorResponse},
            503: {"model": LaunchplaneErrorResponse},
        },
    )

    app.add_api_route(
        "/v1/inventory/{context}/{instance}",
        read_environment_inventory,
        methods=["GET"],
        response_model=EnvironmentInventoryResponse,
        operation_id="read_environment_inventory",
        summary="Read one environment inventory record",
        responses={
            400: {"model": LaunchplaneErrorResponse},
            401: {"model": LaunchplaneErrorResponse},
            403: {"model": LaunchplaneErrorResponse},
            404: {"model": LaunchplaneErrorResponse},
            503: {"model": LaunchplaneErrorResponse},
        },
    )

    app.add_api_route(
        "/v1/contexts/{context}/operations/recent",
        read_recent_operations,
        methods=["GET"],
        response_model=RecentOperationsResponse,
        operation_id="read_recent_operations",
        summary="Read recent Launchplane operations for a context",
        responses={
            400: {"model": LaunchplaneErrorResponse},
            401: {"model": LaunchplaneErrorResponse},
            403: {"model": LaunchplaneErrorResponse},
            503: {"model": LaunchplaneErrorResponse},
        },
    )

    product_environment_error_responses: dict[int | str, dict[str, object]] = {
        400: {"model": LaunchplaneErrorResponse},
        401: {"model": LaunchplaneErrorResponse},
        403: {"model": LaunchplaneErrorResponse},
        404: {"model": LaunchplaneErrorResponse},
        503: {"model": LaunchplaneErrorResponse},
    }

    agent_context_error_responses: dict[int | str, dict[str, object]] = {
        401: {"model": LaunchplaneErrorResponse},
        403: {"model": LaunchplaneErrorResponse},
        503: {"model": LaunchplaneErrorResponse},
    }

    app.add_api_route(
        "/v1/repo-product-mapping",
        read_repo_product_mapping,
        methods=["GET"],
        response_model=RepoProductMappingResponse,
        operation_id="read_repo_product_mapping",
        summary="Read repository product mapping",
        responses=agent_context_error_responses,
    )

    app.add_api_route(
        "/v1/agent/context",
        read_agent_context,
        methods=["GET"],
        response_model=AgentContextResponse,
        operation_id="read_agent_context",
        summary="Read Launchplane agent context",
        responses=agent_context_error_responses,
    )

    app.add_api_route(
        "/v1/work-graph/snapshot",
        read_work_graph_snapshot,
        methods=["GET"],
        response_model=WorkGraphSnapshotResponse,
        operation_id="read_work_graph_snapshot",
        summary="Read Launchplane work graph snapshot",
        responses=agent_context_error_responses,
    )

    app.add_api_route(
        "/v1/work-graph/rank",
        rank_work_graph_snapshot,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        operation_id="rank_work_graph_snapshot",
        summary="Rank Launchplane work graph snapshot",
        responses={
            400: {"model": LaunchplaneErrorResponse},
            401: {"model": LaunchplaneErrorResponse},
            403: {"model": LaunchplaneErrorResponse},
        },
    )

    app.add_api_route(
        "/v1/work-graph/github/issues",
        read_work_graph_issue_inbox,
        methods=["GET"],
        response_model=WorkGraphIssueInboxResponse,
        operation_id="read_work_graph_issue_inbox",
        summary="Read Launchplane GitHub issue inbox",
        responses=agent_context_error_responses,
    )

    app.add_api_route(
        "/v1/work-graph/github/issues/reconcile",
        reconcile_work_graph_issue_inbox,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        operation_id="reconcile_work_graph_issue_inbox",
        summary="Reconcile Launchplane GitHub issue inbox",
        responses={
            400: {"model": LaunchplaneErrorResponse},
            401: {"model": LaunchplaneErrorResponse},
            403: {"model": LaunchplaneErrorResponse},
        },
    )

    merge_train_read_error_responses: dict[int | str, dict[str, object]] = {
        400: {"model": LaunchplaneErrorResponse},
        401: {"model": LaunchplaneErrorResponse},
        403: {"model": LaunchplaneErrorResponse},
        503: {"model": LaunchplaneErrorResponse},
    }

    app.add_api_route(
        "/v1/work-graph/merge-train/admission",
        read_merge_train_admission,
        methods=["GET"],
        response_model=MergeTrainAdmissionResponse,
        operation_id="read_merge_train_admission",
        summary="Read merge train admission",
        responses=merge_train_read_error_responses,
    )

    app.add_api_route(
        "/v1/work-graph/merge-train/controller/status",
        read_merge_train_controller_status,
        methods=["GET"],
        response_model=MergeTrainControllerStatusResponse,
        operation_id="read_merge_train_controller_status",
        summary="Read merge train controller status",
        responses=merge_train_read_error_responses,
    )

    app.add_api_route(
        "/v1/work-graph/merge-train/policy-targets",
        read_merge_train_policy_targets,
        methods=["GET"],
        response_model=MergeTrainPolicyTargetsResponse,
        operation_id="read_merge_train_policy_targets",
        summary="Read merge train policy targets",
        responses=merge_train_read_error_responses,
    )

    app.add_api_route(
        "/v1/every-code/summary",
        read_every_code_summary,
        methods=["GET"],
        response_model=EveryCodeSummaryResponse,
        operation_id="read_every_code_summary",
        summary="Read Every Code work request summary",
        responses=every_code_read_error_responses,
    )

    app.add_api_route(
        "/v1/every-code/work-requests",
        list_every_code_work_requests,
        methods=["GET"],
        response_model=EveryCodeWorkRequestRecordsResponse,
        operation_id="list_every_code_work_requests",
        summary="List Every Code work requests",
        responses=every_code_read_error_responses,
    )

    app.add_api_route(
        "/v1/every-code/work-requests/{request_id}",
        read_every_code_work_request,
        methods=["GET"],
        response_model=EveryCodeWorkRequestRecordResponse,
        operation_id="read_every_code_work_request",
        summary="Read one Every Code work request",
        responses=every_code_read_error_responses,
    )

    app.add_api_route(
        "/v1/every-code/work-requests/create",
        create_every_code_work_request,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        operation_id="create_every_code_work_request",
        summary="Create Every Code work request",
        responses=every_code_work_request_write_error_responses,
    )

    app.add_api_route(
        "/v1/every-code/pr-feedback",
        list_every_code_pr_feedback,
        methods=["GET"],
        response_model=EveryCodePrFeedbackRecordsResponse,
        operation_id="list_every_code_pr_feedback",
        summary="List Every Code PR feedback records",
        responses=every_code_read_error_responses,
    )

    app.add_api_route(
        "/v1/every-code/pr-feedback",
        write_every_code_pr_feedback,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        operation_id="write_every_code_pr_feedback",
        summary="Write Every Code PR feedback",
        responses=every_code_worker_write_error_responses,
    )

    app.add_api_route(
        "/v1/every-code/pr-feedback/status",
        write_every_code_pr_feedback_status,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        operation_id="write_every_code_pr_feedback_status",
        summary="Write Every Code PR feedback status",
        responses=every_code_worker_status_error_responses,
    )

    app.add_api_route(
        "/v1/every-code/preview-gates",
        list_every_code_preview_gates,
        methods=["GET"],
        response_model=EveryCodePreviewGateRecordsResponse,
        operation_id="list_every_code_preview_gates",
        summary="List Every Code preview gates",
        responses=every_code_read_error_responses,
    )

    app.add_api_route(
        "/v1/every-code/preview-gates",
        write_every_code_preview_gate,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        operation_id="write_every_code_preview_gate",
        summary="Write Every Code preview gate",
        responses=every_code_worker_write_error_responses,
    )

    app.add_api_route(
        "/v1/every-code/notification-attempts",
        list_every_code_notification_attempts,
        methods=["GET"],
        response_model=EveryCodeNotificationAttemptRecordsResponse,
        operation_id="list_every_code_notification_attempts",
        summary="List Every Code notification attempts",
        responses=every_code_read_error_responses,
    )

    app.add_api_route(
        "/v1/previews/pr-feedback/notification-attempts",
        list_preview_pr_feedback_notification_attempts,
        methods=["GET"],
        response_model=PreviewPrFeedbackNotificationAttemptRecordsResponse,
        operation_id="list_preview_pr_feedback_notification_attempts",
        summary="List preview PR feedback notification attempts",
        responses=every_code_read_error_responses,
    )

    app.add_api_route(
        "/v1/products",
        list_product_environment_overviews,
        methods=["GET"],
        response_model=ProductEnvironmentListResponse,
        operation_id="list_products",
        summary="List product environment overviews",
        responses=product_environment_error_responses,
    )

    app.add_api_route(
        "/v1/products/{product}",
        read_product_overview,
        methods=["GET"],
        response_model=ProductOverviewResponse,
        operation_id="read_product",
        summary="Read a product environment overview",
        responses=product_environment_error_responses,
    )

    app.add_api_route(
        "/v1/products/{product}/activity",
        read_product_activity,
        methods=["GET"],
        response_model=ProductActivityResponse,
        operation_id="read_product_activity",
        summary="Read product activity",
        responses=product_environment_error_responses,
    )

    app.add_api_route(
        "/v1/products/{product}/environments",
        list_product_environments,
        methods=["GET"],
        response_model=ProductEnvironmentsResponse,
        operation_id="list_product_environments",
        summary="List product environments",
        responses=product_environment_error_responses,
    )

    app.add_api_route(
        "/v1/products/{product}/environments/{environment}",
        read_product_environment,
        methods=["GET"],
        response_model=ProductEnvironmentResponse,
        operation_id="read_product_environment",
        summary="Read one product environment",
        responses=product_environment_error_responses,
    )

    app.add_api_route(
        "/v1/product-profiles",
        list_product_profiles,
        methods=["GET"],
        response_model=ProductProfileListResponse,
        operation_id="list_product_profiles",
        summary="List Launchplane product profiles",
        responses={
            401: {"model": LaunchplaneErrorResponse},
            403: {"model": LaunchplaneErrorResponse},
            503: {"model": LaunchplaneErrorResponse},
        },
    )

    app.add_api_route(
        _PRODUCT_PROFILES_ROUTE,
        write_product_profile,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": {"$ref": "#/components/schemas/LaunchplaneProductProfileRecord"}
                    }
                },
            }
        },
        operation_id="write_product_profile",
        summary="Write a Launchplane product profile",
        responses={
            400: {"model": LaunchplaneErrorResponse},
            401: {"model": LaunchplaneErrorResponse},
            403: {"model": LaunchplaneErrorResponse},
            409: {"model": LaunchplaneErrorResponse},
            503: {"model": LaunchplaneErrorResponse},
        },
    )

    app.add_api_route(
        "/v1/product-profiles/{product}",
        read_product_profile,
        methods=["GET"],
        response_model=ProductProfileResponse,
        operation_id="read_product_profile",
        summary="Read a Launchplane product profile",
        responses={
            401: {"model": LaunchplaneErrorResponse},
            403: {"model": LaunchplaneErrorResponse},
            404: {"model": LaunchplaneErrorResponse},
            503: {"model": LaunchplaneErrorResponse},
        },
    )

    app.add_api_route(
        _PRODUCT_CONTEXT_CUTOVER_APPLY_ROUTE,
        apply_product_context_cutover,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": control_plane_product_context_cutover.ProductContextCutoverRequest.model_json_schema()
                    }
                },
            }
        },
        operation_id="apply_product_context_cutover",
        summary="Plan or apply product context cutover",
        responses={
            400: {"model": LaunchplaneErrorResponse},
            401: {"model": LaunchplaneErrorResponse},
            403: {"model": LaunchplaneErrorResponse},
            404: {"model": LaunchplaneErrorResponse},
            409: {"model": LaunchplaneErrorResponse},
            503: {"model": LaunchplaneErrorResponse},
        },
    )

    app.add_api_route(
        _PRODUCT_LEGACY_CONTEXT_CLEANUP_APPLY_ROUTE,
        apply_product_legacy_context_cleanup,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": control_plane_product_context_cutover.LegacyContextCleanupRequest.model_json_schema()
                    }
                },
            }
        },
        operation_id="apply_product_legacy_context_cleanup",
        summary="Plan or apply legacy product context cleanup",
        responses={
            400: {"model": LaunchplaneErrorResponse},
            401: {"model": LaunchplaneErrorResponse},
            403: {"model": LaunchplaneErrorResponse},
            404: {"model": LaunchplaneErrorResponse},
            409: {"model": LaunchplaneErrorResponse},
            503: {"model": LaunchplaneErrorResponse},
        },
    )

    app.add_api_route(
        "/v1/product-profiles/{product}/context-cutover-audit",
        read_product_context_cutover_audit,
        methods=["GET"],
        response_model=ProductContextCutoverAuditResponse,
        operation_id="read_product_context_cutover_audit",
        summary="Read a product context cutover audit",
        responses={
            400: {"model": LaunchplaneErrorResponse},
            401: {"model": LaunchplaneErrorResponse},
            403: {"model": LaunchplaneErrorResponse},
            404: {"model": LaunchplaneErrorResponse},
            503: {"model": LaunchplaneErrorResponse},
        },
    )

    app.add_api_route(
        "/v1/contexts/{context}/secrets",
        list_context_secret_statuses,
        methods=["GET"],
        response_model=SecretStatusListResponse,
        operation_id="list_context_secret_statuses",
        summary="List Launchplane managed secret status for a context",
        responses={
            400: {"model": LaunchplaneErrorResponse},
            401: {"model": LaunchplaneErrorResponse},
            403: {"model": LaunchplaneErrorResponse},
            404: {"model": LaunchplaneErrorResponse},
            503: {"model": LaunchplaneErrorResponse},
        },
    )

    app.add_api_route(
        "/v1/contexts/{context}/instances/{instance}/secrets",
        list_instance_secret_statuses,
        methods=["GET"],
        response_model=SecretStatusListResponse,
        operation_id="list_instance_secret_statuses",
        summary="List Launchplane managed secret status for an instance",
        responses={
            400: {"model": LaunchplaneErrorResponse},
            401: {"model": LaunchplaneErrorResponse},
            403: {"model": LaunchplaneErrorResponse},
            404: {"model": LaunchplaneErrorResponse},
            503: {"model": LaunchplaneErrorResponse},
        },
    )

    app.add_api_route(
        "/v1/secrets/{secret_id}",
        read_secret_status,
        methods=["GET"],
        response_model=SecretStatusResponse,
        operation_id="read_secret_status",
        summary="Read Launchplane managed secret status",
        responses={
            400: {"model": LaunchplaneErrorResponse},
            401: {"model": LaunchplaneErrorResponse},
            403: {"model": LaunchplaneErrorResponse},
            404: {"model": LaunchplaneErrorResponse},
            503: {"model": LaunchplaneErrorResponse},
        },
    )

    app.add_api_route(
        _PUBLIC_INGRESS_MONITOR_RUN_ONCE_ROUTE,
        run_public_ingress_monitor,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        operation_id="run_public_ingress_monitor",
        summary="Run public ingress monitoring once",
        responses={
            400: {"model": LaunchplaneErrorResponse},
            401: {"model": LaunchplaneErrorResponse},
            403: {"model": LaunchplaneErrorResponse},
            409: {"model": LaunchplaneErrorResponse},
            503: {"model": LaunchplaneErrorResponse},
        },
    )

    app.add_api_route(
        _BACKUP_GATE_EVIDENCE_ROUTE,
        write_backup_gate_evidence,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        operation_id="write_backup_gate_evidence",
        summary="Write backup gate evidence",
        responses={
            400: {"model": LaunchplaneErrorResponse},
            401: {"model": LaunchplaneErrorResponse},
            403: {"model": LaunchplaneErrorResponse},
            409: {"model": LaunchplaneErrorResponse},
            503: {"model": LaunchplaneErrorResponse},
        },
    )

    app.add_api_route(
        _PROMOTION_EVIDENCE_ROUTE,
        write_promotion_evidence,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        operation_id="write_promotion_evidence",
        summary="Write promotion evidence",
        responses={
            400: {"model": LaunchplaneErrorResponse},
            401: {"model": LaunchplaneErrorResponse},
            403: {"model": LaunchplaneErrorResponse},
            409: {"model": LaunchplaneErrorResponse},
            503: {"model": LaunchplaneErrorResponse},
        },
    )

    app.add_api_route(
        _PREVIEW_GENERATION_EVIDENCE_ROUTE,
        write_preview_generation_evidence,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        operation_id="write_preview_generation_evidence",
        summary="Write preview generation evidence",
        responses={
            400: {"model": LaunchplaneErrorResponse},
            401: {"model": LaunchplaneErrorResponse},
            403: {"model": LaunchplaneErrorResponse},
            409: {"model": LaunchplaneErrorResponse},
            503: {"model": LaunchplaneErrorResponse},
        },
    )

    app.add_api_route(
        _PREVIEW_DESTROYED_EVIDENCE_ROUTE,
        write_preview_destroyed_evidence,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        operation_id="write_preview_destroyed_evidence",
        summary="Write preview destroyed evidence",
        responses={
            400: {"model": LaunchplaneErrorResponse},
            401: {"model": LaunchplaneErrorResponse},
            403: {"model": LaunchplaneErrorResponse},
            409: {"model": LaunchplaneErrorResponse},
            503: {"model": LaunchplaneErrorResponse},
        },
    )

    app.add_api_route(
        _RUNNER_HOST_HYGIENE_AUDIT_EVIDENCE_ROUTE,
        write_runner_host_hygiene_audit_evidence,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        operation_id="write_runner_host_hygiene_audit_evidence",
        summary="Write runner host hygiene audit evidence",
        responses={
            400: {"model": LaunchplaneErrorResponse},
            401: {"model": LaunchplaneErrorResponse},
            403: {"model": LaunchplaneErrorResponse},
            409: {"model": LaunchplaneErrorResponse},
            503: {"model": LaunchplaneErrorResponse},
        },
    )

    app.add_api_route(
        _RUNNER_LANE_REGISTRATION_AUDIT_EVIDENCE_ROUTE,
        write_runner_lane_registration_audit_evidence,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        operation_id="write_runner_lane_registration_audit_evidence",
        summary="Write runner lane registration audit evidence",
        responses={
            400: {"model": LaunchplaneErrorResponse},
            401: {"model": LaunchplaneErrorResponse},
            403: {"model": LaunchplaneErrorResponse},
            409: {"model": LaunchplaneErrorResponse},
            503: {"model": LaunchplaneErrorResponse},
        },
    )

    app.add_api_route(
        _DEPLOYMENT_EVIDENCE_ROUTE,
        write_deployment_evidence,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        operation_id="write_deployment_evidence",
        summary="Write deployment evidence",
        responses={
            400: {"model": LaunchplaneErrorResponse},
            401: {"model": LaunchplaneErrorResponse},
            403: {"model": LaunchplaneErrorResponse},
            409: {"model": LaunchplaneErrorResponse},
            503: {"model": LaunchplaneErrorResponse},
        },
    )

    def launchplane_http_exception_handler(request: Request, error: Exception) -> JSONResponse:
        if not isinstance(error, HTTPException):
            raise error
        http_error = error
        trace_id = next_trace_id()
        code = "authentication_required" if http_error.status_code == 401 else "http_error"
        if isinstance(http_error.detail, dict):
            detail = http_error.detail
            trace_id = str(detail.get("trace_id", trace_id))
            code = str(detail.get("code", code))
            message = str(detail.get("message", "Launchplane request failed."))
        else:
            message = str(http_error.detail)
        payload = LaunchplaneErrorResponse(
            trace_id=trace_id,
            error=LaunchplaneErrorDetail(code=code, message=message),
        )
        response = JSONResponse(
            status_code=http_error.status_code,
            content=payload.model_dump(mode="json"),
            headers=http_error.headers,
        )
        preserve_renewed_session_cookie(request, response)
        return response

    def launchplane_request_validation_exception_handler(
        request: Request, error: Exception
    ) -> JSONResponse:
        if not isinstance(error, RequestValidationError):
            raise error
        payload = LaunchplaneErrorResponse(
            trace_id=next_trace_id(),
            error=LaunchplaneErrorDetail(
                code="invalid_request",
                message="Launchplane request validation failed.",
            ),
        )
        response = JSONResponse(
            status_code=400,
            content=payload.model_dump(mode="json"),
        )
        preserve_renewed_session_cookie(request, response)
        return response

    app.add_api_route(
        "/v1/products/{product}/environments/{environment}/config-status",
        read_product_environment_config_status,
        methods=["GET"],
        response_model=ProductEnvironmentConfigStatusResponse,
        responses={
            400: {"model": LaunchplaneErrorResponse},
            401: {"model": LaunchplaneErrorResponse},
            403: {"model": LaunchplaneErrorResponse},
            404: {"model": LaunchplaneErrorResponse},
            503: {"model": LaunchplaneErrorResponse},
        },
    )
    app.add_exception_handler(HTTPException, launchplane_http_exception_handler)
    app.add_exception_handler(
        RequestValidationError,
        launchplane_request_validation_exception_handler,
    )

    return app


def _launchplane_http_error(
    *, status_code: int, trace_id: str, code: str, message: str
) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={"trace_id": trace_id, "code": code, "message": message},
    )


def _record_slug(value: str) -> str:
    compact = "".join(
        character.lower() if character.isalnum() else "-" for character in value.strip()
    )
    normalized = "-".join(part for part in compact.split("-") if part)
    return normalized or "launchplane-record"


def _authentication_required_error(message: str) -> HTTPException:
    return HTTPException(
        status_code=401,
        detail={"code": "authentication_required", "message": message},
        headers=_BEARER_CHALLENGE_HEADER,
    )
