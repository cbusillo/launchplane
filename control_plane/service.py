from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import logging
import mimetypes
import os
import re
import secrets
from socketserver import ThreadingMixIn
import uuid
from pathlib import Path
from typing import BinaryIO, Callable, Generic, Iterable, Literal, Protocol, TypeVar, cast
from urllib.parse import parse_qs, unquote
from wsgiref.simple_server import WSGIServer, make_server
from wsgiref.types import WSGIApplication

import click
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from jwt import InvalidTokenError

from control_plane import authz_grant_service as control_plane_authz_grant_service
from control_plane import dokploy as control_plane_dokploy
from control_plane import product_context_audit as control_plane_product_context_audit
from control_plane import product_context_cutover as control_plane_product_context_cutover
from control_plane import product_onboarding_service as control_plane_product_onboarding_service
from control_plane import product_read_service as control_plane_product_read_service
from control_plane import live_target_runtime as control_plane_live_target_runtime
from control_plane import secrets as control_plane_secrets
from control_plane.agent_context_service import (
    agent_context_action_allowed,
    agent_context_allowed,
    build_agent_context_service_payload,
)
from control_plane.contracts.authz_policy_record import (
    LaunchplaneAuthzPolicyRecord,
    authz_policy_sha256,
    build_authz_policy_record_id,
)
from control_plane.contracts.agent_write_intent import (
    AgentWriteIntentRecord,
    AgentWriteIntentRequest,
    AgentWriteIntentSecretEvidence,
    agent_write_intent_secret_action,
    authz_action_for_agent_write_intent,
    build_agent_write_intent_record_id,
    evaluate_agent_write_intent,
    secret_evidence_for_agent_write_intent,
)
from control_plane.contracts.backup_gate_record import BackupGateRecord
from control_plane.contracts.deployment_record import DeploymentRecord
from control_plane.contracts.every_code_work_request import (
    EveryCodeWorkRequestRecord,
    EveryCodeWorkRequestStatusUpdate,
    apply_every_code_work_request_status,
    build_every_code_work_request_id,
    close_every_code_work_request_for_issue,
    close_every_code_work_request_for_pull_request,
    requeue_every_code_work_request,
)
from control_plane.contracts.every_code_preview_gate_record import (
    EveryCodePreviewGateRecord,
)
from control_plane.contracts.every_code_pr_feedback_record import (
    EveryCodePrFeedbackKind,
    EveryCodePrFeedbackRecord,
    EveryCodePrFeedbackStatus,
    apply_every_code_pr_feedback_status,
    build_every_code_pr_feedback_id,
)
from control_plane.contracts.every_code_summary_read_model import (
    build_every_code_summary_read_model,
)
from control_plane.contracts.idempotency_record import LaunchplaneIdempotencyRecord
from control_plane.contracts.idempotency_record import build_launchplane_idempotency_record_id
from control_plane.contracts.merge_train_policy import (
    build_sellyouroutboard_main_merge_train_policy,
)
from control_plane.contracts.merge_train_run_record import build_merge_train_run_record
from control_plane.merge_train_admission import evaluate_merge_train_admission_from_store
from control_plane.contracts.preview_mutation_request import (
    PreviewDestroyMutationRequest,
    PreviewGenerationMutationRequest,
    PreviewMutationRequest,
)
from control_plane.contracts.preview_desired_state_record import PreviewDesiredStateRecord
from control_plane.contracts.preview_inventory_scan_record import (
    PreviewInventoryScanRecord,
    build_preview_inventory_scan_id,
)
from control_plane.contracts.preview_lifecycle_plan_record import (
    PreviewLifecycleDesiredPreview,
    PreviewLifecyclePlanRecord,
)
from control_plane.contracts.preview_lifecycle_cleanup_record import PreviewLifecycleCleanupRecord
from control_plane.contracts.preview_pr_feedback_record import (
    PreviewPrFeedbackRecord,
    PreviewPrFeedbackStatus,
)
from control_plane.contracts.preview_readiness_read_model import (
    build_preview_readiness_read_model,
)
from control_plane.contracts.product_environment_read_model import ProductReadModelStore
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductLaneProfile,
)
from control_plane.contracts.product_onboarding_manifest import ProductOnboardingManifest
from control_plane.contracts.promotion_record import (
    HealthcheckEvidence,
    PostDeployUpdateEvidence,
    PromotionRecord,
    ReleaseStatus,
)
from control_plane.contracts.runtime_key_safety_policy import RuntimeKeySafetyTarget
from control_plane.runtime_key_safety import (
    evaluate_runtime_key_safety_from_store,
    latest_active_runtime_key_safety_policy,
    runtime_key_safety_environment_class,
)
from control_plane.runtime_key_safety_http import (
    RuntimeKeySafetyPolicyRouteResult,
    apply_runtime_key_safety_policy_route,
    runtime_key_safety_database_required_response,
    validate_runtime_key_safety_policy_request,
)
from control_plane.drivers.registry import (
    build_driver_context_view,
    list_driver_descriptors,
    read_driver_descriptor,
)
from control_plane.launchplane_mutations import (
    apply_launchplane_destroy_preview,
    apply_launchplane_generation_evidence,
    control_plane_root,
)
from control_plane.service_auth import (
    AgentAuthzDecision,
    GitHubActionsIdentity,
    GitHubHumanIdentity,
    LaunchplaneAuthzPolicy,
    LaunchplaneIdentity,
    TerminalAgentIdentity,
    TokenVerifier,
    agent_authz_audit,
    load_authz_policy,
    parse_authz_policy_toml,
)
from control_plane.service_human_auth import (
    GitHubOAuthClient,
    GitHubOAuthConfig,
    HumanSessionManager,
    HumanSessionStore,
    InMemoryHumanSessionStore,
    OAuthLoginStateStore,
    build_pkce_verifier,
    load_github_oauth_config_from_env,
)
from control_plane.storage.factory import build_record_store, storage_backend_name
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.tracked_target_logs import build_tracked_target_logs_payload
from control_plane.product_config_http import (
    ProductConfigRouteResult,
    apply_product_config_route,
    product_config_database_required_response,
    validate_product_config_apply_request,
)
from control_plane.work_graph_github_projects import (
    build_github_project_planning_facts,
    load_github_project_planning_facts_config_from_env,
)
from control_plane.work_graph_service import (
    WorkGraphPlanningFactsProvider,
)
from control_plane.work_graph_http import (
    handle_repo_product_mapping_read,
    handle_work_graph_snapshot_read,
    rank_work_graph_snapshot,
    work_graph_rank_denied_response,
)
from control_plane.merge_train import build_merge_train_dry_run_result
from control_plane.merge_train_github import (
    GitHubMergeTrainClient,
    GitHubMergeTrainSnapshotReader,
    MergeTrainGitHubError,
    UrllibMergeTrainGitHubTransport,
)
from control_plane.workflows.merge_train_worker import (
    MergeTrainWorkerClients,
    run_merge_train_worker_step,
)
from control_plane.workflows.evidence_ingestion import (
    apply_deployment_evidence,
    apply_promotion_evidence,
)
from control_plane.workflows.generic_web_deploy import (
    GenericWebDeployRequest,
    execute_generic_web_deploy,
)
from control_plane.workflows.generic_web_promotion import (
    GenericWebProdPromotionRequest,
    execute_generic_web_prod_promotion,
    resolve_generic_web_promotion_lanes,
)
from control_plane.workflows.generic_web_promotion_workflow import (
    GenericWebPromotionWorkflowRequest,
    dispatch_generic_web_promotion_workflow,
)
from control_plane.workflows.generic_web_preview import (
    GenericWebPreviewDesiredStateRequest,
    GenericWebPreviewDestroyRequest,
    GenericWebPreviewInventoryRequest,
    GenericWebPreviewReadinessRequest,
    GenericWebPreviewRefreshRequest,
    GenericWebPreviewRefreshResult,
    GenericWebPreviewProfileStore,
    discover_generic_web_preview_desired_state,
    evaluate_generic_web_preview_readiness,
    execute_generic_web_preview_destroy,
    execute_generic_web_preview_inventory,
    execute_generic_web_preview_refresh,
    resolve_generic_web_preview_profile,
    preview_pr_number_from_slug,
)
from control_plane.workflows.product_onboarding import apply_product_onboarding_manifest
from control_plane.workflows.preview_desired_state import discover_github_preview_desired_state
from control_plane.workflows.preview_lifecycle import build_preview_lifecycle_plan
from control_plane.workflows.preview_lifecycle_cleanup import (
    build_preview_lifecycle_cleanup_record,
)
from control_plane.workflows.preview_pr_feedback import (
    DEFAULT_PREVIEW_FEEDBACK_MARKER,
    build_preview_pr_feedback_record,
    handle_every_code_preview_validation_comment,
)
from control_plane.workflows.launchplane import (
    find_preview_record,
    launchplane_anchor_repo_context,
    resolve_launchplane_github_token,
    verify_github_webhook_signature,
)
from control_plane.workflows.odoo_artifact_publish import (
    OdooArtifactPublishEvidenceRequest,
    OdooArtifactPublishInputsRequest,
    build_odoo_artifact_publish_inputs,
    ingest_odoo_artifact_publish_evidence,
)
from control_plane.workflows.odoo_post_deploy import (
    OdooPostDeployRequest,
    execute_odoo_post_deploy,
)
from control_plane.workflows.odoo_prod_backup_gate import (
    OdooProdBackupGateRequest,
    execute_odoo_prod_backup_gate,
)
from control_plane.workflows.odoo_prod_promotion import (
    OdooProdPromotionRequest,
    execute_odoo_prod_promotion,
)
from control_plane.workflows.odoo_prod_rollback import (
    OdooProdRollbackRequest,
    execute_odoo_prod_rollback,
)
from control_plane.workflows.verireel_stable_deploy import (
    VeriReelStableDeployRequest,
    execute_verireel_stable_deploy,
)
from control_plane.workflows.verireel_environment import (
    VeriReelStableEnvironmentRequest,
    resolve_verireel_stable_environment,
)
from control_plane.workflows.verireel_rollout import (
    VeriReelRolloutVerificationRequest,
    execute_verireel_rollout_verification,
)
from control_plane.workflows.verireel_app_maintenance import (
    VeriReelAppMaintenanceRequest,
    execute_verireel_app_maintenance,
)
from control_plane.workflows.verireel_prod_backup_gate import (
    VeriReelProdBackupGateRequest,
    execute_verireel_prod_backup_gate,
)
from control_plane.workflows.verireel_prod_promotion import (
    VeriReelProdPromotionRequest,
    execute_verireel_prod_promotion,
)
from control_plane.workflows.verireel_prod_rollback import (
    VeriReelProdRollbackRequest,
    execute_verireel_prod_rollback,
)
from control_plane.workflows.verireel_preview_driver import (
    VeriReelPreviewDestroyRequest,
    VeriReelPreviewDestroyResult,
    VeriReelPreviewInventoryRequest,
    VeriReelPreviewRefreshRequest,
    VeriReelPreviewRefreshResult,
    execute_verireel_preview_destroy,
    execute_verireel_preview_inventory,
    execute_verireel_preview_refresh,
)


_LAUNCHPLANE_SERVICE_CONTEXT = "launchplane"
_EVERY_CODE_GITHUB_WEBHOOK_ROUTE = "/v1/every-code/github-webhook"
_MERGE_TRAIN_ADMISSION_ROUTE = "/v1/work-graph/merge-train/admission"
_MERGE_TRAIN_RUN_ONCE_ROUTE = "/v1/work-graph/merge-train/run-once"
_EVERY_CODE_GITHUB_WEBHOOK_SECRET_ENV_KEY = "LAUNCHPLANE_EVERY_CODE_GITHUB_WEBHOOK_SECRET"


class MergeTrainRunOnceEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    repository: str
    base_branch: str = "main"
    mutate: bool = False
    github_api_base_url: str = "https://api.github.com"

    @model_validator(mode="after")
    def _validate_envelope(self) -> "MergeTrainRunOnceEnvelope":
        self.repository = self.repository.strip()
        self.base_branch = self.base_branch.strip()
        self.github_api_base_url = self.github_api_base_url.strip() or "https://api.github.com"
        if not self.repository:
            raise ValueError("merge train run-once requires repository")
        if "/" not in self.repository:
            raise ValueError("merge train repository must be owner/name")
        if not self.base_branch:
            raise ValueError("merge train run-once requires base_branch")
        return self


class MergeTrainAdmissionEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repository: str
    base_branch: str = "main"

    @model_validator(mode="after")
    def _validate_envelope(self) -> "MergeTrainAdmissionEnvelope":
        self.repository = self.repository.strip()
        self.base_branch = self.base_branch.strip()
        if not self.repository:
            raise ValueError("merge train admission requires repository")
        if "/" not in self.repository:
            raise ValueError("merge train repository must be owner/name")
        if not self.base_branch:
            raise ValueError("merge train admission requires base_branch")
        return self
_GITHUB_CLOSING_REFERENCE_PATTERN = re.compile(
    r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+([^\n\r]+)", re.IGNORECASE
)
_GITHUB_ISSUE_REFERENCE_PATTERN = re.compile(
    r"https://github\.com/(?P<url_repository>[^/\s]+/[^/\s]+)/issues/(?P<url_number>\d+)"
    r"|(?:(?P<repository>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#|#)(?P<number>\d+)",
    re.IGNORECASE,
)
_EVERY_CODE_TRIGGER_LABEL = "every-code"


@dataclass(frozen=True)
class _DriverRouteMetadata:
    driver_id: str
    action_id: str
    method: str
    authz_action: str
    operator_visible: bool


class _ProductRouteEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product: str


_DriverRouteEnvelopeT = TypeVar("_DriverRouteEnvelopeT", bound=_ProductRouteEnvelope)


class _IdempotencyCapableStore(Protocol):
    def read_idempotency_record(
        self,
        *,
        scope: str,
        route_path: str,
        idempotency_key: str,
    ) -> LaunchplaneIdempotencyRecord: ...

    def write_idempotency_record(self, record: LaunchplaneIdempotencyRecord) -> object: ...


_StartResponse = Callable[[str, list[tuple[str, str]]], None]
_WsgiApp = Callable[[dict[str, object], _StartResponse], list[bytes]]


@dataclass(frozen=True)
class _DriverRouteExecutionMetadata(Generic[_DriverRouteEnvelopeT]):
    route_path: str
    envelope_model: type[_DriverRouteEnvelopeT]
    denial_message: str


@dataclass(frozen=True)
class _ResolvedProductDriverContext:
    profile: LaunchplaneProductProfileRecord | None
    lane: ProductLaneProfile | None = None


_LAUNCHPLANE_IMAGE_REFERENCE_ENV_KEY = "DOCKER_IMAGE_REFERENCE"
_LOGGER = logging.getLogger(__name__)
_LAUNCHPLANE_SELF_DEPLOY_OAUTH_ENV_KEYS = frozenset(
    {
        "LAUNCHPLANE_GITHUB_CLIENT_ID",
        "LAUNCHPLANE_GITHUB_CLIENT_SECRET",
        "LAUNCHPLANE_PUBLIC_URL",
        "LAUNCHPLANE_SESSION_SECRET",
        "LAUNCHPLANE_COOKIE_SECURE",
        "LAUNCHPLANE_BOOTSTRAP_ADMIN_EMAILS",
        "LAUNCHPLANE_EVERY_CODE_GITHUB_WEBHOOK_SECRET",
        "LAUNCHPLANE_EVERY_CODE_WORKER_TOKEN",
        "LAUNCHPLANE_TERMINAL_AGENT_READ_TOKEN",
        "LAUNCHPLANE_TERMINAL_AGENT_SUBJECT",
        "LAUNCHPLANE_TERMINAL_AGENT_TOKEN_LABEL",
        "LAUNCHPLANE_WORK_GRAPH_PROJECT_OWNER",
        "LAUNCHPLANE_WORK_GRAPH_PROJECT_NUMBER",
        "LAUNCHPLANE_WORK_GRAPH_PROJECT_LIMIT",
        "LAUNCHPLANE_WORK_GRAPH_PROJECT_SIGNAL_LIMIT",
        "LAUNCHPLANE_WORK_GRAPH_GH_BINARY",
        "GH_TOKEN",
    }
)


class PreviewGenerationEvidenceEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    product: str
    preview: PreviewMutationRequest
    generation: PreviewGenerationMutationRequest

    @model_validator(mode="after")
    def _validate_alignment(self) -> "PreviewGenerationEvidenceEnvelope":
        if not self.product.strip():
            raise ValueError("preview generation evidence requires product")
        if self.preview.context != self.generation.context:
            raise ValueError("preview generation evidence requires matching contexts")
        if self.preview.anchor_repo != self.generation.anchor_repo:
            raise ValueError("preview generation evidence requires matching anchor_repo")
        if self.preview.anchor_pr_number != self.generation.anchor_pr_number:
            raise ValueError("preview generation evidence requires matching anchor_pr_number")
        if self.preview.anchor_pr_url != self.generation.anchor_pr_url:
            raise ValueError("preview generation evidence requires matching anchor_pr_url")
        return self


class PreviewDestroyedEvidenceEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    product: str
    destroy: PreviewDestroyMutationRequest

    @model_validator(mode="after")
    def _validate_alignment(self) -> "PreviewDestroyedEvidenceEnvelope":
        if not self.product.strip():
            raise ValueError("preview destroyed evidence requires product")
        return self


class DeploymentEvidenceEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    product: str
    deployment: DeploymentRecord

    @model_validator(mode="after")
    def _validate_alignment(self) -> "DeploymentEvidenceEnvelope":
        if not self.product.strip():
            raise ValueError("deployment evidence requires product")
        return self


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


class GenericWebPreviewDesiredStateEnvelope(_ProductRouteEnvelope):
    schema_version: int = Field(default=1, ge=1)
    desired_state: GenericWebPreviewDesiredStateRequest

    @model_validator(mode="after")
    def _validate_alignment(self) -> "GenericWebPreviewDesiredStateEnvelope":
        if not self.product.strip():
            raise ValueError("generic web preview desired state requires product")
        if self.product.strip() != self.desired_state.product.strip():
            raise ValueError("generic web preview desired state requires matching product values")
        return self


_GENERIC_WEB_PREVIEW_DESIRED_STATE_ROUTE = _DriverRouteExecutionMetadata(
    route_path="/v1/drivers/generic-web/preview-desired-state",
    envelope_model=GenericWebPreviewDesiredStateEnvelope,
    denial_message=(
        "Workflow cannot discover generic web preview desired state"
        " for the requested product/context."
    ),
)


class GenericWebPreviewRefreshEnvelope(_ProductRouteEnvelope):
    schema_version: int = Field(default=1, ge=1)
    refresh: GenericWebPreviewRefreshRequest

    @model_validator(mode="after")
    def _validate_alignment(self) -> "GenericWebPreviewRefreshEnvelope":
        if not self.product.strip():
            raise ValueError("generic web preview refresh requires product")
        if self.product.strip() != self.refresh.product.strip():
            raise ValueError("generic web preview refresh requires matching product values")
        return self


class GenericWebPreviewInventoryEnvelope(_ProductRouteEnvelope):
    schema_version: int = Field(default=1, ge=1)
    inventory: GenericWebPreviewInventoryRequest

    @model_validator(mode="after")
    def _validate_alignment(self) -> "GenericWebPreviewInventoryEnvelope":
        if not self.product.strip():
            raise ValueError("generic web preview inventory requires product")
        if self.product.strip() != self.inventory.product.strip():
            raise ValueError("generic web preview inventory requires matching product values")
        return self


_GENERIC_WEB_PREVIEW_REFRESH_ROUTE = _DriverRouteExecutionMetadata(
    route_path="/v1/drivers/generic-web/preview-refresh",
    envelope_model=GenericWebPreviewRefreshEnvelope,
    denial_message=(
        "Workflow cannot refresh generic web preview state for the requested product/context."
    ),
)


_GENERIC_WEB_PREVIEW_INVENTORY_ROUTE = _DriverRouteExecutionMetadata(
    route_path="/v1/drivers/generic-web/preview-inventory",
    envelope_model=GenericWebPreviewInventoryEnvelope,
    denial_message=(
        "Workflow cannot read generic web preview inventory for the requested product/context."
    ),
)


class GenericWebPreviewReadinessEnvelope(_ProductRouteEnvelope):
    schema_version: int = Field(default=1, ge=1)
    readiness: GenericWebPreviewReadinessRequest

    @model_validator(mode="after")
    def _validate_alignment(self) -> "GenericWebPreviewReadinessEnvelope":
        if not self.product.strip():
            raise ValueError("generic web preview readiness requires product")
        if self.product.strip() != self.readiness.product.strip():
            raise ValueError("generic web preview readiness requires matching product values")
        return self


_GENERIC_WEB_PREVIEW_READINESS_ROUTE = _DriverRouteExecutionMetadata(
    route_path="/v1/drivers/generic-web/preview-readiness",
    envelope_model=GenericWebPreviewReadinessEnvelope,
    denial_message=(
        "Workflow cannot evaluate generic web preview readiness for the requested product/context."
    ),
)


class GenericWebPreviewDestroyEnvelope(_ProductRouteEnvelope):
    schema_version: int = Field(default=1, ge=1)
    destroy: GenericWebPreviewDestroyRequest

    @model_validator(mode="after")
    def _validate_alignment(self) -> "GenericWebPreviewDestroyEnvelope":
        if not self.product.strip():
            raise ValueError("generic web preview destroy requires product")
        if self.product.strip() != self.destroy.product.strip():
            raise ValueError("generic web preview destroy requires matching product values")
        return self


_GENERIC_WEB_PREVIEW_DESTROY_ROUTE = _DriverRouteExecutionMetadata(
    route_path="/v1/drivers/generic-web/preview-destroy",
    envelope_model=GenericWebPreviewDestroyEnvelope,
    denial_message=(
        "Workflow cannot destroy generic web preview state for the requested product/context."
    ),
)


_ODOO_PREVIEW_DESIRED_STATE_ROUTE = _DriverRouteExecutionMetadata(
    route_path="/v1/drivers/odoo/preview-desired-state",
    envelope_model=GenericWebPreviewDesiredStateEnvelope,
    denial_message=(
        "Workflow cannot discover Odoo preview desired state for the requested product/context."
    ),
)


_ODOO_PREVIEW_REFRESH_ROUTE = _DriverRouteExecutionMetadata(
    route_path="/v1/drivers/odoo/preview-refresh",
    envelope_model=GenericWebPreviewRefreshEnvelope,
    denial_message="Workflow cannot refresh Odoo preview state for the requested product/context.",
)


_ODOO_PREVIEW_INVENTORY_ROUTE = _DriverRouteExecutionMetadata(
    route_path="/v1/drivers/odoo/preview-inventory",
    envelope_model=GenericWebPreviewInventoryEnvelope,
    denial_message="Workflow cannot read Odoo preview inventory for the requested product/context.",
)


_ODOO_PREVIEW_READINESS_ROUTE = _DriverRouteExecutionMetadata(
    route_path="/v1/drivers/odoo/preview-readiness",
    envelope_model=GenericWebPreviewReadinessEnvelope,
    denial_message="Workflow cannot evaluate Odoo preview readiness for the requested product/context.",
)


_ODOO_PREVIEW_DESTROY_ROUTE = _DriverRouteExecutionMetadata(
    route_path="/v1/drivers/odoo/preview-destroy",
    envelope_model=GenericWebPreviewDestroyEnvelope,
    denial_message="Workflow cannot destroy Odoo preview state for the requested product/context.",
)


_PREVIEW_DESIRED_STATE_ROUTE_PATHS = frozenset(
    {
        _GENERIC_WEB_PREVIEW_DESIRED_STATE_ROUTE.route_path,
        _ODOO_PREVIEW_DESIRED_STATE_ROUTE.route_path,
    }
)
_PREVIEW_INVENTORY_ROUTE_PATHS = frozenset(
    {_GENERIC_WEB_PREVIEW_INVENTORY_ROUTE.route_path, _ODOO_PREVIEW_INVENTORY_ROUTE.route_path}
)
_PREVIEW_REFRESH_ROUTE_PATHS = frozenset(
    {_GENERIC_WEB_PREVIEW_REFRESH_ROUTE.route_path, _ODOO_PREVIEW_REFRESH_ROUTE.route_path}
)
_PREVIEW_READINESS_ROUTE_PATHS = frozenset(
    {_GENERIC_WEB_PREVIEW_READINESS_ROUTE.route_path, _ODOO_PREVIEW_READINESS_ROUTE.route_path}
)
_PREVIEW_DESTROY_ROUTE_PATHS = frozenset(
    {_GENERIC_WEB_PREVIEW_DESTROY_ROUTE.route_path, _ODOO_PREVIEW_DESTROY_ROUTE.route_path}
)


class BackupGateEvidenceEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    product: str
    backup_gate: BackupGateRecord

    @model_validator(mode="after")
    def _validate_alignment(self) -> "BackupGateEvidenceEnvelope":
        if not self.product.strip():
            raise ValueError("backup gate evidence requires product")
        return self


class PreviewLifecyclePlanEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    product: str
    context: str
    desired_previews: tuple[PreviewLifecycleDesiredPreview, ...] = ()
    desired_state_id: str = ""
    source: str = "workflow"

    @model_validator(mode="after")
    def _validate_request(self) -> "PreviewLifecyclePlanEnvelope":
        if not self.product.strip():
            raise ValueError("preview lifecycle plan requires product")
        if not self.context.strip():
            raise ValueError("preview lifecycle plan requires context")
        if not self.source.strip():
            raise ValueError("preview lifecycle plan requires source")
        return self


class PreviewDesiredStateEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    product: str
    context: str
    source: str = "workflow"
    repository: str
    label: str = "preview"
    anchor_repo: str
    preview_slug_prefix: str = "pr-"
    max_pages: int = Field(default=10, ge=1, le=20)

    @model_validator(mode="after")
    def _validate_request(self) -> "PreviewDesiredStateEnvelope":
        if not self.product.strip():
            raise ValueError("preview desired state requires product")
        if not self.context.strip():
            raise ValueError("preview desired state requires context")
        if not self.source.strip():
            raise ValueError("preview desired state requires source")
        if not self.repository.strip():
            raise ValueError("preview desired state requires repository")
        if not self.label.strip():
            raise ValueError("preview desired state requires label")
        if not self.anchor_repo.strip():
            raise ValueError("preview desired state requires anchor_repo")
        if not self.preview_slug_prefix.strip():
            raise ValueError("preview desired state requires preview_slug_prefix")
        return self


class PreviewLifecycleCleanupEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    product: str
    context: str
    plan_id: str
    source: str = "workflow"
    apply: bool = False
    destroy_reason: str = "preview_lifecycle_cleanup"
    timeout_seconds: int = Field(default=300, ge=1)

    @model_validator(mode="after")
    def _validate_request(self) -> "PreviewLifecycleCleanupEnvelope":
        if not self.product.strip():
            raise ValueError("preview lifecycle cleanup requires product")
        if not self.context.strip():
            raise ValueError("preview lifecycle cleanup requires context")
        if not self.plan_id.strip():
            raise ValueError("preview lifecycle cleanup requires plan_id")
        if not self.source.strip():
            raise ValueError("preview lifecycle cleanup requires source")
        if not self.destroy_reason.strip():
            raise ValueError("preview lifecycle cleanup requires destroy_reason")
        return self


class PreviewPrFeedbackEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    product: str
    context: str
    source: str = "workflow"
    repository: str
    anchor_repo: str
    anchor_pr_number: int = Field(ge=1)
    anchor_pr_url: str
    status: PreviewPrFeedbackStatus
    marker: str = DEFAULT_PREVIEW_FEEDBACK_MARKER
    preview_url: str = ""
    immutable_image_reference: str = ""
    refresh_image_reference: str = ""
    revision: str = ""
    run_url: str = ""
    failure_summary: str = ""

    @model_validator(mode="after")
    def _validate_request(self) -> "PreviewPrFeedbackEnvelope":
        if not self.product.strip():
            raise ValueError("preview PR feedback requires product")
        if not self.context.strip():
            raise ValueError("preview PR feedback requires context")
        if not self.source.strip():
            raise ValueError("preview PR feedback requires source")
        if not self.repository.strip():
            raise ValueError("preview PR feedback requires repository")
        if not self.anchor_repo.strip():
            raise ValueError("preview PR feedback requires anchor_repo")
        if not self.anchor_pr_url.strip():
            raise ValueError("preview PR feedback requires anchor_pr_url")
        if not self.marker.strip():
            raise ValueError("preview PR feedback requires marker")
        return self


class PromotionEvidenceEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    product: str
    promotion: PromotionRecord

    @model_validator(mode="after")
    def _validate_alignment(self) -> "PromotionEvidenceEnvelope":
        if not self.product.strip():
            raise ValueError("promotion evidence requires product")
        return self


def _validate_driver_envelope_product(product: str, *, label: str) -> None:
    if not product.strip():
        raise ValueError(f"{label} requires product.")


class ProductDriverMismatchError(ValueError):
    pass


class OdooPostDeployEnvelope(_ProductRouteEnvelope):
    schema_version: int = Field(default=1, ge=1)
    post_deploy: OdooPostDeployRequest

    @model_validator(mode="after")
    def _validate_alignment(self) -> "OdooPostDeployEnvelope":
        _validate_driver_envelope_product(self.product, label="Odoo post-deploy")
        return self


class OdooArtifactPublishEnvelope(_ProductRouteEnvelope):
    schema_version: int = Field(default=1, ge=1)
    publish: OdooArtifactPublishEvidenceRequest

    @model_validator(mode="after")
    def _validate_alignment(self) -> "OdooArtifactPublishEnvelope":
        _validate_driver_envelope_product(self.product, label="Odoo artifact publish")
        return self


class OdooArtifactPublishInputsEnvelope(_ProductRouteEnvelope):
    schema_version: int = Field(default=1, ge=1)
    inputs: OdooArtifactPublishInputsRequest

    @model_validator(mode="after")
    def _validate_alignment(self) -> "OdooArtifactPublishInputsEnvelope":
        _validate_driver_envelope_product(self.product, label="Odoo artifact publish inputs")
        return self


_ODOO_POST_DEPLOY_ROUTE = _DriverRouteExecutionMetadata(
    route_path="/v1/drivers/odoo/post-deploy",
    envelope_model=OdooPostDeployEnvelope,
    denial_message=(
        "Workflow cannot execute the Odoo post-deploy driver for the requested product/context."
    ),
)


_ODOO_ARTIFACT_PUBLISH_ROUTE = _DriverRouteExecutionMetadata(
    route_path="/v1/drivers/odoo/artifact-publish",
    envelope_model=OdooArtifactPublishEnvelope,
    denial_message=(
        "Workflow cannot write Odoo artifact publish evidence for the requested product/context."
    ),
)


_ODOO_ARTIFACT_PUBLISH_INPUTS_ROUTE = _DriverRouteExecutionMetadata(
    route_path="/v1/drivers/odoo/artifact-publish-inputs",
    envelope_model=OdooArtifactPublishInputsEnvelope,
    denial_message=(
        "Workflow cannot read Odoo artifact publish inputs for the requested product/context."
    ),
)


class OdooProdRollbackEnvelope(_ProductRouteEnvelope):
    schema_version: int = Field(default=1, ge=1)
    rollback: OdooProdRollbackRequest

    @model_validator(mode="after")
    def _validate_alignment(self) -> "OdooProdRollbackEnvelope":
        _validate_driver_envelope_product(self.product, label="Odoo prod rollback")
        return self


class OdooProdBackupGateEnvelope(_ProductRouteEnvelope):
    schema_version: int = Field(default=1, ge=1)
    backup_gate: OdooProdBackupGateRequest

    @model_validator(mode="after")
    def _validate_alignment(self) -> "OdooProdBackupGateEnvelope":
        _validate_driver_envelope_product(self.product, label="Odoo prod backup gate")
        return self


class OdooProdPromotionEnvelope(_ProductRouteEnvelope):
    schema_version: int = Field(default=1, ge=1)
    promotion: OdooProdPromotionRequest

    @model_validator(mode="after")
    def _validate_alignment(self) -> "OdooProdPromotionEnvelope":
        _validate_driver_envelope_product(self.product, label="Odoo prod promotion")
        return self


_ODOO_PROD_BACKUP_GATE_ROUTE = _DriverRouteExecutionMetadata(
    route_path="/v1/drivers/odoo/prod-backup-gate",
    envelope_model=OdooProdBackupGateEnvelope,
    denial_message=(
        "Workflow cannot execute the Odoo prod backup-gate driver"
        " for the requested product/context."
    ),
)


_ODOO_PROD_PROMOTION_ROUTE = _DriverRouteExecutionMetadata(
    route_path="/v1/drivers/odoo/prod-promotion",
    envelope_model=OdooProdPromotionEnvelope,
    denial_message=(
        "Workflow cannot execute the Odoo prod promotion driver for the requested product/context."
    ),
)


_ODOO_PROD_ROLLBACK_ROUTE = _DriverRouteExecutionMetadata(
    route_path="/v1/drivers/odoo/prod-rollback",
    envelope_model=OdooProdRollbackEnvelope,
    denial_message=(
        "Workflow cannot execute the Odoo prod rollback driver for the requested product/context."
    ),
)


class VeriReelTestingDeployEnvelope(_ProductRouteEnvelope):
    schema_version: int = Field(default=1, ge=1)
    deploy: VeriReelStableDeployRequest

    @model_validator(mode="after")
    def _validate_alignment(self) -> "VeriReelTestingDeployEnvelope":
        _validate_driver_envelope_product(self.product, label="VeriReel testing deploy")
        if self.deploy.instance != "testing":
            raise ValueError("VeriReel testing deploy requires instance 'testing'.")
        return self


def _normalize_release_status(value: object, *, label: str) -> ReleaseStatus:
    normalized = str(value or "").strip().lower()
    if normalized in {"success", "passed", "pass"}:
        return "pass"
    if normalized in {"failure", "failed", "fail", "cancelled", "canceled", "timed_out"}:
        return "fail"
    if normalized in {"skipped", "not-run", "not_run", ""}:
        return "skipped"
    if normalized in {"pending", "in_progress", "in-progress"}:
        return "pending"
    raise ValueError(f"{label} must be pass, fail, skipped, or pending.")


class VeriReelTestingVerificationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    context: str = "verireel"
    instance: str = "testing"
    deployment_record_id: str
    migration_status: ReleaseStatus
    verification_status: ReleaseStatus
    owner_routes_status: ReleaseStatus

    @field_validator(
        "migration_status", "verification_status", "owner_routes_status", mode="before"
    )
    @classmethod
    def _normalize_status(cls, value: object) -> ReleaseStatus:
        return _normalize_release_status(value, label="Testing verification status")

    @model_validator(mode="after")
    def _validate_request(self) -> "VeriReelTestingVerificationRequest":
        if not self.context.strip():
            raise ValueError("VeriReel testing verification requires context.")
        if self.instance != "testing":
            raise ValueError("VeriReel testing verification requires instance 'testing'.")
        if not self.deployment_record_id.strip():
            raise ValueError("VeriReel testing verification requires deployment_record_id.")
        return self


class VeriReelTestingVerificationEnvelope(_ProductRouteEnvelope):
    schema_version: int = Field(default=1, ge=1)
    verification: VeriReelTestingVerificationRequest

    @model_validator(mode="after")
    def _validate_alignment(self) -> "VeriReelTestingVerificationEnvelope":
        _validate_driver_envelope_product(self.product, label="VeriReel testing verification")
        return self


class VeriReelProdDeployEnvelope(_ProductRouteEnvelope):
    schema_version: int = Field(default=1, ge=1)
    deploy: VeriReelStableDeployRequest

    @model_validator(mode="after")
    def _validate_alignment(self) -> "VeriReelProdDeployEnvelope":
        _validate_driver_envelope_product(self.product, label="VeriReel prod deploy")
        if self.deploy.instance != "prod":
            raise ValueError("VeriReel prod deploy requires instance 'prod'.")
        return self


class VeriReelAppMaintenanceEnvelope(_ProductRouteEnvelope):
    schema_version: int = Field(default=1, ge=1)
    maintenance: VeriReelAppMaintenanceRequest

    @model_validator(mode="after")
    def _validate_alignment(self) -> "VeriReelAppMaintenanceEnvelope":
        _validate_driver_envelope_product(self.product, label="VeriReel app maintenance")
        return self


class VeriReelStableEnvironmentEnvelope(_ProductRouteEnvelope):
    schema_version: int = Field(default=1, ge=1)
    environment: VeriReelStableEnvironmentRequest

    @model_validator(mode="after")
    def _validate_alignment(self) -> "VeriReelStableEnvironmentEnvelope":
        _validate_driver_envelope_product(self.product, label="VeriReel stable environment")
        return self


class VeriReelRuntimeVerificationEnvelope(_ProductRouteEnvelope):
    schema_version: int = Field(default=1, ge=1)
    verification: VeriReelRolloutVerificationRequest

    @model_validator(mode="after")
    def _validate_alignment(self) -> "VeriReelRuntimeVerificationEnvelope":
        _validate_driver_envelope_product(self.product, label="VeriReel runtime verification")
        return self


_VERIREEL_TESTING_DEPLOY_ROUTE = _DriverRouteExecutionMetadata(
    route_path="/v1/drivers/verireel/testing-deploy",
    envelope_model=VeriReelTestingDeployEnvelope,
    denial_message=(
        "Workflow cannot execute the VeriReel testing deploy driver"
        " for the requested product/context."
    ),
)


_VERIREEL_TESTING_VERIFICATION_ROUTE = _DriverRouteExecutionMetadata(
    route_path="/v1/drivers/verireel/testing-verification",
    envelope_model=VeriReelTestingVerificationEnvelope,
    denial_message=(
        "Workflow cannot write VeriReel testing verification for the requested product/context."
    ),
)


_VERIREEL_STABLE_ENVIRONMENT_ROUTE = _DriverRouteExecutionMetadata(
    route_path="/v1/drivers/verireel/stable-environment",
    envelope_model=VeriReelStableEnvironmentEnvelope,
    denial_message=(
        "Workflow cannot read the VeriReel stable environment for the requested product/context."
    ),
)


_VERIREEL_RUNTIME_VERIFICATION_ROUTE = _DriverRouteExecutionMetadata(
    route_path="/v1/drivers/verireel/runtime-verification",
    envelope_model=VeriReelRuntimeVerificationEnvelope,
    denial_message=(
        "Workflow cannot execute the VeriReel runtime verification driver"
        " for the requested product/context."
    ),
)


_VERIREEL_APP_MAINTENANCE_ROUTE = _DriverRouteExecutionMetadata(
    route_path="/v1/drivers/verireel/app-maintenance",
    envelope_model=VeriReelAppMaintenanceEnvelope,
    denial_message=(
        "Workflow cannot execute the VeriReel app maintenance driver"
        " for the requested product/context."
    ),
)


class VeriReelProdPromotionEnvelope(_ProductRouteEnvelope):
    schema_version: int = Field(default=1, ge=1)
    promotion: VeriReelProdPromotionRequest

    @model_validator(mode="after")
    def _validate_alignment(self) -> "VeriReelProdPromotionEnvelope":
        _validate_driver_envelope_product(self.product, label="VeriReel prod promotion")
        return self


class VeriReelProdBackupGateEnvelope(_ProductRouteEnvelope):
    schema_version: int = Field(default=1, ge=1)
    backup_gate: VeriReelProdBackupGateRequest

    @model_validator(mode="after")
    def _validate_alignment(self) -> "VeriReelProdBackupGateEnvelope":
        _validate_driver_envelope_product(self.product, label="VeriReel prod backup gate")
        return self


class VeriReelProdRollbackEnvelope(_ProductRouteEnvelope):
    schema_version: int = Field(default=1, ge=1)
    rollback: VeriReelProdRollbackRequest

    @model_validator(mode="after")
    def _validate_alignment(self) -> "VeriReelProdRollbackEnvelope":
        _validate_driver_envelope_product(self.product, label="VeriReel prod rollback")
        return self


_VERIREEL_PROD_DEPLOY_ROUTE = _DriverRouteExecutionMetadata(
    route_path="/v1/drivers/verireel/prod-deploy",
    envelope_model=VeriReelProdDeployEnvelope,
    denial_message=(
        "Workflow cannot execute the VeriReel prod deploy driver for the requested product/context."
    ),
)


_VERIREEL_PROD_BACKUP_GATE_ROUTE = _DriverRouteExecutionMetadata(
    route_path="/v1/drivers/verireel/prod-backup-gate",
    envelope_model=VeriReelProdBackupGateEnvelope,
    denial_message=(
        "Workflow cannot execute the VeriReel prod backup gate driver"
        " for the requested product/context."
    ),
)


_VERIREEL_PROD_PROMOTION_ROUTE = _DriverRouteExecutionMetadata(
    route_path="/v1/drivers/verireel/prod-promotion",
    envelope_model=VeriReelProdPromotionEnvelope,
    denial_message=(
        "Workflow cannot execute the VeriReel prod promotion driver"
        " for the requested product/context."
    ),
)


_VERIREEL_PROD_ROLLBACK_ROUTE = _DriverRouteExecutionMetadata(
    route_path="/v1/drivers/verireel/prod-rollback",
    envelope_model=VeriReelProdRollbackEnvelope,
    denial_message=(
        "Workflow cannot execute the VeriReel prod rollback driver"
        " for the requested product/context."
    ),
)


class VeriReelPreviewRefreshEnvelope(_ProductRouteEnvelope):
    schema_version: int = Field(default=1, ge=1)
    refresh: VeriReelPreviewRefreshRequest

    @model_validator(mode="after")
    def _validate_alignment(self) -> "VeriReelPreviewRefreshEnvelope":
        _validate_driver_envelope_product(self.product, label="VeriReel preview refresh")
        return self


class VeriReelPreviewDestroyEnvelope(_ProductRouteEnvelope):
    schema_version: int = Field(default=1, ge=1)
    destroy: VeriReelPreviewDestroyRequest

    @model_validator(mode="after")
    def _validate_alignment(self) -> "VeriReelPreviewDestroyEnvelope":
        _validate_driver_envelope_product(self.product, label="VeriReel preview destroy")
        return self


class VeriReelPreviewVerificationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    context: str = "verireel-testing"
    anchor_repo: str = "verireel"
    anchor_pr_number: int = Field(ge=1)
    verification_status: str
    verified_at: str
    failure_summary: str = ""

    @model_validator(mode="after")
    def _validate_request(self) -> "VeriReelPreviewVerificationRequest":
        if not self.context.strip():
            raise ValueError("VeriReel preview verification requires context.")
        if not self.anchor_repo.strip():
            raise ValueError("VeriReel preview verification requires anchor_repo.")
        if self.verification_status.strip() not in {"pass", "fail"}:
            raise ValueError("VeriReel preview verification status must be 'pass' or 'fail'.")
        if not self.verified_at.strip():
            raise ValueError("VeriReel preview verification requires verified_at.")
        return self


class VeriReelPreviewVerificationEnvelope(_ProductRouteEnvelope):
    schema_version: int = Field(default=1, ge=1)
    verification: VeriReelPreviewVerificationRequest

    @model_validator(mode="after")
    def _validate_alignment(self) -> "VeriReelPreviewVerificationEnvelope":
        _validate_driver_envelope_product(self.product, label="VeriReel preview verification")
        return self


class VeriReelPreviewInventoryEnvelope(_ProductRouteEnvelope):
    schema_version: int = Field(default=1, ge=1)
    inventory: VeriReelPreviewInventoryRequest

    @model_validator(mode="after")
    def _validate_alignment(self) -> "VeriReelPreviewInventoryEnvelope":
        _validate_driver_envelope_product(self.product, label="VeriReel preview inventory")
        return self


_VERIREEL_PREVIEW_REFRESH_ROUTE = _DriverRouteExecutionMetadata(
    route_path="/v1/drivers/verireel/preview-refresh",
    envelope_model=VeriReelPreviewRefreshEnvelope,
    denial_message=(
        "Workflow cannot execute the VeriReel preview refresh driver"
        " for the requested product/context."
    ),
)


_VERIREEL_PREVIEW_INVENTORY_ROUTE = _DriverRouteExecutionMetadata(
    route_path="/v1/drivers/verireel/preview-inventory",
    envelope_model=VeriReelPreviewInventoryEnvelope,
    denial_message=(
        "Workflow cannot read the VeriReel preview inventory for the requested product/context."
    ),
)


_VERIREEL_PREVIEW_DESTROY_ROUTE = _DriverRouteExecutionMetadata(
    route_path="/v1/drivers/verireel/preview-destroy",
    envelope_model=VeriReelPreviewDestroyEnvelope,
    denial_message=(
        "Workflow cannot execute the VeriReel preview destroy driver"
        " for the requested product/context."
    ),
)


_VERIREEL_PREVIEW_VERIFICATION_ROUTE = _DriverRouteExecutionMetadata(
    route_path="/v1/drivers/verireel/preview-verification",
    envelope_model=VeriReelPreviewVerificationEnvelope,
    denial_message=(
        "Workflow cannot write VeriReel preview verification for the requested product/context."
    ),
)


_HUMAN_IDENTITY_MUTATION_ROUTES = frozenset(
    {
        "/v1/agent/write-intents/evaluate",
        _GENERIC_WEB_PROD_PROMOTION_ROUTE.route_path,
        _GENERIC_WEB_PROD_PROMOTION_WORKFLOW_ROUTE.route_path,
        "/v1/authz-policies/github-actions/grants",
        "/v1/authz-policies/github-humans/grants",
        "/v1/authz-policies/terminal-agents/grants",
    }
)
_HUMAN_IDENTITY_READ_MODEL_POST_ROUTES = frozenset({"/v1/work-graph/rank"})
_NON_IDEMPOTENT_DRIVER_RESULT_ROUTES = frozenset(
    {
        _VERIREEL_STABLE_ENVIRONMENT_ROUTE.route_path,
        _VERIREEL_RUNTIME_VERIFICATION_ROUTE.route_path,
        _VERIREEL_PREVIEW_INVENTORY_ROUTE.route_path,
    }
)
_PENDING_RESULT_IDEMPOTENCY_SKIP_ROUTES = frozenset({_VERIREEL_PROD_BACKUP_GATE_ROUTE.route_path})


class LaunchplaneSelfDeployRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_type: str
    target_id: str
    image_reference: str
    policy_b64: str = ""
    oauth_env: dict[str, str] = Field(default_factory=dict)
    no_cache: bool = False

    @model_validator(mode="after")
    def _validate_values(self) -> "LaunchplaneSelfDeployRequest":
        normalized_target_type = self.target_type.strip()
        if normalized_target_type not in {"compose", "application"}:
            raise ValueError(
                "Launchplane self deploy requires target_type 'compose' or 'application'."
            )
        if not self.target_id.strip():
            raise ValueError("Launchplane self deploy requires target_id.")
        if not self.image_reference.strip():
            raise ValueError("Launchplane self deploy requires image_reference.")
        normalized_policy_b64 = self.policy_b64.strip()
        if normalized_policy_b64:
            try:
                policy_text = base64.b64decode(normalized_policy_b64, validate=True).decode("utf-8")
            except Exception as error:
                raise ValueError(
                    "Launchplane self deploy requires valid base64 policy_b64."
                ) from error
            parse_authz_policy_toml(policy_text)
        self.target_type = normalized_target_type
        self.target_id = self.target_id.strip()
        self.image_reference = self.image_reference.strip()
        self.policy_b64 = normalized_policy_b64
        normalized_oauth_env: dict[str, str] = {}
        for env_key, raw_value in self.oauth_env.items():
            normalized_key = env_key.strip()
            if normalized_key not in _LAUNCHPLANE_SELF_DEPLOY_OAUTH_ENV_KEYS:
                raise ValueError(
                    f"Launchplane self deploy does not accept oauth_env key {normalized_key!r}."
                )
            normalized_value = raw_value.strip()
            if "\n" in normalized_value or "\r" in normalized_value:
                raise ValueError(
                    f"Launchplane self deploy oauth_env key {normalized_key!r} must be a single line."
                )
            if normalized_value:
                normalized_oauth_env[normalized_key] = normalized_value
        self.oauth_env = normalized_oauth_env
        return self


class LaunchplaneSelfDeployEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    product: str
    deploy: LaunchplaneSelfDeployRequest

    @model_validator(mode="after")
    def _validate_alignment(self) -> "LaunchplaneSelfDeployEnvelope":
        if self.product.strip() != "launchplane":
            raise ValueError("Launchplane self deploy requires product 'launchplane'.")
        return self


class EveryCodeWorkRequestCreateEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repository: str
    issue_number: int = Field(ge=1)
    issue_url: str
    issue_title: str = ""
    trigger_label: str = "every-code"
    trigger_actor: str = ""
    github_delivery_id: str = ""
    source: Literal["github_issue_label", "manual", "reconciliation"] = "manual"
    queued_at: str = ""


class EveryCodeWorkRequestClaimEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str
    host: str

    @model_validator(mode="after")
    def _validate_claim(self) -> "EveryCodeWorkRequestClaimEnvelope":
        if not self.request_id.strip():
            raise ValueError("Every Code work request claim requires request_id")
        if not self.host.strip():
            raise ValueError("Every Code work request claim requires host")
        return self


class EveryCodeWorkRequestStatusEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str
    host: str
    state: Literal["running", "done", "blocked"]
    result_pr_url: str = ""
    result_summary: str = ""
    error_message: str = ""
    updated_at: str = ""


class EveryCodeWorkRequestRerunEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str
    trigger_actor: str = ""

    @model_validator(mode="after")
    def _validate_rerun(self) -> "EveryCodeWorkRequestRerunEnvelope":
        if not self.request_id.strip():
            raise ValueError("Every Code work request rerun requires request_id")
        return self


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


class EveryCodePreviewGateEnvelope(EveryCodePreviewGateRecord):
    pass


class ProductOnboardingApplyEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    product: str
    manifest: ProductOnboardingManifest

    @model_validator(mode="after")
    def _validate_alignment(self) -> "ProductOnboardingApplyEnvelope":
        if self.product.strip() != "launchplane":
            raise ValueError("Product onboarding writes require product 'launchplane'.")
        self.product = "launchplane"
        return self


class LiveTargetRuntimeApplyEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    mode: str
    product: str
    context: str
    instance: str
    deploy: bool = False
    no_cache: bool = False
    deploy_timeout_seconds: int | None = Field(default=None, gt=0)

    @field_validator("mode")
    @classmethod
    def _validate_mode(cls, value: str) -> str:
        normalized_value = value.strip().lower()
        if normalized_value not in {"dry-run", "apply"}:
            raise ValueError("Live target runtime mode must be 'dry-run' or 'apply'.")
        return normalized_value

    @model_validator(mode="after")
    def _validate_route(self) -> "LiveTargetRuntimeApplyEnvelope":
        self.product = self.product.strip()
        self.context = self.context.strip()
        self.instance = self.instance.strip()
        if not self.product:
            raise ValueError("Live target runtime apply requires product.")
        if not self.context:
            raise ValueError("Live target runtime apply requires context.")
        if not self.instance:
            raise ValueError("Live target runtime apply requires instance.")
        if self.mode == "dry-run" and (
            self.deploy or self.no_cache or self.deploy_timeout_seconds is not None
        ):
            raise ValueError("Deploy options require live target runtime mode 'apply'.")
        return self

    @property
    def apply_changes(self) -> bool:
        return self.mode == "apply"


class ThreadingWSGIServer(ThreadingMixIn, WSGIServer):
    daemon_threads = True


def _json_response(
    *,
    start_response: _StartResponse,
    status_code: int,
    payload: dict[str, object],
    headers: list[tuple[str, str]] | None = None,
) -> list[bytes]:
    encoded = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
    status_line = f"{status_code} {_http_status_text(status_code)}"
    response_headers = [
        ("Content-Type", "application/json"),
        ("Content-Length", str(len(encoded))),
    ]
    response_headers.extend(headers or [])
    start_response(status_line, response_headers)
    return [encoded]


def _redirect_response(
    *,
    start_response: _StartResponse,
    location: str,
    headers: list[tuple[str, str]] | None = None,
) -> list[bytes]:
    body = b""
    response_headers = [("Location", location), ("Content-Length", "0")]
    response_headers.extend(headers or [])
    start_response("302 Found", response_headers)
    return [body]


def _driver_route_authorization_response(
    *,
    authz_policy: LaunchplaneAuthzPolicy,
    identity: LaunchplaneIdentity,
    route_path: str,
    product: str,
    context: str,
    denial_message: str,
    start_response: _StartResponse,
    trace_id: str,
) -> list[bytes] | None:
    """Authorize descriptor routes against normalized product/context values."""

    normalized_product = product.strip()
    normalized_context = context.strip()
    if authz_policy.allows(
        identity=identity,
        action=_descriptor_driver_authz_action(route_path),
        product=normalized_product,
        context=normalized_context,
    ):
        return None
    return _json_response(
        start_response=start_response,
        status_code=403,
        payload={
            "status": "rejected",
            "trace_id": trace_id,
            "error": {
                "code": "authorization_denied",
                "message": denial_message,
            },
        },
    )


def _resolve_and_authorize_descriptor_route(
    *,
    route_metadata: _DriverRouteExecutionMetadata[_DriverRouteEnvelopeT],
    record_store: object,
    authz_policy: LaunchplaneAuthzPolicy,
    identity: LaunchplaneIdentity,
    product: str,
    authorization_context: str,
    start_response: _StartResponse,
    trace_id: str,
    descriptor_context: str = "",
    descriptor_instance: str = "",
    require_profile: bool = False,
) -> tuple[_ResolvedProductDriverContext, list[bytes] | None]:
    resolved_driver_context = _resolve_descriptor_product_driver_context(
        record_store=record_store,
        route_path=route_metadata.route_path,
        product=product,
        context=descriptor_context,
        instance=descriptor_instance,
        require_profile=require_profile,
    )
    authorization_response = _driver_route_authorization_response(
        authz_policy=authz_policy,
        identity=identity,
        route_path=route_metadata.route_path,
        product=product,
        context=authorization_context,
        denial_message=route_metadata.denial_message,
        start_response=start_response,
        trace_id=trace_id,
    )
    return resolved_driver_context, authorization_response


def _authorize_generic_web_preview_route(
    *,
    route_metadata: _DriverRouteExecutionMetadata[_DriverRouteEnvelopeT],
    payload: dict[str, object],
    record_store: GenericWebPreviewProfileStore,
    authz_policy: LaunchplaneAuthzPolicy,
    identity: LaunchplaneIdentity,
    start_response: _StartResponse,
    trace_id: str,
) -> tuple[_DriverRouteEnvelopeT, LaunchplaneProductProfileRecord, list[bytes] | None]:
    request = route_metadata.envelope_model.model_validate(payload)
    profile = resolve_generic_web_preview_profile(
        record_store=record_store,
        product=request.product,
    )
    authorization_response = _driver_route_authorization_response(
        authz_policy=authz_policy,
        identity=identity,
        route_path=route_metadata.route_path,
        product=profile.product,
        context=profile.preview.context,
        denial_message=route_metadata.denial_message,
        start_response=start_response,
        trace_id=trace_id,
    )
    return request, profile, authorization_response


def _generic_web_preview_desired_state_route_metadata(
    path: str,
) -> _DriverRouteExecutionMetadata[GenericWebPreviewDesiredStateEnvelope]:
    if path == _ODOO_PREVIEW_DESIRED_STATE_ROUTE.route_path:
        return _ODOO_PREVIEW_DESIRED_STATE_ROUTE
    return _GENERIC_WEB_PREVIEW_DESIRED_STATE_ROUTE


def _generic_web_preview_inventory_route_metadata(
    path: str,
) -> _DriverRouteExecutionMetadata[GenericWebPreviewInventoryEnvelope]:
    if path == _ODOO_PREVIEW_INVENTORY_ROUTE.route_path:
        return _ODOO_PREVIEW_INVENTORY_ROUTE
    return _GENERIC_WEB_PREVIEW_INVENTORY_ROUTE


def _generic_web_preview_refresh_route_metadata(
    path: str,
) -> _DriverRouteExecutionMetadata[GenericWebPreviewRefreshEnvelope]:
    if path == _ODOO_PREVIEW_REFRESH_ROUTE.route_path:
        return _ODOO_PREVIEW_REFRESH_ROUTE
    return _GENERIC_WEB_PREVIEW_REFRESH_ROUTE


def _generic_web_preview_readiness_route_metadata(
    path: str,
) -> _DriverRouteExecutionMetadata[GenericWebPreviewReadinessEnvelope]:
    if path == _ODOO_PREVIEW_READINESS_ROUTE.route_path:
        return _ODOO_PREVIEW_READINESS_ROUTE
    return _GENERIC_WEB_PREVIEW_READINESS_ROUTE


def _generic_web_preview_destroy_route_metadata(
    path: str,
) -> _DriverRouteExecutionMetadata[GenericWebPreviewDestroyEnvelope]:
    if path == _ODOO_PREVIEW_DESTROY_ROUTE.route_path:
        return _ODOO_PREVIEW_DESTROY_ROUTE
    return _GENERIC_WEB_PREVIEW_DESTROY_ROUTE


def _http_status_text(status_code: int) -> str:
    return {
        200: "OK",
        202: "Accepted",
        400: "Bad Request",
        401: "Unauthorized",
        403: "Forbidden",
        404: "Not Found",
        405: "Method Not Allowed",
        409: "Conflict",
        500: "Internal Server Error",
    }.get(status_code, "OK")


def _trace_id() -> str:
    return f"launchplane_req_{uuid.uuid4().hex}"


def _utc_now_timestamp() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _not_found_response(
    *,
    start_response: _StartResponse,
    trace_id: str,
    path: str,
) -> list[bytes]:
    return _json_response(
        start_response=start_response,
        status_code=404,
        payload={
            "status": "rejected",
            "trace_id": trace_id,
            "error": {"code": "not_found", "message": f"No Launchplane route for {path}."},
        },
    )


def _ui_static_response(
    *,
    start_response: _StartResponse,
    status_code: int,
    content: bytes,
    content_type: str,
    cache_control: str,
) -> list[bytes]:
    status_line = f"{status_code} {_http_status_text(status_code)}"
    start_response(
        status_line,
        [
            ("Content-Type", content_type),
            ("Content-Length", str(len(content))),
            ("Cache-Control", cache_control),
        ],
    )
    return [content]


def _ui_file_response(
    *,
    start_response: _StartResponse,
    file_path: Path,
    cache_control: str,
) -> list[bytes]:
    content_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
    return _ui_static_response(
        start_response=start_response,
        status_code=200,
        content=file_path.read_bytes(),
        content_type=content_type,
        cache_control=cache_control,
    )


def _serve_ui_route(
    *,
    start_response: _StartResponse,
    trace_id: str,
    path: str,
    ui_static_root: Path,
) -> list[bytes]:
    index_path = ui_static_root / "index.html"
    if not index_path.is_file():
        return _not_found_response(start_response=start_response, trace_id=trace_id, path=path)

    if path in {"/", "/ui", "/ui/"}:
        return _ui_file_response(
            start_response=start_response,
            file_path=index_path,
            cache_control="no-store",
        )

    if path.startswith("/ui/assets/"):
        relative_asset_path = unquote(path.removeprefix("/ui/"))
        if ".." in Path(relative_asset_path).parts:
            return _not_found_response(start_response=start_response, trace_id=trace_id, path=path)
        asset_path = (ui_static_root / relative_asset_path).resolve()
        try:
            asset_path.relative_to(ui_static_root.resolve())
        except ValueError:
            return _not_found_response(start_response=start_response, trace_id=trace_id, path=path)
        if not asset_path.is_file():
            return _not_found_response(start_response=start_response, trace_id=trace_id, path=path)
        return _ui_file_response(
            start_response=start_response,
            file_path=asset_path,
            cache_control="public, max-age=31536000, immutable",
        )

    if path.startswith("/ui/"):
        return _ui_file_response(
            start_response=start_response,
            file_path=index_path,
            cache_control="no-store",
        )

    return _not_found_response(start_response=start_response, trace_id=trace_id, path=path)


def _match_read_route(path: str) -> tuple[str, dict[str, str]] | None:
    segments = [segment for segment in path.split("/") if segment]
    if len(segments) == 3 and segments == ["v1", "previews", "readiness"]:
        return "every_code_preview_gate.read", {"readiness": "true"}
    if len(segments) == 3 and segments == ["v1", "every-code", "summary"]:
        return "every_code_work_request.read", {"summary": "true"}
    if len(segments) == 3 and segments == ["v1", "every-code", "work-requests"]:
        return "every_code_work_request.read", {}
    if len(segments) == 3 and segments == ["v1", "every-code", "pr-feedback"]:
        return "every_code_pr_feedback.read", {}
    if len(segments) == 3 and segments == ["v1", "every-code", "preview-gates"]:
        return "every_code_preview_gate.read", {}
    if len(segments) == 3 and segments == ["v1", "agent", "context"]:
        return "product_environment.read", {"agent_context": "true"}
    if len(segments) == 4 and segments[:3] == ["v1", "every-code", "work-requests"]:
        return "every_code_work_request.read", {"request_id": segments[3]}
    if len(segments) == 2 and segments == ["v1", "drivers"]:
        return "driver.read", {}
    if len(segments) == 3 and segments[:2] == ["v1", "drivers"]:
        return "driver.read", {"driver_id": segments[2]}
    if len(segments) == 4 and segments[:2] == ["v1", "contexts"] and segments[3] == "driver-view":
        return "driver.read", {"context": segments[2]}
    if (
        len(segments) == 6
        and segments[:2] == ["v1", "contexts"]
        and segments[3] == "instances"
        and segments[5] == "driver-view"
    ):
        return "driver.read", {"context": segments[2], "instance": segments[4]}
    if len(segments) == 3 and segments[:2] == ["v1", "deployments"]:
        return "deployment.read", {"record_id": segments[2]}
    if len(segments) == 3 and segments[:2] == ["v1", "promotions"]:
        return "promotion.read", {"record_id": segments[2]}
    if len(segments) == 4 and segments[:2] == ["v1", "inventory"]:
        return "inventory.read", {"context": segments[2], "instance": segments[3]}
    if len(segments) == 3 and segments[:2] == ["v1", "previews"]:
        return "preview.read", {"preview_id": segments[2]}
    if len(segments) == 4 and segments[:2] == ["v1", "previews"] and segments[3] == "history":
        return "preview.read", {"preview_id": segments[2], "include_history": "true"}
    if len(segments) == 3 and segments[:2] == ["v1", "secrets"]:
        return "secret.read", {"secret_id": segments[2]}
    if len(segments) == 4 and segments[:2] == ["v1", "contexts"] and segments[3] == "secrets":
        return "secret.list", {"context": segments[2]}
    if (
        len(segments) == 6
        and segments[:2] == ["v1", "contexts"]
        and segments[3] == "instances"
        and segments[5] == "secrets"
    ):
        return "secret.list", {"context": segments[2], "instance": segments[4]}
    if (
        len(segments) == 6
        and segments[:2] == ["v1", "contexts"]
        and segments[3] == "instances"
        and segments[5] == "logs"
    ):
        return "target_logs.read", {"context": segments[2], "instance": segments[4]}
    if (
        len(segments) == 5
        and segments[:2] == ["v1", "contexts"]
        and segments[3:] == ["operations", "recent"]
    ):
        return "operations.read", {"context": segments[2]}
    if len(segments) == 3 and segments == ["v1", "service", "runtime"]:
        return "launchplane_service.read", {}
    if path == _MERGE_TRAIN_ADMISSION_ROUTE:
        return "merge_train.admission", {}
    if len(segments) == 3 and segments == ["v1", "work-graph", "snapshot"]:
        return "work_graph.rank", {}
    if len(segments) == 2 and segments == ["v1", "repo-product-mapping"]:
        return "product_environment.read", {"repo_product_mapping": "true"}
    if len(segments) == 2 and segments == ["v1", "product-profiles"]:
        return "product_profile.read", {}
    if (
        len(segments) == 4
        and segments[:2] == ["v1", "product-profiles"]
        and segments[3] == "context-cutover-audit"
    ):
        return "product_profile.read", {"product": segments[2], "context_cutover_audit": "true"}
    if len(segments) == 3 and segments[:2] == ["v1", "product-profiles"]:
        return "product_profile.read", {"product": segments[2]}
    if len(segments) == 2 and segments == ["v1", "products"]:
        return "product_environment.read", {}
    if len(segments) == 4 and segments[:2] == ["v1", "products"] and segments[3] == "activity":
        return "product_environment.read", {"product": segments[2], "activity": "true"}
    if len(segments) == 3 and segments[:2] == ["v1", "products"]:
        return "product_environment.read", {"product": segments[2]}
    if len(segments) == 5 and segments[:2] == ["v1", "products"] and segments[3] == "environments":
        return "product_environment.read", {"product": segments[2], "environment": segments[4]}
    if (
        len(segments) == 6
        and segments[:2] == ["v1", "products"]
        and segments[3] == "environments"
        and segments[5] == "config-status"
    ):
        return "product_environment.read", {
            "product": segments[2],
            "environment": segments[4],
            "config_status": "true",
        }
    return None


def _driver_route_metadata_from_descriptors() -> dict[str, _DriverRouteMetadata]:
    route_metadata: dict[str, _DriverRouteMetadata] = {}
    for descriptor in list_driver_descriptors():
        for action in descriptor.actions:
            if not action.route_path.startswith("/v1/drivers/"):
                continue
            if not action.authz_action:
                raise ValueError(
                    f"Driver action {descriptor.driver_id}.{action.action_id} "
                    "must declare authz_action."
                )
            if action.route_path in route_metadata:
                raise ValueError(f"Duplicate driver action route path: {action.route_path}")
            route_metadata[action.route_path] = _DriverRouteMetadata(
                driver_id=descriptor.driver_id,
                action_id=action.action_id,
                method=action.method,
                authz_action=action.authz_action,
                operator_visible=action.operator_visible,
            )
    return route_metadata


def _descriptor_driver_authz_action(route_path: str) -> str:
    try:
        return _driver_route_metadata_from_descriptors()[route_path].authz_action
    except KeyError as exc:
        raise ValueError(f"Unknown descriptor-backed driver route: {route_path}") from exc


def _driver_write_routes_from_descriptors() -> frozenset[str]:
    return frozenset(
        route_path
        for route_path, route_metadata in _driver_route_metadata_from_descriptors().items()
        if route_metadata.method == "POST"
    )


def _build_write_routes() -> frozenset[str]:
    launchplane_write_routes = {
        _EVERY_CODE_GITHUB_WEBHOOK_ROUTE,
        _MERGE_TRAIN_RUN_ONCE_ROUTE,
        "/v1/agent/write-intents/evaluate",
        "/v1/every-code/work-requests/create",
        "/v1/every-code/work-requests/claim",
        "/v1/every-code/work-requests/rerun",
        "/v1/every-code/work-requests/status",
        "/v1/every-code/pr-feedback/status",
        "/v1/every-code/preview-gates",
        "/v1/work-graph/rank",
        "/v1/evidence/deployments",
        "/v1/evidence/backup-gates",
        "/v1/evidence/previews/generations",
        "/v1/evidence/previews/destroyed",
        "/v1/authz-policies/github-actions/grants",
        "/v1/authz-policies/github-humans/grants",
        "/v1/authz-policies/terminal-agents/grants",
        "/v1/runtime-key-safety/policies/apply",
        "/v1/live-target-runtime/apply",
        "/v1/product-onboarding/apply",
        "/v1/product-config/apply",
        "/v1/product-profiles/context-cutover/apply",
        "/v1/product-profiles/legacy-context-cleanup/apply",
        "/v1/previews/desired-state",
        "/v1/previews/pr-feedback",
        "/v1/previews/lifecycle-cleanup",
        "/v1/previews/lifecycle-plan",
        "/v1/product-profiles",
        "/v1/evidence/promotions",
        "/v1/drivers/launchplane/self-deploy",
    }
    return frozenset(launchplane_write_routes | set(_driver_write_routes_from_descriptors()))


def _secret_capable_store(record_store: object) -> control_plane_secrets.SecretReadStore | None:
    if hasattr(record_store, "read_secret_record") and hasattr(record_store, "list_secret_records"):
        return cast(control_plane_secrets.SecretReadStore, record_store)
    return None


class _EveryCodeWorkRequestStore(Protocol):
    def write_every_code_work_request_record(
        self, record: EveryCodeWorkRequestRecord
    ) -> object: ...

    def create_every_code_work_request_record_if_absent(
        self, record: EveryCodeWorkRequestRecord
    ) -> tuple[EveryCodeWorkRequestRecord, bool]: ...

    def read_every_code_work_request_record(
        self, request_id: str
    ) -> EveryCodeWorkRequestRecord: ...

    def list_every_code_work_request_records(
        self,
        *,
        state: str = "",
        repository: str = "",
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[EveryCodeWorkRequestRecord, ...]: ...

    def claim_every_code_work_request_record(
        self,
        *,
        request_id: str,
        host: str,
        claimed_at: str,
    ) -> EveryCodeWorkRequestRecord | None: ...

    def write_every_code_pr_feedback_record(self, record: EveryCodePrFeedbackRecord) -> object: ...

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

    def write_every_code_preview_gate_record(self, record: EveryCodePreviewGateRecord) -> object: ...

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


class _AgentWriteIntentRecordStore(Protocol):
    def write_agent_write_intent_record(self, record: AgentWriteIntentRecord) -> object: ...


def _every_code_work_request_store(record_store: object) -> _EveryCodeWorkRequestStore:
    required_methods = (
        "write_every_code_work_request_record",
        "create_every_code_work_request_record_if_absent",
        "read_every_code_work_request_record",
        "list_every_code_work_request_records",
        "claim_every_code_work_request_record",
        "write_every_code_pr_feedback_record",
        "list_every_code_pr_feedback_records",
        "write_every_code_preview_gate_record",
        "list_every_code_preview_gate_records",
    )
    if all(hasattr(record_store, method_name) for method_name in required_methods):
        return cast(_EveryCodeWorkRequestStore, record_store)
    raise TypeError("record store does not support Every Code work requests")


def _agent_write_intent_record_store(record_store: object) -> _AgentWriteIntentRecordStore:
    if hasattr(record_store, "write_agent_write_intent_record"):
        return cast(_AgentWriteIntentRecordStore, record_store)
    raise TypeError("record store does not support agent write intent records")


def _supports_every_code_work_requests(record_store: object) -> bool:
    return hasattr(record_store, "list_every_code_work_request_records")


def _github_webhook_header(environ: dict[str, object], name: str) -> str:
    return str(environ.get(f"HTTP_{name.upper().replace('-', '_')}", "")).strip()


def _github_webhook_mapping(payload: dict[str, object], key: str) -> dict[str, object] | None:
    value = payload.get(key)
    if isinstance(value, dict):
        return cast(dict[str, object], value)
    return None


def _github_webhook_required_mapping(payload: dict[str, object], key: str) -> dict[str, object]:
    value = _github_webhook_mapping(payload, key)
    if value is None:
        raise ValueError(f"GitHub webhook requires object field {key!r}")
    return value


def _github_webhook_string(mapping: dict[str, object] | None, key: str) -> str:
    if mapping is None:
        return ""
    value = mapping.get(key)
    return value.strip() if isinstance(value, str) else ""


def _github_login_normalized(login: str) -> str:
    return login.strip().lstrip("@").casefold()


def _github_actor_login(payload: dict[str, object]) -> str:
    return _github_webhook_string(_github_webhook_mapping(payload, "sender"), "login")


def _every_code_trusted_manager_logins(repository: str) -> frozenset[str]:
    normalized_repository = repository.strip().casefold()
    if not normalized_repository:
        return frozenset()
    config_paths = (
        Path.home() / ".code" / "github-planning.json",
        Path.home() / ".codex" / "github-planning.json",
    )
    for config_path in config_paths:
        try:
            payload = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        workflow = payload.get("workflow")
        if not isinstance(workflow, dict):
            continue
        managers: set[str] = set()
        default_manager = workflow.get("default_manager")
        if isinstance(default_manager, str) and default_manager.strip():
            managers.add(_github_login_normalized(default_manager))
        repo_managers = workflow.get("repo_managers")
        if isinstance(repo_managers, dict):
            repo_manager = repo_managers.get(repository) or repo_managers.get(normalized_repository)
            if isinstance(repo_manager, str) and repo_manager.strip():
                managers.add(_github_login_normalized(repo_manager))
        return frozenset(manager for manager in managers if manager)
    return frozenset()


def _every_code_feedback_actor_is_trusted(
    *,
    repository: str,
    actor: str,
    source_issue_author: str = "",
) -> bool:
    normalized_actor = _github_login_normalized(actor)
    if not normalized_actor:
        return False
    repository_owner = repository.strip().split("/", 1)[0]
    trusted = {_github_login_normalized(repository_owner)}
    if source_issue_author.strip():
        trusted.add(_github_login_normalized(source_issue_author))
    trusted.update(_every_code_trusted_manager_logins(repository))
    return normalized_actor in trusted


def _every_code_untrusted_feedback_response(
    *,
    start_response: _StartResponse,
    trace_id: str,
    delivery_id: str,
) -> list[bytes]:
    return _json_response(
        start_response=start_response,
        status_code=202,
        payload={
            "status": "accepted",
            "trace_id": trace_id,
            "skipped": True,
            "reason": "untrusted_actor",
            "github_delivery_id": delivery_id,
        },
    )


def _handle_every_code_github_webhook(
    *,
    environ: dict[str, object],
    start_response: _StartResponse,
    trace_id: str,
    record_store: object,
    control_plane_root_path: Path,
) -> list[bytes]:
    secret = os.environ.get(_EVERY_CODE_GITHUB_WEBHOOK_SECRET_ENV_KEY, "").strip()
    if not secret:
        return _json_response(
            start_response=start_response,
            status_code=503,
            payload={
                "status": "rejected",
                "trace_id": trace_id,
                "error": {
                    "code": "webhook_secret_not_configured",
                    "message": "Every Code GitHub webhook secret is not configured.",
                },
            },
        )

    body_bytes = _read_request_body(environ)
    try:
        verify_github_webhook_signature(
            payload_bytes=body_bytes,
            signature_header=_github_webhook_header(environ, "X-Hub-Signature-256"),
            secret=secret,
        )
    except click.ClickException:
        return _json_response(
            start_response=start_response,
            status_code=401,
            payload={
                "status": "rejected",
                "trace_id": trace_id,
                "error": {
                    "code": "webhook_signature_invalid",
                    "message": "GitHub webhook signature verification failed.",
                },
            },
        )

    delivery_id = _github_webhook_header(environ, "X-GitHub-Delivery")
    if not delivery_id:
        return _json_response(
            start_response=start_response,
            status_code=400,
            payload={
                "status": "rejected",
                "trace_id": trace_id,
                "error": {
                    "code": "github_delivery_required",
                    "message": "GitHub webhook delivery id is required.",
                },
            },
        )

    payload = _decode_json_request_body(body_bytes)
    event_name = _github_webhook_header(environ, "X-GitHub-Event")
    if event_name == "issue_comment":
        preview_validation_response = _handle_every_code_preview_validation_webhook(
            start_response=start_response,
            trace_id=trace_id,
            delivery_id=delivery_id,
            payload=payload,
            record_store=record_store,
            control_plane_root_path=control_plane_root_path,
        )
        if preview_validation_response is not None:
            return preview_validation_response
    if event_name in {"issue_comment", "pull_request_review", "pull_request_review_comment"}:
        return _handle_every_code_pr_feedback_webhook(
            start_response=start_response,
            trace_id=trace_id,
            delivery_id=delivery_id,
            event_name=event_name,
            payload=payload,
            record_store=record_store,
        )
    if event_name == "pull_request":
        return _handle_every_code_pull_request_webhook(
            start_response=start_response,
            trace_id=trace_id,
            delivery_id=delivery_id,
            payload=payload,
            record_store=record_store,
        )
    if event_name != "issues":
        return _json_response(
            start_response=start_response,
            status_code=202,
            payload={
                "status": "accepted",
                "trace_id": trace_id,
                "skipped": True,
                "reason": "unsupported_event",
            },
        )
    if payload.get("action") == "closed":
        return _handle_every_code_issue_closed_webhook(
            start_response=start_response,
            trace_id=trace_id,
            delivery_id=delivery_id,
            payload=payload,
            record_store=record_store,
        )
    if payload.get("action") != "labeled":
        return _json_response(
            start_response=start_response,
            status_code=202,
            payload={
                "status": "accepted",
                "trace_id": trace_id,
                "skipped": True,
                "reason": "unsupported_action",
            },
        )

    label = _github_webhook_mapping(payload, "label")
    label_name = _github_webhook_string(label, "name")
    if label_name.strip().lower() != _EVERY_CODE_TRIGGER_LABEL:
        return _json_response(
            start_response=start_response,
            status_code=202,
            payload={
                "status": "accepted",
                "trace_id": trace_id,
                "skipped": True,
                "reason": "label_not_matched",
            },
        )

    repository_payload = _github_webhook_mapping(payload, "repository")
    issue_payload = _github_webhook_mapping(payload, "issue")
    sender_payload = _github_webhook_mapping(payload, "sender")
    issue_number_value = issue_payload.get("number") if issue_payload is not None else None
    if not isinstance(issue_number_value, int):
        raise ValueError("GitHub issue webhook requires integer issue.number")

    request = EveryCodeWorkRequestCreateEnvelope(
        repository=_github_webhook_string(repository_payload, "full_name"),
        issue_number=issue_number_value,
        issue_url=_github_webhook_string(issue_payload, "html_url"),
        issue_title=_github_webhook_string(issue_payload, "title"),
        trigger_label=_EVERY_CODE_TRIGGER_LABEL,
        trigger_actor=_github_webhook_string(sender_payload, "login"),
        github_delivery_id=delivery_id,
        source="github_issue_label",
        queued_at=_utc_now_timestamp(),
    )
    record = _build_every_code_work_request_record(request, queued_at=request.queued_at)
    every_code_store = _every_code_work_request_store(record_store)
    stored_record, created = every_code_store.create_every_code_work_request_record_if_absent(
        record
    )
    deduped = not created

    accepted_payload = _accepted_payload(
        trace_id=trace_id,
        result={"request_id": stored_record.request_id, "state": stored_record.state},
        driver_result={"request": stored_record.model_dump(mode="json")},
    )
    accepted_payload["deduped"] = deduped
    accepted_payload["github_delivery_id"] = delivery_id
    return _json_response(start_response=start_response, status_code=202, payload=accepted_payload)


def _handle_every_code_issue_closed_webhook(
    *,
    start_response: _StartResponse,
    trace_id: str,
    delivery_id: str,
    payload: dict[str, object],
    record_store: object,
) -> list[bytes]:
    repository_payload = _github_webhook_mapping(payload, "repository")
    issue_payload = _github_webhook_mapping(payload, "issue")
    repository = _github_webhook_string(repository_payload, "full_name")
    issue_number_value = issue_payload.get("number") if issue_payload is not None else None
    if not isinstance(issue_number_value, int):
        raise ValueError("GitHub issue webhook requires integer issue.number")
    issue_url = _github_webhook_string(issue_payload, "html_url")
    closed_at = _github_webhook_string(issue_payload, "closed_at") or _utc_now_timestamp()
    state_reason = _github_webhook_string(issue_payload, "state_reason")

    every_code_store = _every_code_work_request_store(record_store)
    updated_records: list[EveryCodeWorkRequestRecord] = []
    terminal_records: list[EveryCodeWorkRequestRecord] = []
    for record in _iter_every_code_work_request_records(
        every_code_store,
        repository=repository,
    ):
        if record.issue_number != issue_number_value:
            continue
        if issue_url.strip() and record.issue_url.strip() != issue_url.strip():
            continue
        closed_record = close_every_code_work_request_for_issue(
            record,
            closed_at=closed_at,
            reason=state_reason,
        )
        if closed_record is None:
            terminal_records.append(record)
            continue
        every_code_store.write_every_code_work_request_record(closed_record)
        updated_records.append(closed_record)

    if not updated_records:
        response_payload: dict[str, object] = {
            "status": "accepted",
            "trace_id": trace_id,
            "skipped": True,
            "reason": "linked_every_code_request_not_found",
            "github_delivery_id": delivery_id,
        }
        if terminal_records:
            terminal_record = terminal_records[0]
            response_payload["reason"] = "linked_every_code_request_already_terminal"
            response_payload["result"] = {
                "request_id": terminal_record.request_id,
                "state": terminal_record.state,
            }
        return _json_response(
            start_response=start_response,
            status_code=202,
            payload=response_payload,
        )

    updated_record = updated_records[0]
    accepted_payload = _accepted_payload(
        trace_id=trace_id,
        result={
            "request_id": updated_record.request_id,
            "state": updated_record.state,
            "closed_count": len(updated_records),
        },
        driver_result={
            "request": updated_record.model_dump(mode="json"),
            "closed_count": len(updated_records),
            "requests": [record.model_dump(mode="json") for record in updated_records],
        },
    )
    accepted_payload["github_delivery_id"] = delivery_id
    return _json_response(start_response=start_response, status_code=202, payload=accepted_payload)


def _handle_every_code_pull_request_webhook(
    *,
    start_response: _StartResponse,
    trace_id: str,
    delivery_id: str,
    payload: dict[str, object],
    record_store: object,
) -> list[bytes]:
    if payload.get("action") != "closed":
        return _json_response(
            start_response=start_response,
            status_code=202,
            payload={
                "status": "accepted",
                "trace_id": trace_id,
                "skipped": True,
                "reason": "unsupported_action",
            },
        )

    repository_payload = _github_webhook_mapping(payload, "repository")
    pull_request_payload = _github_webhook_mapping(payload, "pull_request")
    repository = _github_webhook_string(repository_payload, "full_name")
    pr_url = _github_webhook_string(pull_request_payload, "html_url")
    linked_issue_numbers = (
        _github_issue_numbers_referenced_by_pull_request(
            pull_request_payload,
            repository=repository,
        )
        if pull_request_payload is not None
        else frozenset()
    )
    merged = bool(pull_request_payload.get("merged")) if pull_request_payload else False
    closed_at = _github_webhook_string(pull_request_payload, "closed_at") or _utc_now_timestamp()
    every_code_store = _every_code_work_request_store(record_store)
    candidate_records: dict[str, EveryCodeWorkRequestRecord] = {}
    repository_records = tuple(
        _iter_every_code_work_request_records(every_code_store, repository=repository)
    )
    for record in repository_records:
        if record.result_pr_url.strip() == pr_url:
            candidate_records[record.request_id] = record

    feedback_record = _find_every_code_pr_feedback_for_pull_request(
        every_code_store,
        repository=repository,
        pr_url=pr_url,
    )
    if feedback_record is not None:
        feedback_request_record = every_code_store.read_every_code_work_request_record(
            feedback_record.request_id
        )
        candidate_records[feedback_request_record.request_id] = feedback_request_record

    for record in repository_records:
        if _every_code_issue_url_matches_pull_request(
            issue_url=record.issue_url,
            repository=repository,
            pr_url=pr_url,
            linked_issue_numbers=linked_issue_numbers,
        ):
            candidate_records[record.request_id] = record

    if not candidate_records:
        return _json_response(
            start_response=start_response,
            status_code=202,
            payload={
                "status": "accepted",
                "trace_id": trace_id,
                "skipped": True,
                "reason": "linked_every_code_request_not_found",
                "github_delivery_id": delivery_id,
            },
        )

    updated_records: list[EveryCodeWorkRequestRecord] = []
    terminal_records: list[EveryCodeWorkRequestRecord] = []
    for record in candidate_records.values():
        closed_record = close_every_code_work_request_for_pull_request(
            record,
            pr_url=pr_url,
            merged=merged,
            closed_at=closed_at,
        )
        if closed_record is None:
            terminal_records.append(record)
            continue
        every_code_store.write_every_code_work_request_record(closed_record)
        updated_records.append(closed_record)

    if not updated_records:
        terminal_record = terminal_records[0]
        return _json_response(
            start_response=start_response,
            status_code=202,
            payload={
                "status": "accepted",
                "trace_id": trace_id,
                "skipped": True,
                "reason": "linked_every_code_request_already_terminal",
                "result": {
                    "request_id": terminal_record.request_id,
                    "state": terminal_record.state,
                },
                "github_delivery_id": delivery_id,
            },
        )

    updated_record = updated_records[0]
    accepted_payload = _accepted_payload(
        trace_id=trace_id,
        result={
            "request_id": updated_record.request_id,
            "state": updated_record.state,
            "closed_count": len(updated_records),
        },
        driver_result={
            "request": updated_record.model_dump(mode="json"),
            "closed_count": len(updated_records),
            "requests": [record.model_dump(mode="json") for record in updated_records],
        },
    )
    accepted_payload["github_delivery_id"] = delivery_id
    return _json_response(start_response=start_response, status_code=202, payload=accepted_payload)


def _every_code_issue_url_matches_pull_request(
    *,
    issue_url: str,
    repository: str,
    pr_url: str,
    linked_issue_numbers: frozenset[int],
) -> bool:
    normalized_issue_url = issue_url.strip().rstrip("/").lower()
    normalized_pr_url = pr_url.strip().rstrip("/").lower()
    if not normalized_issue_url or not normalized_pr_url:
        return False
    if normalized_issue_url == normalized_pr_url:
        return True
    normalized_repository = repository.strip().strip("/").lower()
    if not normalized_repository:
        return False
    return any(
        normalized_issue_url == f"https://github.com/{normalized_repository}/issues/{issue_number}"
        for issue_number in linked_issue_numbers
    )


def _handle_every_code_preview_validation_webhook(
    *,
    start_response: _StartResponse,
    trace_id: str,
    delivery_id: str,
    payload: dict[str, object],
    record_store: object,
    control_plane_root_path: Path,
) -> list[bytes] | None:
    if payload.get("action") != "created":
        return None
    issue_payload = _github_webhook_mapping(payload, "issue")
    if issue_payload is None or isinstance(issue_payload.get("pull_request"), dict):
        return None
    repository_payload = _github_webhook_mapping(payload, "repository")
    repository = _github_webhook_string(repository_payload, "full_name")
    if "/" not in repository:
        return None
    owner, repo = repository.split("/", 1)
    issue_number_value = issue_payload.get("number")
    if not isinstance(issue_number_value, int):
        return None
    issue_author_payload = _github_webhook_mapping(issue_payload, "user")
    issue_author = _github_webhook_string(issue_author_payload, "login")
    actor = _github_actor_login(payload)
    comment_payload = _github_webhook_mapping(payload, "comment")
    if comment_payload is None:
        return None
    comment_body = _github_webhook_string(comment_payload, "body")
    if not comment_body.strip().lower().startswith("/preview"):
        return None
    if not _every_code_feedback_actor_is_trusted(
        repository=repository,
        actor=actor,
        source_issue_author=issue_author,
    ):
        return _every_code_untrusted_feedback_response(
            start_response=start_response,
            trace_id=trace_id,
            delivery_id=delivery_id,
        )
    every_code_store = _every_code_work_request_store(record_store)
    context_name = launchplane_anchor_repo_context(
        record_store=cast(FilesystemRecordStore, record_store),
        repo=repo,
    )
    if not context_name:
        context_name = f"{repo}-preview"
    try:
        token = resolve_launchplane_github_token(
            control_plane_root=control_plane_root_path,
            context_name=context_name,
        )
        result = handle_every_code_preview_validation_comment(
            record_store=every_code_store,
            owner=owner,
            repo=repo,
            issue_number=issue_number_value,
            issue_url=_github_webhook_string(issue_payload, "html_url"),
            issue_author=issue_author,
            actor=actor,
            comment_body=comment_body,
            comment_id=str(comment_payload.get("id") or ""),
            comment_node_id=_github_webhook_string(comment_payload, "node_id"),
            comment_url=_github_webhook_string(comment_payload, "html_url"),
            delivery_id=delivery_id,
            token=token,
            received_at=_utc_now_timestamp(),
        )
    except click.ClickException as exc:
        return _json_response(
            start_response=start_response,
            status_code=202,
            payload={
                "status": "accepted",
                "trace_id": trace_id,
                "skipped": True,
                "reason": "preview_validation_failed",
                "message": str(exc),
                "github_delivery_id": delivery_id,
            },
        )
    if not bool(result.get("handled")):
        return None
    accepted_payload = _accepted_payload(
        trace_id=trace_id,
        result={"preview_validation": {key: value for key, value in result.items() if key != "handled"}},
        driver_result={"preview_validation": dict(result)},
    )
    if bool(result.get("skipped")):
        accepted_payload["skipped"] = True
        reason = result.get("reason")
        if isinstance(reason, str):
            accepted_payload["reason"] = reason
    if bool(result.get("deduped")):
        accepted_payload["deduped"] = True
    accepted_payload["github_delivery_id"] = delivery_id
    return _json_response(start_response=start_response, status_code=202, payload=accepted_payload)


def _github_issue_numbers_referenced_by_pull_request(
    pull_request_payload: dict[str, object],
    *,
    repository: str,
) -> frozenset[int]:
    normalized_repository = repository.strip().strip("/").lower()
    if not normalized_repository:
        return frozenset()

    issue_numbers: set[int] = set()
    for field in ("title", "body"):
        value = _github_webhook_string(pull_request_payload, field)
        if not value:
            continue
        for closing_reference_match in _GITHUB_CLOSING_REFERENCE_PATTERN.finditer(value):
            references = closing_reference_match.group(1)
            for issue_reference_match in _GITHUB_ISSUE_REFERENCE_PATTERN.finditer(references):
                reference_repository = (
                    issue_reference_match.group("url_repository")
                    or issue_reference_match.group("repository")
                    or normalized_repository
                ).lower()
                if reference_repository != normalized_repository:
                    continue
                issue_number = issue_reference_match.group(
                    "url_number"
                ) or issue_reference_match.group("number")
                issue_numbers.add(int(issue_number))
    return frozenset(issue_numbers)


def _find_every_code_pr_feedback_for_pull_request(
    every_code_store: _EveryCodeWorkRequestStore,
    *,
    repository: str,
    pr_url: str,
) -> EveryCodePrFeedbackRecord | None:
    for record in _iter_every_code_pr_feedback_records(
        every_code_store,
        repository=repository,
    ):
        if record.pr_url.strip() == pr_url:
            return record
    return None


def _iter_every_code_work_request_records(
    every_code_store: _EveryCodeWorkRequestStore,
    *,
    repository: str,
) -> Iterable[EveryCodeWorkRequestRecord]:
    page_size = 100
    offset = 0
    while True:
        records = every_code_store.list_every_code_work_request_records(
            repository=repository,
            limit=page_size,
            offset=offset,
        )
        if not records:
            break
        yield from records
        if len(records) < page_size:
            break
        offset += page_size


def _iter_every_code_pr_feedback_records(
    every_code_store: _EveryCodeWorkRequestStore,
    *,
    repository: str,
) -> Iterable[EveryCodePrFeedbackRecord]:
    page_size = 100
    offset = 0
    while True:
        records = every_code_store.list_every_code_pr_feedback_records(
            repository=repository,
            limit=page_size,
            offset=offset,
        )
        if not records:
            break
        yield from records
        if len(records) < page_size:
            break
        offset += page_size


def _handle_every_code_pr_feedback_webhook(
    *,
    start_response: _StartResponse,
    trace_id: str,
    delivery_id: str,
    event_name: str,
    payload: dict[str, object],
    record_store: object,
) -> list[bytes]:
    if not _every_code_pr_feedback_action_supported(
        event_name=event_name,
        action=str(payload.get("action", "")),
    ):
        return _json_response(
            start_response=start_response,
            status_code=202,
            payload={
                "status": "accepted",
                "trace_id": trace_id,
                "skipped": True,
                "reason": "unsupported_action",
                "github_delivery_id": delivery_id,
            },
        )

    sender_payload = _github_webhook_mapping(payload, "sender")
    body_payload = _every_code_feedback_body_payload(event_name=event_name, payload=payload)
    if _every_code_feedback_actor_is_automation(
        sender_payload=sender_payload,
        body_payload=body_payload,
    ):
        return _json_response(
            start_response=start_response,
            status_code=202,
            payload={
                "status": "accepted",
                "trace_id": trace_id,
                "skipped": True,
                "reason": "automation_actor",
                "github_delivery_id": delivery_id,
            },
        )

    repository_payload = _github_webhook_mapping(payload, "repository")
    repository = _github_webhook_string(repository_payload, "full_name")
    actor = _github_webhook_string(sender_payload, "login")
    if not _every_code_feedback_actor_is_trusted(repository=repository, actor=actor):
        return _every_code_untrusted_feedback_response(
            start_response=start_response,
            trace_id=trace_id,
            delivery_id=delivery_id,
        )
    pr_number, pr_url = _every_code_feedback_pr_reference(
        event_name=event_name,
        payload=payload,
        repository=repository,
    )
    body = _github_webhook_string(body_payload, "body")
    if not body.strip():
        return _json_response(
            start_response=start_response,
            status_code=202,
            payload={
                "status": "accepted",
                "trace_id": trace_id,
                "skipped": True,
                "reason": "empty_feedback_body",
                "github_delivery_id": delivery_id,
            },
        )

    every_code_store = _every_code_work_request_store(record_store)
    matched_record = next(
        (
            record
            for record in _iter_every_code_work_request_records(
                every_code_store,
                repository=repository,
            )
            if record.result_pr_url.strip() == pr_url
        ),
        None,
    )
    if matched_record is None:
        pull_request_payload = _github_webhook_mapping(payload, "pull_request")
        linked_issue_numbers: frozenset[int] = frozenset()
        if pull_request_payload is not None:
            linked_issue_numbers = _github_issue_numbers_referenced_by_pull_request(
                pull_request_payload, repository=repository
            )
        if event_name == "issue_comment":
            issue_payload = _github_webhook_mapping(payload, "issue")
            if issue_payload is not None:
                linked_issue_numbers = (
                    linked_issue_numbers
                    | _github_issue_numbers_referenced_by_pull_request(
                        issue_payload, repository=repository
                    )
                )
        for record in _iter_every_code_work_request_records(
            every_code_store,
            repository=repository,
        ):
            if _every_code_issue_url_matches_pull_request(
                issue_url=record.issue_url,
                repository=repository,
                pr_url=pr_url,
                linked_issue_numbers=linked_issue_numbers,
            ):
                matched_record = record
                break
    if matched_record is None:
        return _json_response(
            start_response=start_response,
            status_code=202,
            payload={
                "status": "accepted",
                "trace_id": trace_id,
                "skipped": True,
                "reason": "linked_every_code_request_not_found",
                "github_delivery_id": delivery_id,
            },
        )

    github_node_id = _github_webhook_string(body_payload, "node_id")
    github_id_value = body_payload.get("id") if body_payload is not None else ""
    github_id = str(github_id_value) if github_id_value is not None else ""
    feedback_id = build_every_code_pr_feedback_id(
        repository=repository,
        pr_number=pr_number,
        github_delivery_id=delivery_id,
        github_node_id=github_node_id,
        github_id=github_id,
    )
    existing_feedback_records = every_code_store.list_every_code_pr_feedback_records(
        request_id=matched_record.request_id,
        repository=repository,
        pr_number=pr_number,
        limit=100,
    )
    for existing_feedback in existing_feedback_records:
        if existing_feedback.feedback_id == feedback_id:
            return _json_response(
                start_response=start_response,
                status_code=202,
                payload={
                    "status": "accepted",
                    "trace_id": trace_id,
                    "deduped": True,
                    "result": {
                        "feedback_id": existing_feedback.feedback_id,
                        "request_id": existing_feedback.request_id,
                    },
                    "github_delivery_id": delivery_id,
                },
            )

    feedback_record = EveryCodePrFeedbackRecord(
        feedback_id=feedback_id,
        request_id=matched_record.request_id,
        repository=repository,
        pr_number=pr_number,
        pr_url=pr_url,
        feedback_kind=_every_code_feedback_kind(event_name),
        github_delivery_id=delivery_id,
        github_node_id=github_node_id,
        github_id=github_id,
        actor=actor,
        author_association=_github_webhook_string(body_payload, "author_association"),
        body=body,
        html_url=_github_webhook_string(body_payload, "html_url"),
        submitted_at=_github_webhook_string(body_payload, "submitted_at"),
        received_at=_utc_now_timestamp(),
    )
    every_code_store.write_every_code_pr_feedback_record(feedback_record)

    accepted_payload = _accepted_payload(
        trace_id=trace_id,
        result={
            "feedback_id": feedback_record.feedback_id,
            "request_id": feedback_record.request_id,
            "status": feedback_record.status,
        },
        driver_result={"feedback": feedback_record.model_dump(mode="json")},
    )
    accepted_payload["github_delivery_id"] = delivery_id
    return _json_response(start_response=start_response, status_code=202, payload=accepted_payload)


def _every_code_pr_feedback_action_supported(*, event_name: str, action: str) -> bool:
    if event_name == "issue_comment":
        return action == "created"
    if event_name == "pull_request_review":
        return action == "submitted"
    if event_name == "pull_request_review_comment":
        return action == "created"
    return False


def _every_code_feedback_actor_is_automation(
    *,
    sender_payload: dict[str, object] | None,
    body_payload: dict[str, object],
) -> bool:
    actors = [sender_payload]
    user_payload = _github_webhook_mapping(body_payload, "user")
    if user_payload is not None:
        actors.append(user_payload)
    for actor_payload in actors:
        if actor_payload is None:
            continue
        actor_type = _github_webhook_string(actor_payload, "type").lower()
        actor_login = _github_webhook_string(actor_payload, "login").lower()
        if actor_type == "bot" or actor_login.endswith("[bot]"):
            return True
    return False


def _every_code_feedback_kind(event_name: str) -> EveryCodePrFeedbackKind:
    if event_name == "issue_comment":
        return "issue_comment"
    if event_name == "pull_request_review":
        return "pull_request_review"
    if event_name == "pull_request_review_comment":
        return "pull_request_review_comment"
    raise ValueError(f"Unsupported Every Code PR feedback event {event_name!r}")


def _every_code_feedback_body_payload(
    *, event_name: str, payload: dict[str, object]
) -> dict[str, object]:
    key = "review" if event_name == "pull_request_review" else "comment"
    return _github_webhook_required_mapping(payload, key)


def _every_code_feedback_pr_reference(
    *,
    event_name: str,
    payload: dict[str, object],
    repository: str,
) -> tuple[int, str]:
    if event_name == "issue_comment":
        issue_payload = _github_webhook_required_mapping(payload, "issue")
        pull_request_marker = issue_payload.get("pull_request")
        if not isinstance(pull_request_marker, dict):
            raise ValueError("Every Code PR feedback issue_comment requires pull_request issue")
        pr_number_value = issue_payload.get("number")
    else:
        pull_request_payload = _github_webhook_required_mapping(payload, "pull_request")
        pr_number_value = pull_request_payload.get("number")
    if not isinstance(pr_number_value, int):
        raise ValueError("Every Code PR feedback requires integer pull request number")
    return pr_number_value, f"https://github.com/{repository}/pull/{pr_number_value}"


def _idempotency_capable_store(record_store: object) -> _IdempotencyCapableStore | None:
    if hasattr(record_store, "read_idempotency_record") and hasattr(
        record_store, "write_idempotency_record"
    ):
        return cast(_IdempotencyCapableStore, record_store)
    return None


def _human_session_capable_store(record_store: object) -> HumanSessionStore | None:
    if all(
        hasattr(record_store, method_name)
        for method_name in ("write_session", "read_session", "delete_session")
    ):
        return record_store  # type: ignore[return-value]
    return None


def _idempotency_key(environ: dict[str, object]) -> str:
    return str(environ.get("HTTP_IDEMPOTENCY_KEY", "")).strip()


def _identity_actor(identity: LaunchplaneIdentity) -> str:
    if isinstance(identity, GitHubHumanIdentity):
        return f"github:{identity.login}"
    if isinstance(identity, TerminalAgentIdentity):
        return f"terminal-agent:{identity.subject}"
    return (
        f"github-actions:{identity.repository}:{identity.workflow_ref or identity.job_workflow_ref}"
    )


def _idempotency_scope(identity: LaunchplaneIdentity) -> str:
    if isinstance(identity, GitHubHumanIdentity):
        return "|".join(("github-human", identity.login, str(identity.github_id)))
    if isinstance(identity, TerminalAgentIdentity):
        return "|".join(("terminal-agent", identity.subject, identity.token_label))
    workflow_ref = identity.workflow_ref or identity.job_workflow_ref or ""
    return "|".join(
        (
            str(identity.repository).strip(),
            str(workflow_ref).strip(),
            str(identity.subject).strip(),
        )
    )


def _request_fingerprint(payload: dict[str, object]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _canonical_request_payload_for_idempotency(
    *, route_path: str, payload: dict[str, object]
) -> dict[str, object]:
    if route_path not in _PREVIEW_DESTROY_ROUTE_PATHS:
        return payload
    canonical_payload = json.loads(json.dumps(payload))
    destroy_payload = canonical_payload.get("destroy")
    if isinstance(destroy_payload, dict):
        destroy_payload.pop("destroy_reason", None)
    return cast(dict[str, object], canonical_payload)


def _idempotency_request_fingerprint(*, route_path: str, payload: dict[str, object]) -> str:
    return _request_fingerprint(
        _canonical_request_payload_for_idempotency(route_path=route_path, payload=payload)
    )


def _accepted_payload(
    *,
    trace_id: str,
    result: dict[str, object],
    driver_result: BaseModel | dict[str, object] | None,
    replayed: bool = False,
    original_trace_id: str = "",
) -> dict[str, object]:
    serialized_driver_result: dict[str, object] | None = None
    if isinstance(driver_result, BaseModel):
        serialized_driver_result = driver_result.model_dump(mode="json")
    elif isinstance(driver_result, dict):
        serialized_driver_result = dict(driver_result)
    payload: dict[str, object] = {
        "status": "accepted",
        "trace_id": trace_id,
        "records": {
            key: str(value)
            for key, value in result.items()
            if key
            in {
                "deployment_record_id",
                "backup_gate_record_id",
                "backup_record_id",
                "release_tuple_id",
                "inventory_record_id",
                "preview_id",
                "preview_desired_state_id",
                "preview_inventory_scan_id",
                "preview_pr_feedback_id",
                "preview_lifecycle_cleanup_id",
                "preview_lifecycle_plan_id",
                "authz_policy_record_id",
                "runtime_key_safety_policy_record_id",
                "product_profile",
                "dokploy_target_count",
                "dokploy_target_id_count",
                "runtime_environment_record_count",
                "secret_binding_count",
                "generation_id",
                "promotion_record_id",
                "target_id",
                "target_type",
                "image_reference",
                "artifact_id",
                "transition",
                "request_id",
                "state",
                "merge_train_run_id",
            }
        },
        **({"result": serialized_driver_result} if serialized_driver_result else {}),
    }
    if replayed:
        payload["replayed"] = True
        payload["original_trace_id"] = original_trace_id
    return payload


def _replay_idempotent_response(
    *,
    start_response: _StartResponse,
    trace_id: str,
    stored_record: LaunchplaneIdempotencyRecord,
) -> list[bytes]:
    stored_payload = dict(stored_record.response_payload)
    stored_driver_result = stored_payload.get("result")
    result_payload = _accepted_payload(
        trace_id=trace_id,
        result=dict(stored_payload.get("records") or {}),
        driver_result=stored_driver_result if isinstance(stored_driver_result, dict) else None,
        replayed=True,
        original_trace_id=stored_record.response_trace_id,
    )
    return _json_response(
        start_response=start_response,
        status_code=stored_record.response_status_code,
        payload=result_payload,
    )


def _agent_write_intent_secret_evidence(
    *, record_store: object, request: AgentWriteIntentRequest
) -> AgentWriteIntentSecretEvidence:
    if not request.secret_bindings:
        return secret_evidence_for_agent_write_intent(request=request, evaluation=None)
    if request.destination is None:
        return secret_evidence_for_agent_write_intent(
            request=request, evaluation=None, unavailable=True
        )
    try:
        policy_record = latest_active_runtime_key_safety_policy(
            record_store  # type: ignore[arg-type]
        )
        evaluation = evaluate_runtime_key_safety_from_store(
            record_store=record_store,  # type: ignore[arg-type]
            policy_record=policy_record,
            target=RuntimeKeySafetyTarget(
                context=request.destination.context,
                instance=request.destination.instance,
                environment_class=runtime_key_safety_environment_class(
                    request.destination.instance
                ),
            ),
            required_binding_keys=request.secret_bindings,
        )
    except (AttributeError, ValueError):
        return secret_evidence_for_agent_write_intent(
            request=request, evaluation=None, unavailable=True
        )
    return secret_evidence_for_agent_write_intent(
        request=request,
        evaluation=evaluation,
        policy_record_id=policy_record.record_id,
        policy_sha256=policy_record.policy_sha256,
    )


def _read_idempotency_record(
    *,
    record_store: object,
    scope: str,
    route_path: str,
    idempotency_key: str,
) -> LaunchplaneIdempotencyRecord | None:
    idempotency_store = _idempotency_capable_store(record_store)
    if idempotency_store is None or not idempotency_key:
        return None
    return idempotency_store.read_idempotency_record(
        scope=scope,
        route_path=route_path,
        idempotency_key=idempotency_key,
    )


def _write_idempotency_record(
    *,
    record_store: object,
    scope: str,
    route_path: str,
    idempotency_key: str,
    request_fingerprint: str,
    response_status_code: int,
    response_trace_id: str,
    response_payload: dict[str, object],
) -> None:
    idempotency_store = _idempotency_capable_store(record_store)
    if idempotency_store is None or not idempotency_key:
        return
    idempotency_store.write_idempotency_record(
        LaunchplaneIdempotencyRecord(
            record_id=build_launchplane_idempotency_record_id(
                response_trace_id=response_trace_id,
            ),
            scope=scope,
            route_path=route_path,
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint,
            response_status_code=response_status_code,
            response_trace_id=response_trace_id,
            recorded_at=_utc_now_timestamp(),
            response_payload=response_payload,
        )
    )


def _check_idempotent_request(
    *,
    record_store: object,
    scope: str,
    route_path: str,
    idempotency_key: str,
    request_fingerprint: str,
    start_response: _StartResponse,
    trace_id: str,
) -> list[bytes] | None:
    stored_record = _read_idempotency_record(
        record_store=record_store,
        scope=scope,
        route_path=route_path,
        idempotency_key=idempotency_key,
    )
    if stored_record is None:
        return None
    if stored_record.request_fingerprint != request_fingerprint:
        return _json_response(
            start_response=start_response,
            status_code=409,
            payload={
                "status": "rejected",
                "trace_id": trace_id,
                "error": {
                    "code": "idempotency_key_reused",
                    "message": (
                        "Idempotency-Key was already used for a different Launchplane request payload on this route."
                    ),
                },
            },
        )
    return _replay_idempotent_response(
        start_response=start_response,
        trace_id=trace_id,
        stored_record=stored_record,
    )


def _driver_result_status_values(
    driver_result: BaseModel | dict[str, object] | object,
) -> tuple[str, ...]:
    if isinstance(driver_result, BaseModel):
        items = driver_result.model_dump(mode="json").items()
    elif isinstance(driver_result, dict):
        items = driver_result.items()
    elif hasattr(driver_result, "__dict__"):
        items = vars(driver_result).items()
    else:
        return ()
    return tuple(
        str(value).strip() for key, value in items if key.endswith("_status") or key == "status"
    )


def _driver_result_contains_status(
    driver_result: BaseModel | dict[str, object] | object, status: str
) -> bool:
    return status in _driver_result_status_values(driver_result)


def _should_store_idempotency_record(
    *, path: str, driver_result: BaseModel | dict[str, object] | None
) -> bool:
    if path in _NON_IDEMPOTENT_DRIVER_RESULT_ROUTES:
        return False
    if (
        path
        in {
            "/v1/authz-policies/github-actions/grants",
            "/v1/authz-policies/github-humans/grants",
            "/v1/authz-policies/terminal-agents/grants",
        }
        and isinstance(driver_result, dict)
        and driver_result.get("mode") == "dry_run"
    ):
        return False
    if driver_result is None:
        return True
    if _driver_result_contains_status(driver_result, "blocked"):
        return False
    if _driver_result_contains_status(driver_result, "fail"):
        return False
    if path in _PENDING_RESULT_IDEMPOTENCY_SKIP_ROUTES:
        return not _driver_result_contains_status(driver_result, "pending")
    return True


def _read_json_request(environ: dict[str, object]) -> dict[str, object]:
    body_bytes = _read_request_body(environ)
    if not body_bytes:
        raise ValueError("Request body is required.")
    return _decode_json_request_body(body_bytes)


def _read_request_body(environ: dict[str, object]) -> bytes:
    content_length = int(str(environ.get("CONTENT_LENGTH", "0") or "0"))
    body_stream = cast(BinaryIO | None, environ.get("wsgi.input"))
    return body_stream.read(content_length) if body_stream is not None else b""


def _decode_json_request_body(body_bytes: bytes) -> dict[str, object]:
    try:
        payload = json.loads(body_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Request body must be valid UTF-8 JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("Request body must decode to a JSON object.")
    return payload


def _bearer_token(environ: dict[str, object]) -> str:
    header = str(environ.get("HTTP_AUTHORIZATION", "")).strip()
    if not header:
        raise PermissionError("Authorization header is required.")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise PermissionError("Authorization header must use Bearer token format.")
    return token.strip()


def _every_code_worker_token_from_env() -> str:
    return os.environ.get("LAUNCHPLANE_EVERY_CODE_WORKER_TOKEN", "").strip()


def _terminal_agent_read_token_from_env() -> str:
    return os.environ.get("LAUNCHPLANE_TERMINAL_AGENT_READ_TOKEN", "").strip()


def _terminal_agent_subject_from_env() -> str:
    return os.environ.get("LAUNCHPLANE_TERMINAL_AGENT_SUBJECT", "local-owner-agent").strip()


def _terminal_agent_token_label_from_env() -> str:
    return os.environ.get("LAUNCHPLANE_TERMINAL_AGENT_TOKEN_LABEL", "local-owner-read").strip()


def _terminal_agent_identity_from_bearer(
    environ: dict[str, object]
) -> TerminalAgentIdentity | None:
    expected_token = _terminal_agent_read_token_from_env()
    if not expected_token:
        return None
    try:
        provided_token = _bearer_token(environ)
    except PermissionError:
        return None
    if not secrets.compare_digest(provided_token, expected_token):
        return None
    subject = _terminal_agent_subject_from_env() or "local-owner-agent"
    token_label = _terminal_agent_token_label_from_env() or "local-owner-read"
    return TerminalAgentIdentity(subject=subject, token_label=token_label)


def _is_every_code_worker_route(*, method: str, path: str) -> bool:
    if method == "GET" and path == "/v1/product-profiles":
        return True
    if method == "GET" and path == "/v1/previews/readiness":
        return True
    if method == "GET" and path == "/v1/every-code/summary":
        return True
    if method == "GET" and path == "/v1/every-code/work-requests":
        return True
    if method == "GET" and path == "/v1/every-code/pr-feedback":
        return True
    if method == "GET" and path == "/v1/every-code/preview-gates":
        return True
    if method == "GET" and path.startswith("/v1/every-code/work-requests/"):
        return True
    return method == "POST" and path in {
        "/v1/every-code/work-requests/claim",
        "/v1/every-code/work-requests/rerun",
        "/v1/every-code/work-requests/status",
        "/v1/every-code/pr-feedback/status",
        "/v1/every-code/preview-gates",
    }


def _every_code_worker_token_authorized(
    *, environ: dict[str, object], method: str, path: str
) -> bool:
    if not _is_every_code_worker_route(method=method, path=path):
        return False
    expected_token = _every_code_worker_token_from_env()
    if not expected_token:
        return False
    try:
        provided_token = _bearer_token(environ)
    except PermissionError:
        return False
    return secrets.compare_digest(provided_token, expected_token)


def _every_code_pagination_value(query: dict[str, list[str]], *, key: str, default: int) -> int:
    value = int(str((query.get(key) or [str(default)])[0] or str(default)))
    if value < 0:
        raise ValueError(f"Every Code pagination {key} must be non-negative")
    return value


def _every_code_read_payload(
    *,
    record_store: object,
    path: str,
    query: dict[str, list[str]],
) -> dict[str, object]:
    segments = [segment for segment in path.split("/") if segment]
    if path == "/v1/product-profiles":
        driver_id_filter = str((query.get("driver_id") or [""])[0] or "").strip()
        return control_plane_product_read_service.build_product_profile_list_service_payload(
            record_store=cast(ProductReadModelStore, record_store),
            driver_id=driver_id_filter,
        )
    every_code_store = _every_code_work_request_store(record_store)
    if path == "/v1/previews/readiness":
        repository_filter = str((query.get("repository") or [""])[0] or "").strip()
        pr_number_value = str((query.get("pr_number") or [""])[0] or "").strip()
        pr_number_filter = int(pr_number_value) if pr_number_value else None
        status_filter = str((query.get("status") or [""])[0] or "").strip()
        limit = _every_code_pagination_value(query, key="limit", default=50)
        offset = _every_code_pagination_value(query, key="offset", default=0)
        readiness = build_preview_readiness_read_model(
            generated_at=_utc_now_timestamp(),
            record_store=every_code_store,
            repository=repository_filter,
            pr_number=pr_number_filter,
            status=status_filter,
            limit=limit,
            offset=offset,
        )
        return {"readiness": readiness.model_dump(mode="json")}
    if path == "/v1/every-code/summary":
        repository_filter = str((query.get("repository") or [""])[0] or "").strip()
        issue_number_value = str((query.get("issue_number") or [""])[0] or "").strip()
        issue_number_filter = int(issue_number_value) if issue_number_value else None
        state_filter = str((query.get("state") or [""])[0] or "").strip()
        limit = _every_code_pagination_value(query, key="limit", default=50)
        offset = _every_code_pagination_value(query, key="offset", default=0)
        summary = build_every_code_summary_read_model(
            generated_at=_utc_now_timestamp(),
            record_store=every_code_store,
            repository=repository_filter,
            issue_number=issue_number_filter,
            state=state_filter,
            limit=limit,
            offset=offset,
        )
        return {"summary": summary.model_dump(mode="json")}
    if path == "/v1/every-code/pr-feedback":
        request_id_filter = str((query.get("request_id") or [""])[0] or "").strip()
        repository_filter = str((query.get("repository") or [""])[0] or "").strip()
        status_filter = str((query.get("status") or [""])[0] or "").strip()
        pr_number_value = str((query.get("pr_number") or [""])[0] or "").strip()
        pr_number_filter = int(pr_number_value) if pr_number_value else None
        limit = _every_code_pagination_value(query, key="limit", default=50)
        offset = _every_code_pagination_value(query, key="offset", default=0)
        feedback_records = every_code_store.list_every_code_pr_feedback_records(
            request_id=request_id_filter,
            repository=repository_filter,
            pr_number=pr_number_filter,
            status=status_filter,
            limit=limit,
            offset=offset,
        )
        return {
            "request_id": request_id_filter,
            "repository": repository_filter,
            "status_filter": status_filter,
            "feedback": [record.model_dump(mode="json") for record in feedback_records],
        }
    if path == "/v1/every-code/preview-gates":
        request_id_filter = str((query.get("request_id") or [""])[0] or "").strip()
        repository_filter = str((query.get("repository") or [""])[0] or "").strip()
        status_filter = str((query.get("status") or [""])[0] or "").strip()
        pr_number_value = str((query.get("pr_number") or [""])[0] or "").strip()
        pr_number_filter = int(pr_number_value) if pr_number_value else None
        limit = _every_code_pagination_value(query, key="limit", default=50)
        offset = _every_code_pagination_value(query, key="offset", default=0)
        gate_records = every_code_store.list_every_code_preview_gate_records(
            request_id=request_id_filter,
            repository=repository_filter,
            pr_number=pr_number_filter,
            status=status_filter,
            limit=limit,
            offset=offset,
        )
        return {
            "request_id": request_id_filter,
            "repository": repository_filter,
            "status_filter": status_filter,
            "gates": [record.model_dump(mode="json") for record in gate_records],
        }
    if len(segments) == 4 and segments[:3] == ["v1", "every-code", "work-requests"]:
        record = every_code_store.read_every_code_work_request_record(segments[3])
        return {"request": record.model_dump(mode="json")}
    state_filter = str((query.get("state") or [""])[0] or "").strip()
    repository_filter = str((query.get("repository") or [""])[0] or "").strip()
    limit = _every_code_pagination_value(query, key="limit", default=50)
    offset = _every_code_pagination_value(query, key="offset", default=0)
    work_request_records = every_code_store.list_every_code_work_request_records(
        state=state_filter,
        repository=repository_filter,
        limit=limit,
        offset=offset,
    )
    return {
        "state": state_filter,
        "repository": repository_filter,
        "requests": [record.model_dump(mode="json") for record in work_request_records],
    }


def _handle_every_code_work_request_read(
    *,
    start_response: _StartResponse,
    trace_id: str,
    record_store: object,
    path: str,
    query: dict[str, list[str]],
) -> list[bytes]:
    return _json_response(
        start_response=start_response,
        status_code=200,
        payload={
            "status": "ok",
            "trace_id": trace_id,
            **_every_code_read_payload(record_store=record_store, path=path, query=query),
        },
    )


def _handle_every_code_worker_write(
    *,
    start_response: _StartResponse,
    trace_id: str,
    record_store: object,
    path: str,
    payload: dict[str, object],
) -> list[bytes]:
    every_code_store = _every_code_work_request_store(record_store)
    if path == "/v1/every-code/preview-gates":
        gate_record = EveryCodePreviewGateEnvelope.model_validate(payload)
        every_code_store.write_every_code_preview_gate_record(gate_record)
        return _json_response(
            start_response=start_response,
            status_code=202,
            payload=_accepted_payload(
                trace_id=trace_id,
                result={
                    "gate_id": gate_record.gate_id,
                    "request_id": gate_record.request_id,
                    "status": gate_record.status,
                },
                driver_result={"gate": gate_record.model_dump(mode="json")},
            ),
        )
    if path == "/v1/every-code/pr-feedback/status":
        feedback_status_request = EveryCodePrFeedbackStatusEnvelope.model_validate(payload)
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
            return _json_response(
                start_response=start_response,
                status_code=404,
                payload={
                    "status": "rejected",
                    "trace_id": trace_id,
                    "error": {
                        "code": "not_found",
                        "message": "Every Code PR feedback record was not found.",
                    },
                },
            )
        updated_feedback_record = apply_every_code_pr_feedback_status(
            existing_feedback_record,
            status=feedback_status_request.status,
        )
        if updated_feedback_record is None:
            return _json_response(
                start_response=start_response,
                status_code=409,
                payload={
                    "status": "rejected",
                    "trace_id": trace_id,
                    "error": {
                        "code": "feedback_already_final",
                        "message": "Every Code PR feedback is already applied or ignored.",
                    },
                },
            )
        every_code_store.write_every_code_pr_feedback_record(updated_feedback_record)
        return _json_response(
            start_response=start_response,
            status_code=202,
            payload=_accepted_payload(
                trace_id=trace_id,
                result={
                    "request_id": updated_feedback_record.request_id,
                    "feedback_id": updated_feedback_record.feedback_id,
                    "status": updated_feedback_record.status,
                },
                driver_result={"feedback": updated_feedback_record.model_dump(mode="json")},
            ),
        )
    if path == "/v1/every-code/work-requests/claim":
        claim_request = EveryCodeWorkRequestClaimEnvelope.model_validate(payload)
        claimed_record = every_code_store.claim_every_code_work_request_record(
            request_id=claim_request.request_id.strip(),
            host=claim_request.host.strip(),
            claimed_at=_utc_now_timestamp(),
        )
        if claimed_record is None:
            return _json_response(
                start_response=start_response,
                status_code=409,
                payload={
                    "status": "rejected",
                    "trace_id": trace_id,
                    "error": {
                        "code": "work_request_already_claimed",
                        "message": "Every Code work request is not queued for claim.",
                    },
                },
            )
        return _json_response(
            start_response=start_response,
            status_code=202,
            payload=_accepted_payload(
                trace_id=trace_id,
                result={"request_id": claimed_record.request_id, "state": claimed_record.state},
                driver_result={"request": claimed_record.model_dump(mode="json")},
            ),
        )
    if path == "/v1/every-code/work-requests/rerun":
        rerun_request = EveryCodeWorkRequestRerunEnvelope.model_validate(payload)
        existing_record = every_code_store.read_every_code_work_request_record(
            rerun_request.request_id.strip()
        )
        requeued_record = requeue_every_code_work_request(
            existing_record,
            queued_at=_utc_now_timestamp(),
            trigger_actor=rerun_request.trigger_actor,
        )
        every_code_store.write_every_code_work_request_record(requeued_record)
        return _json_response(
            start_response=start_response,
            status_code=202,
            payload=_accepted_payload(
                trace_id=trace_id,
                result={"request_id": requeued_record.request_id, "state": requeued_record.state},
                driver_result={"request": requeued_record.model_dump(mode="json")},
            ),
        )
    work_request_status_request = EveryCodeWorkRequestStatusEnvelope.model_validate(payload)
    existing_work_request_record = every_code_store.read_every_code_work_request_record(
        work_request_status_request.request_id.strip()
    )
    updated_work_request_record = apply_every_code_work_request_status(
        existing_work_request_record,
        EveryCodeWorkRequestStatusUpdate(
            state=work_request_status_request.state,
            host=work_request_status_request.host,
            updated_at=work_request_status_request.updated_at.strip() or _utc_now_timestamp(),
            result_pr_url=work_request_status_request.result_pr_url,
            result_summary=work_request_status_request.result_summary,
            error_message=work_request_status_request.error_message,
        ),
    )
    every_code_store.write_every_code_work_request_record(updated_work_request_record)
    return _json_response(
        start_response=start_response,
        status_code=202,
        payload=_accepted_payload(
            trace_id=trace_id,
            result={
                "request_id": updated_work_request_record.request_id,
                "state": updated_work_request_record.state,
            },
            driver_result={"request": updated_work_request_record.model_dump(mode="json")},
        ),
    )


def _session_identity(
    *, environ: dict[str, object], session_manager: HumanSessionManager | None
) -> GitHubHumanIdentity | None:
    if session_manager is None:
        return None
    session = session_manager.read_cookie(str(environ.get("HTTP_COOKIE", "")))
    return session.identity if session is not None else None


def _read_identity(
    *,
    environ: dict[str, object],
    verifier: TokenVerifier,
    session_manager: HumanSessionManager | None,
) -> LaunchplaneIdentity:
    human_identity = _session_identity(environ=environ, session_manager=session_manager)
    if human_identity is not None:
        return human_identity
    terminal_agent_identity = _terminal_agent_identity_from_bearer(environ)
    if terminal_agent_identity is not None:
        return terminal_agent_identity
    token = _bearer_token(environ)
    try:
        return verifier.verify(token)
    except ValueError as error:
        raise PermissionError(str(error)) from error


def _human_identity_payload(identity: GitHubHumanIdentity) -> dict[str, object]:
    return {
        "provider": "github",
        "login": identity.login,
        "github_id": identity.github_id,
        "name": identity.name,
        "email": identity.email,
        "organizations": sorted(identity.organizations),
        "teams": sorted(identity.teams),
        "role": identity.role,
    }


def _authz_diagnostic_payload(
    *,
    identity: LaunchplaneIdentity,
    authz_policy_sha256_value: str,
    authz_policy_source: str,
    action: str = "",
    product: str = "",
    context: str = "",
    decision: str = "denied",
    reason_code: str = "authorization_denied",
) -> dict[str, object]:
    if isinstance(identity, GitHubHumanIdentity):
        identity_payload: dict[str, object] = {
            "type": "github_human",
            "login": identity.login,
            "role": identity.role,
        }
    elif isinstance(identity, TerminalAgentIdentity):
        identity_payload = {
            "type": "terminal_agent",
            "subject": identity.subject,
            "token_label": identity.token_label,
        }
    else:
        identity_payload = {
            "type": "github_actions",
            "repository": identity.repository,
            "workflow_ref": identity.workflow_ref,
            "job_workflow_ref": identity.job_workflow_ref,
            "event_name": identity.event_name,
            "ref": identity.ref,
            "ref_type": identity.ref_type,
            "environment": identity.environment,
            "subject": identity.subject,
        }
    normalized_decision: AgentAuthzDecision = "allowed" if decision == "allowed" else "denied"
    audit = agent_authz_audit(
        identity=identity,
        action=action,
        product=product,
        context=context,
        decision=normalized_decision,
        reason_code=reason_code,
        policy_source=authz_policy_source,
        policy_sha256=authz_policy_sha256_value,
    )
    payload: dict[str, object] = {
        "identity": identity_payload,
        "agent_consumer": audit.subject.model_dump(mode="json"),
        "agent_audit": audit.model_dump(mode="json"),
        "policy_source": authz_policy_source,
        "policy_sha256": authz_policy_sha256_value,
    }
    if action or product or context:
        payload["request"] = {
            "action": action,
            "product": product,
            "context": context,
        }
    return payload


def _product_profile_context_cutover_allowed_contexts(
    profile: LaunchplaneProductProfileRecord,
) -> frozenset[str]:
    contexts = {profile.product.strip()}
    contexts.update(lane.context.strip() for lane in profile.lanes if lane.context.strip())
    if profile.preview.enabled and profile.preview.context.strip():
        contexts.add(profile.preview.context.strip())
    return frozenset(context for context in contexts if context)


def _product_profile_context_cutover_contexts_allowed(
    *,
    profile: LaunchplaneProductProfileRecord,
    source_context: str,
    target_context: str,
    preview_context: str,
) -> bool:
    allowed_contexts = _product_profile_context_cutover_allowed_contexts(profile)
    requested_contexts = {source_context.strip(), target_context.strip()}
    if preview_context.strip():
        requested_contexts.add(preview_context.strip())
    requested_contexts.discard("")
    return requested_contexts.issubset(allowed_contexts)


def _product_driver_compatible(
    *, profile: LaunchplaneProductProfileRecord, expected_driver_id: str
) -> bool:
    expected = expected_driver_id.strip()
    if profile.driver_id == expected:
        return True
    descriptor = read_driver_descriptor(profile.driver_id)
    return descriptor.base_driver_id == expected


def _find_product_profile_lane(
    *, profile: LaunchplaneProductProfileRecord, context: str, instance: str
) -> ProductLaneProfile | None:
    normalized_context = context.strip()
    normalized_instance = instance.strip()
    for lane in profile.lanes:
        lane_context = lane.context.strip()
        lane_instance = lane.instance.strip()
        if (not normalized_context or lane_context == normalized_context) and (
            not normalized_instance or lane_instance == normalized_instance
        ):
            return lane
    return None


def _resolve_product_driver_context(
    *,
    record_store: object,
    product: str,
    driver_id: str,
    context: str = "",
    instance: str = "",
    require_profile: bool = False,
) -> _ResolvedProductDriverContext:
    normalized_product = product.strip()
    normalized_driver_id = driver_id.strip()
    if normalized_product == normalized_driver_id and not require_profile:
        return _ResolvedProductDriverContext(profile=None)
    read_profile = getattr(record_store, "read_product_profile_record", None)
    if not callable(read_profile):
        raise ValueError("Product driver validation requires product profile storage.")
    profile = cast(LaunchplaneProductProfileRecord, read_profile(normalized_product))
    if not _product_driver_compatible(profile=profile, expected_driver_id=normalized_driver_id):
        raise ProductDriverMismatchError(
            "Product profile is not compatible with the requested driver route."
        )
    if context.strip() or instance.strip():
        lane = _find_product_profile_lane(profile=profile, context=context, instance=instance)
        if lane is None:
            raise ProductDriverMismatchError(
                "Product profile does not own the requested driver lane."
            )
        return _ResolvedProductDriverContext(profile=profile, lane=lane)
    return _ResolvedProductDriverContext(profile=profile)


def _resolve_descriptor_product_driver_context(
    *,
    record_store: object,
    route_path: str,
    product: str,
    context: str = "",
    instance: str = "",
    require_profile: bool = False,
) -> _ResolvedProductDriverContext:
    return _resolve_product_driver_context(
        record_store=record_store,
        product=product,
        driver_id=_driver_route_metadata_from_descriptors()[route_path].driver_id,
        context=context,
        instance=instance,
        require_profile=require_profile,
    )


def _safe_return_to(value: str) -> str:
    normalized = value.strip() or "/"
    if not normalized.startswith("/") or normalized.startswith("//"):
        return "/"
    return normalized


def _now_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _record_slug(value: str) -> str:
    compact = "".join(
        character.lower() if character.isalnum() else "-" for character in value.strip()
    )
    normalized = "-".join(part for part in compact.split("-") if part)
    return normalized or "launchplane-record"


def _build_every_code_work_request_record(
    request: EveryCodeWorkRequestCreateEnvelope, *, queued_at: str
) -> EveryCodeWorkRequestRecord:
    return EveryCodeWorkRequestRecord(
        request_id=build_every_code_work_request_id(
            repository=request.repository,
            issue_number=request.issue_number,
            trigger_label=request.trigger_label,
        ),
        source=request.source,
        state="queued",
        repository=request.repository.strip(),
        issue_number=request.issue_number,
        issue_url=request.issue_url.strip(),
        issue_title=request.issue_title.strip(),
        trigger_label=request.trigger_label.strip(),
        trigger_actor=request.trigger_actor.strip(),
        github_delivery_id=request.github_delivery_id.strip(),
        queued_at=queued_at,
        updated_at=queued_at,
    )


def _bootstrap_policy_source_from_env() -> str:
    if os.environ.get("LAUNCHPLANE_POLICY_TOML", "").strip():
        return "bootstrap-env:LAUNCHPLANE_POLICY_TOML"
    if os.environ.get("LAUNCHPLANE_POLICY_B64", "").strip():
        return "bootstrap-env:LAUNCHPLANE_POLICY_B64"
    if os.environ.get("LAUNCHPLANE_POLICY_FILE", "").strip():
        return "bootstrap-env:LAUNCHPLANE_POLICY_FILE"
    return "bootstrap-policy"


def _resolve_authz_policy(
    *,
    record_store: object,
    bootstrap_policy: LaunchplaneAuthzPolicy,
) -> tuple[LaunchplaneAuthzPolicy, str, str]:
    list_records = getattr(record_store, "list_authz_policy_records", None)
    if callable(list_records):
        records = list_records(status="active", limit=1)
        if records:
            record = records[0]
            return record.policy, record.policy_sha256, "db"

    policy_sha256 = authz_policy_sha256(bootstrap_policy)
    write_record = getattr(record_store, "write_authz_policy_record", None)
    if callable(write_record):
        updated_at = _now_timestamp()
        record = LaunchplaneAuthzPolicyRecord(
            record_id=build_authz_policy_record_id(
                updated_at=updated_at,
                policy_sha256=policy_sha256,
            ),
            status="active",
            source=_bootstrap_policy_source_from_env(),
            updated_at=updated_at,
            policy_sha256=policy_sha256,
            policy=bootstrap_policy,
        )
        write_record(record)
        return record.policy, record.policy_sha256, "bootstrap_seeded_store"

    return bootstrap_policy, policy_sha256, "bootstrap"


def _launchplane_policy_sha256_from_env() -> str:
    policy_toml = os.environ.get("LAUNCHPLANE_POLICY_TOML", "").strip()
    if policy_toml:
        return hashlib.sha256(policy_toml.encode("utf-8")).hexdigest()

    policy_b64 = os.environ.get("LAUNCHPLANE_POLICY_B64", "").strip()
    if policy_b64:
        try:
            policy_bytes = base64.b64decode(policy_b64, validate=True)
        except Exception:
            return ""
        return hashlib.sha256(policy_bytes).hexdigest()

    policy_file = os.environ.get("LAUNCHPLANE_POLICY_FILE", "").strip()
    if not policy_file:
        return ""
    try:
        return hashlib.sha256(Path(policy_file).read_bytes()).hexdigest()
    except OSError:
        return ""


def _launchplane_runtime_payload(
    *, storage_backend: str, authz_policy_sha256_value: str, authz_policy_source: str
) -> dict[str, object]:
    return {
        "authz_policy_sha256": authz_policy_sha256_value,
        "authz_policy_source": authz_policy_source,
        "bootstrap_authz_policy_sha256": _launchplane_policy_sha256_from_env(),
        "docker_image_reference": os.environ.get(_LAUNCHPLANE_IMAGE_REFERENCE_ENV_KEY, "").strip(),
        "service_audience": os.environ.get("LAUNCHPLANE_SERVICE_AUDIENCE", "").strip(),
        "storage_backend": storage_backend,
    }


def _request_launchplane_self_deploy(
    *,
    control_plane_root_path: Path,
    request: LaunchplaneSelfDeployRequest,
) -> dict[str, object]:
    host, token = control_plane_dokploy.read_dokploy_config(
        control_plane_root=control_plane_root_path
    )
    target_payload = control_plane_dokploy.fetch_dokploy_target_payload(
        host=host,
        token=token,
        target_type=request.target_type,
        target_id=request.target_id,
    )
    raw_env_text = str(target_payload.get("env") or "")
    previous_env_map = control_plane_dokploy.parse_dokploy_env_text(raw_env_text)
    updates = {_LAUNCHPLANE_IMAGE_REFERENCE_ENV_KEY: request.image_reference}
    updates.update(request.oauth_env)
    removals: tuple[str, ...] = ()
    if request.policy_b64:
        updates["LAUNCHPLANE_POLICY_B64"] = request.policy_b64
        removals = ("LAUNCHPLANE_POLICY_TOML", "LAUNCHPLANE_POLICY_FILE")
    updated_env_text = control_plane_dokploy.render_dokploy_env_text_with_overrides(
        raw_env_text,
        updates=updates,
        removals=removals,
    )
    if updated_env_text != raw_env_text:
        control_plane_dokploy.update_dokploy_target_env(
            host=host,
            token=token,
            target_type=request.target_type,
            target_id=request.target_id,
            target_payload=target_payload,
            env_text=updated_env_text,
        )
    control_plane_dokploy.trigger_deployment(
        host=host,
        token=token,
        target_type=request.target_type,
        target_id=request.target_id,
        no_cache=request.no_cache,
    )
    return {
        "target_type": request.target_type,
        "target_id": request.target_id,
        "image_reference": request.image_reference,
        "image_reference_changed": previous_env_map.get(_LAUNCHPLANE_IMAGE_REFERENCE_ENV_KEY, "")
        != request.image_reference,
        "authz_policy_changed": bool(request.policy_b64)
        and previous_env_map.get("LAUNCHPLANE_POLICY_B64", "") != request.policy_b64,
        "authz_policy_sha256": (
            hashlib.sha256(base64.b64decode(request.policy_b64, validate=True)).hexdigest()
            if request.policy_b64
            else ""
        ),
        "oauth_env_keys_changed": sorted(
            env_key
            for env_key in request.oauth_env
            if previous_env_map.get(env_key, "") != request.oauth_env[env_key]
        ),
    }


def _write_preview_inventory_scan_if_supported(
    *,
    record_store: object,
    context: str,
    source: str,
    preview_slugs: tuple[str, ...],
) -> str:
    if not hasattr(record_store, "write_preview_inventory_scan_record"):
        return ""
    scanned_at = _utc_now_timestamp()
    scan_id = build_preview_inventory_scan_id(
        context_name=context,
        scanned_at=scanned_at,
    )
    getattr(record_store, "write_preview_inventory_scan_record")(
        PreviewInventoryScanRecord(
            scan_id=scan_id,
            context=context,
            scanned_at=scanned_at,
            source=source,
            status="pass",
            preview_count=len(preview_slugs),
            preview_slugs=preview_slugs,
        )
    )
    return scan_id


def _write_preview_desired_state_if_supported(
    *, record_store: object, record: PreviewDesiredStateRecord
) -> str:
    if not hasattr(record_store, "write_preview_desired_state_record"):
        return ""
    getattr(record_store, "write_preview_desired_state_record")(record)
    return record.desired_state_id


def _latest_preview_inventory_scan(
    *, record_store: object, context_name: str
) -> PreviewInventoryScanRecord | None:
    if not hasattr(record_store, "list_preview_inventory_scan_records"):
        return None
    scans = getattr(record_store, "list_preview_inventory_scan_records")(
        context_name=context_name,
        limit=1,
    )
    return next(iter(scans), None)


def _write_preview_lifecycle_plan_if_supported(
    *, record_store: object, record: PreviewLifecyclePlanRecord
) -> str:
    if not hasattr(record_store, "write_preview_lifecycle_plan_record"):
        return ""
    getattr(record_store, "write_preview_lifecycle_plan_record")(record)
    return record.plan_id


def _latest_preview_lifecycle_plan(
    *, record_store: object, context_name: str, plan_id: str
) -> PreviewLifecyclePlanRecord | None:
    if not hasattr(record_store, "list_preview_lifecycle_plan_records"):
        return None
    records = getattr(record_store, "list_preview_lifecycle_plan_records")(
        context_name=context_name,
        limit=None,
    )
    return next((record for record in records if record.plan_id == plan_id), None)


def _write_preview_lifecycle_cleanup_if_supported(
    *, record_store: object, record: PreviewLifecycleCleanupRecord
) -> str:
    if not hasattr(record_store, "write_preview_lifecycle_cleanup_record"):
        return ""
    getattr(record_store, "write_preview_lifecycle_cleanup_record")(record)
    return record.cleanup_id


def _write_preview_pr_feedback_if_supported(
    *, record_store: object, record: PreviewPrFeedbackRecord
) -> str:
    if not hasattr(record_store, "write_preview_pr_feedback_record"):
        return ""
    getattr(record_store, "write_preview_pr_feedback_record")(record)
    return record.feedback_id


def _repo_token(value: str) -> str:
    normalized = "".join(
        character if character.isalnum() else "-" for character in value.strip().lower()
    ).strip("-")
    while "--" in normalized:
        normalized = normalized.replace("--", "-")
    if not normalized:
        raise ValueError("repository token is required")
    return normalized


def _verireel_preview_manifest_fingerprint(request: VeriReelPreviewRefreshRequest) -> str:
    normalized_sha = request.anchor_head_sha.strip().lower()
    short_sha = normalized_sha[:7]
    return (
        f"{_repo_token(request.anchor_repo)}-preview-manifest-"
        f"{request.preview_slug.strip()}-{short_sha}"
    )


def _image_reference_tail(value: str) -> str:
    normalized = value.strip()
    if not normalized:
        return ""
    for separator in ("@", ":"):
        if separator in normalized:
            normalized = normalized.rsplit(separator, maxsplit=1)[1]
    return normalized.strip()


def _generic_web_preview_manifest_fingerprint(
    request: GenericWebPreviewRefreshRequest,
) -> str:
    artifact_token = _repo_token(_image_reference_tail(request.image_reference) or "image")
    return f"{_repo_token(request.preview_slug)}-{artifact_token}"


def _generic_web_preview_anchor_pr_number(
    *,
    request: GenericWebPreviewRefreshRequest,
    profile: LaunchplaneProductProfileRecord,
) -> int:
    if request.anchor_pr_number is not None:
        return request.anchor_pr_number
    pr_number = preview_pr_number_from_slug(
        preview_slug=request.preview_slug,
        slug_template=profile.preview.slug_template,
    )
    if pr_number is None:
        raise click.ClickException(
            "Generic web preview refresh requires anchor_pr_number when preview_slug does not match the profile slug_template."
        )
    return pr_number


def _generic_web_preview_anchor_pr_url(
    *,
    request: GenericWebPreviewRefreshRequest,
    profile: LaunchplaneProductProfileRecord,
    anchor_pr_number: int,
) -> str:
    if request.anchor_pr_url.strip():
        return request.anchor_pr_url.strip()
    return f"https://github.com/{profile.repository.strip()}/pull/{anchor_pr_number}"


def _generic_web_preview_anchor_repo(profile: LaunchplaneProductProfileRecord) -> str:
    _owner, separator, repo = profile.repository.strip().partition("/")
    if not separator or not repo.strip():
        raise click.ClickException(
            "Generic web preview profile repository must use owner/repo format."
        )
    return repo.strip()


def _generic_web_preview_anchor_head_sha(request: GenericWebPreviewRefreshRequest) -> str:
    if request.anchor_head_sha.strip():
        return request.anchor_head_sha.strip()
    return _image_reference_tail(request.image_reference) or request.image_reference.strip()


def _apply_generic_web_preview_refresh_records(
    *,
    control_plane_root_path: Path,
    record_store: object,
    request: GenericWebPreviewRefreshRequest,
    driver_result: GenericWebPreviewRefreshResult,
    profile: LaunchplaneProductProfileRecord,
) -> dict[str, object]:
    if driver_result.refresh_status == "blocked":
        return {}
    anchor_pr_number = _generic_web_preview_anchor_pr_number(
        request=request,
        profile=profile,
    )
    requested_at = (
        driver_result.refresh_started_at.strip() or driver_result.refresh_finished_at.strip()
    )
    finished_at = driver_result.refresh_finished_at.strip() or requested_at
    refresh_passed = driver_result.refresh_status == "pass"
    failure_summary = driver_result.error_message.strip() or "Preview provisioning failed."
    preview_request = PreviewMutationRequest(
        context=profile.preview.context,
        anchor_repo=_generic_web_preview_anchor_repo(profile),
        anchor_pr_number=anchor_pr_number,
        anchor_pr_url=_generic_web_preview_anchor_pr_url(
            request=request,
            profile=profile,
            anchor_pr_number=anchor_pr_number,
        ),
        canonical_url=driver_result.preview_url.strip() or request.preview_url.strip(),
        state="pending" if refresh_passed else "failed",
        created_at=requested_at,
        updated_at=finished_at,
        eligible_at=requested_at,
    )
    generation_request = PreviewGenerationMutationRequest(
        context=profile.preview.context,
        anchor_repo=preview_request.anchor_repo,
        anchor_pr_number=anchor_pr_number,
        anchor_pr_url=preview_request.anchor_pr_url,
        anchor_head_sha=_generic_web_preview_anchor_head_sha(request),
        state="verifying" if refresh_passed else "failed",
        requested_reason="external_preview_refresh",
        requested_at=requested_at,
        started_at=requested_at,
        finished_at="" if refresh_passed else finished_at,
        failed_at="" if refresh_passed else finished_at,
        resolved_manifest_fingerprint=_generic_web_preview_manifest_fingerprint(request),
        artifact_id=request.image_reference,
        deploy_status="pass" if refresh_passed else "fail",
        verify_status="pending" if refresh_passed else "skipped",
        overall_health_status="pending" if refresh_passed else "fail",
        failure_stage="" if refresh_passed else "provision",
        failure_summary="" if refresh_passed else failure_summary,
    )
    typed_record_store = cast(FilesystemRecordStore, record_store)
    return apply_launchplane_generation_evidence(
        control_plane_root_path=control_plane_root_path,
        record_store=typed_record_store,
        preview_request=preview_request,
        generation_request=generation_request,
    )


def _apply_verireel_preview_refresh_records(
    *,
    control_plane_root_path: Path,
    record_store: object,
    request: VeriReelPreviewRefreshRequest,
    driver_result: VeriReelPreviewRefreshResult,
) -> dict[str, object]:
    requested_at = (
        driver_result.refresh_started_at.strip() or driver_result.refresh_finished_at.strip()
    )
    finished_at = driver_result.refresh_finished_at.strip() or requested_at
    preview_url = driver_result.preview_url.strip() or request.preview_url.strip()
    refresh_passed = driver_result.refresh_status == "pass"
    failure_summary = driver_result.error_message.strip() or "Preview provisioning failed."
    preview_request = PreviewMutationRequest(
        context=request.context,
        anchor_repo=request.anchor_repo,
        anchor_pr_number=request.anchor_pr_number,
        anchor_pr_url=request.anchor_pr_url,
        canonical_url=preview_url,
        state="pending" if refresh_passed else "failed",
        created_at=requested_at,
        updated_at=finished_at,
        eligible_at=requested_at,
    )
    generation_request = PreviewGenerationMutationRequest(
        context=request.context,
        anchor_repo=request.anchor_repo,
        anchor_pr_number=request.anchor_pr_number,
        anchor_pr_url=request.anchor_pr_url,
        anchor_head_sha=request.anchor_head_sha,
        state="verifying" if refresh_passed else "failed",
        requested_reason="external_preview_refresh",
        requested_at=requested_at,
        started_at=requested_at,
        finished_at="" if refresh_passed else finished_at,
        failed_at="" if refresh_passed else finished_at,
        resolved_manifest_fingerprint=_verireel_preview_manifest_fingerprint(request),
        artifact_id=request.image_reference,
        deploy_status="pass" if refresh_passed else "fail",
        verify_status="pending" if refresh_passed else "skipped",
        overall_health_status="pending" if refresh_passed else "fail",
        failure_stage="" if refresh_passed else "provision",
        failure_summary="" if refresh_passed else failure_summary,
    )
    typed_record_store = cast(FilesystemRecordStore, record_store)
    return apply_launchplane_generation_evidence(
        control_plane_root_path=control_plane_root_path,
        record_store=typed_record_store,
        preview_request=preview_request,
        generation_request=generation_request,
    )


def _apply_verireel_preview_destroy_records(
    *,
    record_store: object,
    request: VeriReelPreviewDestroyRequest,
    driver_result: VeriReelPreviewDestroyResult,
) -> dict[str, object]:
    if driver_result.destroy_status != "pass":
        return {"transition": "destroy_failed"}
    typed_record_store = cast(FilesystemRecordStore, record_store)
    try:
        return apply_launchplane_destroy_preview(
            record_store=typed_record_store,
            request=PreviewDestroyMutationRequest(
                context=request.context,
                anchor_repo=request.anchor_repo,
                anchor_pr_number=request.anchor_pr_number,
                destroyed_at=(
                    driver_result.destroy_finished_at.strip()
                    or driver_result.destroy_started_at.strip()
                    or _utc_now_timestamp()
                ),
                destroy_reason=request.destroy_reason,
            ),
        )
    except click.ClickException as error:
        if str(error).startswith("No Launchplane preview found"):
            return {"transition": "destroyed_missing_preview"}
        raise


def _apply_verireel_preview_verification_records(
    *,
    control_plane_root_path: Path,
    record_store: object,
    request: VeriReelPreviewVerificationRequest,
) -> dict[str, object]:
    typed_record_store = cast(FilesystemRecordStore, record_store)
    preview = find_preview_record(
        record_store=typed_record_store,
        context_name=request.context,
        anchor_repo=request.anchor_repo,
        anchor_pr_number=request.anchor_pr_number,
    )
    if preview is None:
        raise click.ClickException(
            f"No Launchplane preview found for {request.context}/{request.anchor_repo}/pr-{request.anchor_pr_number}."
        )
    generation_id = preview.latest_generation_id or preview.active_generation_id
    if not generation_id:
        raise click.ClickException(
            f"No Launchplane preview generation found for {preview.preview_id}."
        )
    generation = typed_record_store.read_preview_generation_record(generation_id)
    verified_at = request.verified_at.strip()
    verification_passed = request.verification_status.strip() == "pass"
    failure_summary = request.failure_summary.strip() or "Preview E2E verification failed."
    return apply_launchplane_generation_evidence(
        control_plane_root_path=control_plane_root_path,
        record_store=typed_record_store,
        preview_request=PreviewMutationRequest(
            context=preview.context,
            anchor_repo=preview.anchor_repo,
            anchor_pr_number=preview.anchor_pr_number,
            anchor_pr_url=preview.anchor_pr_url,
            canonical_url=preview.canonical_url,
            state="active" if verification_passed else "failed",
            created_at=preview.created_at,
            updated_at=verified_at,
            eligible_at=preview.eligible_at,
        ),
        generation_request=PreviewGenerationMutationRequest(
            context=preview.context,
            anchor_repo=preview.anchor_repo,
            anchor_pr_number=preview.anchor_pr_number,
            anchor_pr_url=preview.anchor_pr_url,
            anchor_head_sha=generation.anchor_summary.head_sha,
            sequence=generation.sequence,
            generation_id=generation.generation_id,
            state="ready" if verification_passed else "failed",
            requested_reason=generation.requested_reason,
            requested_at=generation.requested_at,
            started_at=generation.started_at,
            ready_at=verified_at if verification_passed else "",
            finished_at=verified_at,
            failed_at="" if verification_passed else verified_at,
            resolved_manifest_fingerprint=generation.resolved_manifest_fingerprint,
            artifact_id=generation.artifact_id,
            baseline_release_tuple_id=generation.baseline_release_tuple_id,
            source_map=generation.source_map,
            companion_summaries=generation.companion_summaries,
            deploy_status=generation.deploy_status,
            verify_status="pass" if verification_passed else "fail",
            overall_health_status="pass" if verification_passed else "fail",
            failure_stage="" if verification_passed else "verify",
            failure_summary="" if verification_passed else failure_summary,
        ),
    )


def _testing_post_deploy_detail(status: ReleaseStatus) -> str:
    if status == "pass":
        return "Prisma migrations completed on testing."
    if status == "fail":
        return "Prisma migrations failed on testing."
    return ""


def _testing_destination_health_status(
    *,
    deployment_record: DeploymentRecord,
    request: VeriReelTestingVerificationRequest,
) -> ReleaseStatus:
    statuses = (
        deployment_record.destination_health.status,
        request.verification_status,
        request.owner_routes_status,
    )
    if any(status == "fail" for status in statuses):
        return "fail"
    if all(status == "pass" for status in statuses):
        return "pass"
    if any(status == "pending" for status in statuses):
        return "pending"
    return "skipped"


def _updated_testing_destination_health(
    *,
    deployment_record: DeploymentRecord,
    status: ReleaseStatus,
) -> HealthcheckEvidence:
    if status in {"pass", "fail"} and deployment_record.destination_health.urls:
        return deployment_record.destination_health.model_copy(update={"status": status})
    return HealthcheckEvidence(status=status)


def _apply_verireel_testing_verification_records(
    *,
    record_store: object,
    request: VeriReelTestingVerificationRequest,
) -> dict[str, str]:
    typed_record_store = cast(FilesystemRecordStore, record_store)
    try:
        deployment_record = typed_record_store.read_deployment_record(request.deployment_record_id)
    except FileNotFoundError as exc:
        raise click.ClickException(
            f"No Launchplane deployment record found for {request.deployment_record_id}."
        ) from exc
    if deployment_record.context != request.context:
        raise click.ClickException(
            "Testing verification context does not match deployment record context."
        )
    if deployment_record.instance != request.instance:
        raise click.ClickException(
            "Testing verification instance does not match deployment record instance."
        )

    destination_health_status = _testing_destination_health_status(
        deployment_record=deployment_record,
        request=request,
    )
    updated_record = deployment_record.model_copy(
        update={
            "post_deploy_update": PostDeployUpdateEvidence(
                attempted=request.migration_status != "skipped",
                status=request.migration_status,
                detail=_testing_post_deploy_detail(request.migration_status),
            ),
            "destination_health": _updated_testing_destination_health(
                deployment_record=deployment_record,
                status=destination_health_status,
            ),
        }
    )
    result = apply_deployment_evidence(
        record_store=typed_record_store,
        deployment_record=updated_record,
    )
    result["deployment_health_status"] = destination_health_status
    result["post_deploy_status"] = request.migration_status
    return result


def _allows_preview_pr_feedback_write(
    *,
    authz_policy: LaunchplaneAuthzPolicy,
    identity: LaunchplaneIdentity,
    product: str,
    context: str,
    status: PreviewPrFeedbackStatus,
) -> bool:
    if authz_policy.allows(
        identity=identity,
        action="preview_pr_feedback.write",
        product=product,
        context=context,
    ):
        return True

    lifecycle_actions_by_status = {
        "pending": ("preview_refresh.execute",),
        "ready": ("preview_refresh.execute",),
        "failed": ("preview_refresh.execute",),
        "destroyed": ("preview_destroy.execute",),
        "cleanup_failed": ("preview_destroy.execute",),
        "cleared": ("preview_refresh.execute", "preview_destroy.execute"),
    }
    return any(
        authz_policy.allows(
            identity=identity,
            action=action,
            product=product,
            context=context,
        )
        for action in lifecycle_actions_by_status.get(status, ())
    )


def create_launchplane_service_app(
    *,
    state_dir: Path,
    verifier: TokenVerifier,
    authz_policy: LaunchplaneAuthzPolicy,
    control_plane_root_path: Path | None = None,
    database_url: str | None = None,
    github_oauth_config: GitHubOAuthConfig | None = None,
    github_oauth_client: GitHubOAuthClient | None = None,
    human_session_store: HumanSessionStore | None = None,
    work_graph_planning_facts_provider: WorkGraphPlanningFactsProvider | None = None,
) -> _WsgiApp:
    resolved_root = control_plane_root_path or control_plane_root()
    ui_static_root = resolved_root / "control_plane" / "ui_static"
    record_store = build_record_store(state_dir=state_dir, database_url=database_url)
    storage_backend = storage_backend_name(record_store)
    authz_policy, resolved_authz_policy_sha256, resolved_authz_policy_source = (
        _resolve_authz_policy(record_store=record_store, bootstrap_policy=authz_policy)
    )
    resolved_github_oauth_config = github_oauth_config or load_github_oauth_config_from_env()
    oauth_login_states = OAuthLoginStateStore()
    session_manager = (
        HumanSessionManager(
            config=resolved_github_oauth_config,
            session_store=(
                human_session_store
                or _human_session_capable_store(record_store)
                or InMemoryHumanSessionStore()
            ),
        )
        if resolved_github_oauth_config is not None
        else None
    )
    resolved_github_oauth_client = (
        github_oauth_client
        if github_oauth_client is not None
        else (
            GitHubOAuthClient(resolved_github_oauth_config)
            if resolved_github_oauth_config is not None
            else None
        )
    )
    write_routes = _build_write_routes()

    def app(
        environ: dict[str, object],
        start_response: _StartResponse,
    ) -> list[bytes]:
        nonlocal authz_policy, resolved_authz_policy_sha256, resolved_authz_policy_source

        request_trace_id = _trace_id()
        method = str(environ.get("REQUEST_METHOD", "GET")).upper()
        path = str(environ.get("PATH_INFO", ""))
        query = parse_qs(str(environ.get("QUERY_STRING", "")), keep_blank_values=True)
        if method == "GET" and path == "/auth/github/login":
            if resolved_github_oauth_config is None or resolved_github_oauth_client is None:
                return _json_response(
                    start_response=start_response,
                    status_code=503,
                    payload={
                        "status": "rejected",
                        "trace_id": request_trace_id,
                        "error": {
                            "code": "auth_not_configured",
                            "message": "GitHub OAuth is not configured for Launchplane.",
                        },
                    },
                )
            state = secrets.token_urlsafe(32)
            code_verifier, code_challenge = build_pkce_verifier()
            return_to = _safe_return_to((query.get("return_to") or ["/"])[0])
            oauth_login_states.put(
                state=state,
                code_verifier=code_verifier,
                return_to=return_to,
            )
            return _redirect_response(
                start_response=start_response,
                location=resolved_github_oauth_client.authorization_url(
                    state=state,
                    code_challenge=code_challenge,
                ),
            )
        if method == "GET" and path == "/auth/github/callback":
            if session_manager is None or resolved_github_oauth_client is None:
                return _json_response(
                    start_response=start_response,
                    status_code=503,
                    payload={
                        "status": "rejected",
                        "trace_id": request_trace_id,
                        "error": {
                            "code": "auth_not_configured",
                            "message": "GitHub OAuth is not configured for Launchplane.",
                        },
                    },
                )
            code = str((query.get("code") or [""])[0]).strip()
            state = str((query.get("state") or [""])[0]).strip()
            login_state = oauth_login_states.pop(state)
            if not code or login_state is None:
                return _json_response(
                    start_response=start_response,
                    status_code=400,
                    payload={
                        "status": "rejected",
                        "trace_id": request_trace_id,
                        "error": {
                            "code": "invalid_oauth_callback",
                            "message": "GitHub OAuth callback is missing a valid code or state.",
                        },
                    },
                )
            try:
                human_identity = resolved_github_oauth_client.fetch_identity(
                    code=code,
                    code_verifier=login_state.code_verifier,
                    authz_policy=authz_policy,
                )
            except PermissionError:
                return _json_response(
                    start_response=start_response,
                    status_code=403,
                    payload={
                        "status": "rejected",
                        "trace_id": request_trace_id,
                        "error": {
                            "code": "authorization_denied",
                            "message": "GitHub identity is not authorized for Launchplane.",
                        },
                    },
                )
            except Exception:  # noqa: BLE001
                _LOGGER.exception(
                    "GitHub OAuth callback failed", extra={"trace_id": request_trace_id}
                )
                return _json_response(
                    start_response=start_response,
                    status_code=400,
                    payload={
                        "status": "rejected",
                        "trace_id": request_trace_id,
                        "error": {
                            "code": "invalid_oauth_callback",
                            "message": "GitHub OAuth callback could not be completed.",
                        },
                    },
                )
            session = session_manager.issue(human_identity)
            return _redirect_response(
                start_response=start_response,
                location=login_state.return_to,
                headers=[("Set-Cookie", session_manager.session_cookie_header(session))],
            )
        if method == "POST" and path == "/auth/logout":
            if session_manager is not None:
                session_manager.delete_cookie_session(str(environ.get("HTTP_COOKIE", "")))
                clear_cookie = session_manager.clear_cookie_header()
            else:
                clear_cookie = "launchplane_session=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax"
            return _json_response(
                start_response=start_response,
                status_code=200,
                payload={"status": "ok", "trace_id": request_trace_id},
                headers=[("Set-Cookie", clear_cookie)],
            )
        if method == "GET" and path == "/v1/auth/session":
            session_identity = _session_identity(environ=environ, session_manager=session_manager)
            if session_identity is None:
                return _json_response(
                    start_response=start_response,
                    status_code=401,
                    payload={
                        "status": "rejected",
                        "trace_id": request_trace_id,
                        "configured": resolved_github_oauth_config is not None,
                        "error": {
                            "code": "authentication_required",
                            "message": "Sign in with GitHub to access Launchplane.",
                        },
                    },
                )
            return _json_response(
                start_response=start_response,
                status_code=200,
                payload={
                    "status": "ok",
                    "trace_id": request_trace_id,
                    "identity": _human_identity_payload(session_identity),
                },
            )
        if method == "GET" and (path == "/" or path == "/ui" or path.startswith("/ui/")):
            return _serve_ui_route(
                start_response=start_response,
                trace_id=request_trace_id,
                path=path,
                ui_static_root=ui_static_root,
            )
        if method == "GET" and path == "/v1/health":
            return _json_response(
                start_response=start_response,
                status_code=200,
                payload={
                    "status": "ok",
                    "trace_id": request_trace_id,
                    "storage_backend": storage_backend,
                },
            )
        read_route = _match_read_route(path)
        if path not in write_routes and read_route is None:
            return _not_found_response(
                start_response=start_response,
                trace_id=request_trace_id,
                path=path,
            )
        if method not in {"GET", "POST"}:
            return _json_response(
                start_response=start_response,
                status_code=405,
                payload={
                    "status": "rejected",
                    "trace_id": request_trace_id,
                    "error": {
                        "code": "method_not_allowed",
                        "message": "Only GET and POST are allowed for Launchplane routes.",
                    },
                },
            )
        if method == "GET" and read_route is None:
            return _json_response(
                start_response=start_response,
                status_code=405,
                payload={
                    "status": "rejected",
                    "trace_id": request_trace_id,
                    "error": {
                        "code": "method_not_allowed",
                        "message": "Only POST is allowed for this Launchplane route.",
                    },
                },
            )
        if method == "POST" and path not in write_routes:
            return _json_response(
                start_response=start_response,
                status_code=405,
                payload={
                    "status": "rejected",
                    "trace_id": request_trace_id,
                    "error": {
                        "code": "method_not_allowed",
                        "message": "Only GET is allowed for this Launchplane route.",
                    },
                },
            )
        if method == "POST" and path == _EVERY_CODE_GITHUB_WEBHOOK_ROUTE:
            try:
                return _handle_every_code_github_webhook(
                    environ=environ,
                    start_response=start_response,
                    trace_id=request_trace_id,
                    record_store=record_store,
                    control_plane_root_path=resolved_root,
                )
            except ValueError:
                return _json_response(
                    start_response=start_response,
                    status_code=400,
                    payload={
                        "status": "rejected",
                        "trace_id": request_trace_id,
                        "error": {
                            "code": "invalid_request",
                            "message": "GitHub webhook payload is invalid.",
                        },
                    },
                )
        if _every_code_worker_token_authorized(environ=environ, method=method, path=path):
            if method == "GET":
                try:
                    return _handle_every_code_work_request_read(
                        start_response=start_response,
                        trace_id=request_trace_id,
                        record_store=record_store,
                        path=path,
                        query=query,
                    )
                except ValueError as error:
                    return _json_response(
                        start_response=start_response,
                        status_code=400,
                        payload={
                            "status": "rejected",
                            "trace_id": request_trace_id,
                            "error": {
                                "code": "invalid_payload",
                                "message": str(error),
                            },
                        },
                    )
            payload = _read_json_request(environ)
            try:
                return _handle_every_code_worker_write(
                    start_response=start_response,
                    trace_id=request_trace_id,
                    record_store=record_store,
                    path=path,
                    payload=payload,
                )
            except ValueError as error:
                return _json_response(
                    start_response=start_response,
                    status_code=400,
                    payload={
                        "status": "rejected",
                        "trace_id": request_trace_id,
                        "error": {
                            "code": "invalid_payload",
                            "message": str(error),
                        },
                    },
                )
        try:
            if method == "GET":
                identity = _read_identity(
                    environ=environ,
                    verifier=verifier,
                    session_manager=session_manager,
                )
            else:
                if (
                    path in _HUMAN_IDENTITY_MUTATION_ROUTES
                    or path in _HUMAN_IDENTITY_READ_MODEL_POST_ROUTES
                ):
                    identity = _read_identity(
                        environ=environ,
                        verifier=verifier,
                        session_manager=session_manager,
                    )
                else:
                    token = _bearer_token(environ)
                    identity = verifier.verify(token)
                    if not isinstance(identity, GitHubActionsIdentity):
                        raise PermissionError("Mutation routes require GitHub Actions OIDC.")
            if (
                isinstance(identity, TerminalAgentIdentity)
                and method != "GET"
                and path != "/v1/agent/write-intents/evaluate"
            ):
                return _json_response(
                    start_response=start_response,
                    status_code=403,
                    payload={
                        "status": "rejected",
                        "trace_id": request_trace_id,
                        "error": {
                            "code": "authorization_denied",
                            "message": (
                                "Terminal agent credentials can only read redacted "
                                "Launchplane context."
                            ),
                        },
                    },
                )
            if method == "GET":
                assert read_route is not None
                action, params = read_route
                if action == "driver.read":
                    context_name = params.get("context", _LAUNCHPLANE_SERVICE_CONTEXT)
                    if not authz_policy.allows(
                        identity=identity,
                        action=action,
                        product="launchplane",
                        context=context_name,
                    ):
                        return _json_response(
                            start_response=start_response,
                            status_code=403,
                            payload={
                                "status": "rejected",
                                "trace_id": request_trace_id,
                                "error": {
                                    "code": "authorization_denied",
                                    "message": "Workflow cannot read driver metadata for the requested context.",
                                },
                            },
                        )
                    if "context" in params:
                        view = build_driver_context_view(
                            record_store=record_store,
                            context_name=context_name,
                            instance_name=params.get("instance", ""),
                        )
                        return _json_response(
                            start_response=start_response,
                            status_code=200,
                            payload={
                                "status": "ok",
                                "trace_id": request_trace_id,
                                "view": view.model_dump(mode="json"),
                            },
                        )
                    if "driver_id" in params:
                        descriptor = read_driver_descriptor(params["driver_id"])
                        return _json_response(
                            start_response=start_response,
                            status_code=200,
                            payload={
                                "status": "ok",
                                "trace_id": request_trace_id,
                                "driver": descriptor.model_dump(mode="json"),
                            },
                        )
                    descriptors = list_driver_descriptors()
                    return _json_response(
                        start_response=start_response,
                        status_code=200,
                        payload={
                            "status": "ok",
                            "trace_id": request_trace_id,
                            "drivers": [
                                descriptor.model_dump(mode="json") for descriptor in descriptors
                            ],
                        },
                    )
                if action == "deployment.read":
                    deployment = record_store.read_deployment_record(params["record_id"])
                    if not authz_policy.allows(
                        identity=identity,
                        action=action,
                        product="launchplane",
                        context=deployment.context,
                    ):
                        return _json_response(
                            start_response=start_response,
                            status_code=403,
                            payload={
                                "status": "rejected",
                                "trace_id": request_trace_id,
                                "error": {
                                    "code": "authorization_denied",
                                    "message": "Workflow cannot read deployment records for the requested context.",
                                },
                            },
                        )
                    return _json_response(
                        start_response=start_response,
                        status_code=200,
                        payload={
                            "status": "ok",
                            "trace_id": request_trace_id,
                            "record": deployment.model_dump(mode="json"),
                        },
                    )
                if action == "promotion.read":
                    promotion = record_store.read_promotion_record(params["record_id"])
                    if not authz_policy.allows(
                        identity=identity,
                        action=action,
                        product="launchplane",
                        context=promotion.context,
                    ):
                        return _json_response(
                            start_response=start_response,
                            status_code=403,
                            payload={
                                "status": "rejected",
                                "trace_id": request_trace_id,
                                "error": {
                                    "code": "authorization_denied",
                                    "message": "Workflow cannot read promotion records for the requested context.",
                                },
                            },
                        )
                    return _json_response(
                        start_response=start_response,
                        status_code=200,
                        payload={
                            "status": "ok",
                            "trace_id": request_trace_id,
                            "record": promotion.model_dump(mode="json"),
                        },
                    )
                if action == "inventory.read":
                    inventory = record_store.read_environment_inventory(
                        context_name=params["context"],
                        instance_name=params["instance"],
                    )
                    if not authz_policy.allows(
                        identity=identity,
                        action=action,
                        product="launchplane",
                        context=inventory.context,
                    ):
                        return _json_response(
                            start_response=start_response,
                            status_code=403,
                            payload={
                                "status": "rejected",
                                "trace_id": request_trace_id,
                                "error": {
                                    "code": "authorization_denied",
                                    "message": "Workflow cannot read inventory for the requested context.",
                                },
                            },
                        )
                    return _json_response(
                        start_response=start_response,
                        status_code=200,
                        payload={
                            "status": "ok",
                            "trace_id": request_trace_id,
                            "record": inventory.model_dump(mode="json"),
                        },
                    )
                if action == "preview.read":
                    preview = record_store.read_preview_record(params["preview_id"])
                    if not authz_policy.allows(
                        identity=identity,
                        action=action,
                        product="launchplane",
                        context=preview.context,
                    ):
                        return _json_response(
                            start_response=start_response,
                            status_code=403,
                            payload={
                                "status": "rejected",
                                "trace_id": request_trace_id,
                                "error": {
                                    "code": "authorization_denied",
                                    "message": "Workflow cannot read previews for the requested context.",
                                },
                            },
                        )
                    if params.get("include_history") == "true":
                        generations = record_store.list_preview_generation_records(
                            preview_id=preview.preview_id
                        )
                        return _json_response(
                            start_response=start_response,
                            status_code=200,
                            payload={
                                "status": "ok",
                                "trace_id": request_trace_id,
                                "preview": preview.model_dump(mode="json"),
                                "generations": [
                                    generation.model_dump(mode="json") for generation in generations
                                ],
                            },
                        )
                    return _json_response(
                        start_response=start_response,
                        status_code=200,
                        payload={
                            "status": "ok",
                            "trace_id": request_trace_id,
                            "record": preview.model_dump(mode="json"),
                        },
                    )
                if action == "secret.read":
                    secret_store = _secret_capable_store(record_store)
                    if secret_store is None:
                        return _json_response(
                            start_response=start_response,
                            status_code=404,
                            payload={
                                "status": "rejected",
                                "trace_id": request_trace_id,
                                "error": {
                                    "code": "not_found",
                                    "message": "Launchplane secret status routes require the Postgres storage backend.",
                                },
                            },
                        )
                    secret_status = control_plane_secrets.build_secret_status(
                        secret_store,
                        secret_id=params["secret_id"],
                    )
                    if not authz_policy.allows(
                        identity=identity,
                        action=action,
                        product="launchplane",
                        context=str(secret_status["context"]),
                    ):
                        return _json_response(
                            start_response=start_response,
                            status_code=403,
                            payload={
                                "status": "rejected",
                                "trace_id": request_trace_id,
                                "error": {
                                    "code": "authorization_denied",
                                    "message": "Workflow cannot read Launchplane managed secret status for the requested context.",
                                },
                            },
                        )
                    return _json_response(
                        start_response=start_response,
                        status_code=200,
                        payload={
                            "status": "ok",
                            "trace_id": request_trace_id,
                            "secret": secret_status,
                        },
                    )
                if action == "secret.list":
                    context_name = params["context"]
                    if not authz_policy.allows(
                        identity=identity,
                        action=action,
                        product="launchplane",
                        context=context_name,
                    ):
                        return _json_response(
                            start_response=start_response,
                            status_code=403,
                            payload={
                                "status": "rejected",
                                "trace_id": request_trace_id,
                                "error": {
                                    "code": "authorization_denied",
                                    "message": "Workflow cannot list Launchplane managed secret status for the requested context.",
                                },
                            },
                        )
                    secret_store = _secret_capable_store(record_store)
                    if secret_store is None:
                        return _json_response(
                            start_response=start_response,
                            status_code=404,
                            payload={
                                "status": "rejected",
                                "trace_id": request_trace_id,
                                "error": {
                                    "code": "not_found",
                                    "message": "Launchplane secret status routes require the Postgres storage backend.",
                                },
                            },
                        )
                    statuses = control_plane_secrets.list_secret_statuses(
                        secret_store,
                        context_name=context_name,
                        instance_name=params.get("instance", ""),
                    )
                    return _json_response(
                        start_response=start_response,
                        status_code=200,
                        payload={
                            "status": "ok",
                            "trace_id": request_trace_id,
                            "context": context_name,
                            "instance": params.get("instance", ""),
                            "secrets": statuses,
                        },
                    )
                if action == "target_logs.read":
                    context_name = params["context"]
                    instance_name = params["instance"]
                    if not authz_policy.allows(
                        identity=identity,
                        action=action,
                        product="launchplane",
                        context=context_name,
                    ):
                        return _json_response(
                            start_response=start_response,
                            status_code=403,
                            payload={
                                "status": "rejected",
                                "trace_id": request_trace_id,
                                "error": {
                                    "code": "authorization_denied",
                                    "message": "Workflow cannot read tracked target logs for the requested context.",
                                },
                            },
                        )
                    line_count = int(
                        str(
                            (
                                query.get("lines")
                                or [str(control_plane_dokploy.DEFAULT_DOKPLOY_LOG_LINE_COUNT)]
                            )[0]
                        )
                    )
                    since = str((query.get("since") or ["all"])[0])
                    search = str((query.get("search") or [""])[0])
                    if not isinstance(record_store, PostgresRecordStore):
                        return _json_response(
                            start_response=start_response,
                            status_code=503,
                            payload={
                                "status": "rejected",
                                "trace_id": request_trace_id,
                                "error": {
                                    "code": "database_required",
                                    "message": "Tracked target logs require DB-backed Launchplane storage.",
                                },
                            },
                        )
                    log_payload = build_tracked_target_logs_payload(
                        record_store=record_store,
                        control_plane_root=resolved_root,
                        context_name=context_name,
                        instance_name=instance_name,
                        line_count=line_count,
                        since=since,
                        search=search,
                    )
                    return _json_response(
                        start_response=start_response,
                        status_code=200,
                        payload={
                            "status": "ok",
                            "trace_id": request_trace_id,
                            **log_payload,
                        },
                    )
                if action == "launchplane_service.read":
                    if not authz_policy.allows(
                        identity=identity,
                        action=action,
                        product="launchplane",
                        context=_LAUNCHPLANE_SERVICE_CONTEXT,
                    ):
                        return _json_response(
                            start_response=start_response,
                            status_code=403,
                            payload={
                                "status": "rejected",
                                "trace_id": request_trace_id,
                                "error": {
                                    "code": "authorization_denied",
                                    "message": "Workflow cannot read Launchplane service runtime state.",
                                },
                            },
                        )
                    return _json_response(
                        start_response=start_response,
                        status_code=200,
                        payload={
                            "status": "ok",
                            "trace_id": request_trace_id,
                            "runtime": _launchplane_runtime_payload(
                                storage_backend=storage_backend,
                                authz_policy_sha256_value=resolved_authz_policy_sha256,
                                authz_policy_source=resolved_authz_policy_source,
                            ),
                        },
                    )
                if action == "every_code_work_request.read":
                    if not authz_policy.allows(
                        identity=identity,
                        action=action,
                        product="launchplane",
                        context=_LAUNCHPLANE_SERVICE_CONTEXT,
                    ):
                        return _json_response(
                            start_response=start_response,
                            status_code=403,
                            payload={
                                "status": "rejected",
                                "trace_id": request_trace_id,
                                "error": {
                                    "code": "authorization_denied",
                                    "message": "Workflow cannot read Every Code work requests.",
                                },
                            },
                        )
                    return _handle_every_code_work_request_read(
                        start_response=start_response,
                        trace_id=request_trace_id,
                        record_store=record_store,
                        path=path,
                        query=query,
                    )
                if action == "every_code_preview_gate.read":
                    if not authz_policy.allows(
                        identity=identity,
                        action=action,
                        product="launchplane",
                        context=_LAUNCHPLANE_SERVICE_CONTEXT,
                    ):
                        return _json_response(
                            start_response=start_response,
                            status_code=403,
                            payload={
                                "status": "rejected",
                                "trace_id": request_trace_id,
                                "error": {
                                    "code": "authorization_denied",
                                    "message": "Workflow cannot read Every Code preview readiness.",
                                },
                            },
                        )
                    return _handle_every_code_work_request_read(
                        start_response=start_response,
                        trace_id=request_trace_id,
                        record_store=record_store,
                        path=path,
                        query=query,
                    )
                if action == "work_graph.rank":
                    return handle_work_graph_snapshot_read(
                        authz_policy=authz_policy,
                        identity=identity,
                        trace_id=request_trace_id,
                        product_store=record_store,
                        work_request_store=_every_code_work_request_store(record_store),
                        planning_facts_provider=work_graph_planning_facts_provider,
                        utc_now=_utc_now_timestamp,
                        json_response=_json_response,
                        start_response=start_response,
                    )
                if action == "merge_train.admission":
                    admission_request = MergeTrainAdmissionEnvelope.model_validate(
                        {
                            "repository": str((query.get("repository") or [""])[0] or ""),
                            "base_branch": str((query.get("base_branch") or ["main"])[0] or ""),
                        }
                    )
                    policy = build_sellyouroutboard_main_merge_train_policy()
                    repository_policy = policy.find_repository_policy(
                        repository=admission_request.repository,
                        base_branch=admission_request.base_branch,
                    )
                    if not authz_policy.allows(
                        identity=identity,
                        action=repository_policy.service_authz.action,
                        product=repository_policy.service_authz.product,
                        context=repository_policy.service_authz.context,
                    ):
                        return _json_response(
                            start_response=start_response,
                            status_code=403,
                            payload={
                                "status": "rejected",
                                "trace_id": request_trace_id,
                                "error": {
                                    "code": "authorization_denied",
                                    "message": "Workflow cannot read the requested merge train admission decision.",
                                },
                            },
                        )
                    admission_decision = evaluate_merge_train_admission_from_store(
                        store=record_store,
                        repository=admission_request.repository,
                        base_branch=admission_request.base_branch,
                        requested_at=_utc_now_timestamp(),
                    )
                    return _json_response(
                        start_response=start_response,
                        status_code=200,
                        payload={
                            "status": "ok",
                            "trace_id": request_trace_id,
                            "admission": admission_decision.model_dump(mode="json"),
                        },
                    )
                if action == "product_profile.read":
                    if "product" in params:
                        profile = record_store.read_product_profile_record(params["product"])
                        if not authz_policy.allows(
                            identity=identity,
                            action=action,
                            product=profile.product,
                            context=_LAUNCHPLANE_SERVICE_CONTEXT,
                        ):
                            return _json_response(
                                start_response=start_response,
                                status_code=403,
                                payload={
                                    "status": "rejected",
                                    "trace_id": request_trace_id,
                                    "error": {
                                        "code": "authorization_denied",
                                        "message": "Workflow cannot read the requested product profile.",
                                    },
                                    "authz": _authz_diagnostic_payload(
                                        identity=identity,
                                        authz_policy_sha256_value=resolved_authz_policy_sha256,
                                        authz_policy_source=resolved_authz_policy_source,
                                    ),
                                },
                            )
                        if params.get("context_cutover_audit") == "true":
                            if not isinstance(record_store, PostgresRecordStore):
                                return _json_response(
                                    start_response=start_response,
                                    status_code=503,
                                    payload={
                                        "status": "rejected",
                                        "trace_id": request_trace_id,
                                        "error": {
                                            "code": "database_required",
                                            "message": "Context cutover audit requires Launchplane database storage.",
                                        },
                                    },
                                )
                            source_context = str(
                                (query.get("source_context") or [""])[0] or ""
                            ).strip()
                            target_context = str(
                                (query.get("target_context") or [""])[0] or ""
                            ).strip()
                            preview_context = str(
                                (query.get("preview_context") or [""])[0] or ""
                            ).strip()
                            if not _product_profile_context_cutover_contexts_allowed(
                                profile=profile,
                                source_context=source_context,
                                target_context=target_context,
                                preview_context=preview_context,
                            ):
                                return _json_response(
                                    start_response=start_response,
                                    status_code=403,
                                    payload={
                                        "status": "rejected",
                                        "trace_id": request_trace_id,
                                        "error": {
                                            "code": "context_not_in_product_boundary",
                                            "message": "Requested audit contexts are not owned by the product profile.",
                                        },
                                    },
                                )
                            try:
                                audit_payload = control_plane_product_context_audit.build_product_context_cutover_audit(
                                    record_store=record_store,
                                    product=profile.product,
                                    source_context=source_context,
                                    target_context=target_context,
                                    preview_context=preview_context,
                                )
                            except ValueError:
                                return _json_response(
                                    start_response=start_response,
                                    status_code=400,
                                    payload={
                                        "status": "rejected",
                                        "trace_id": request_trace_id,
                                        "error": {
                                            "code": "invalid_context_cutover_audit_request",
                                            "message": "Context cutover audit request is invalid.",
                                        },
                                    },
                                )
                            return _json_response(
                                start_response=start_response,
                                status_code=200,
                                payload={
                                    "status": "ok",
                                    "trace_id": request_trace_id,
                                    "audit": audit_payload,
                                },
                            )
                        return _json_response(
                            start_response=start_response,
                            status_code=200,
                            payload={
                                "status": "ok",
                                "trace_id": request_trace_id,
                                "profile": profile.model_dump(mode="json"),
                            },
                        )
                    if not authz_policy.allows(
                        identity=identity,
                        action=action,
                        product="launchplane",
                        context=_LAUNCHPLANE_SERVICE_CONTEXT,
                    ):
                        return _json_response(
                            start_response=start_response,
                            status_code=403,
                            payload={
                                "status": "rejected",
                                "trace_id": request_trace_id,
                                "error": {
                                    "code": "authorization_denied",
                                    "message": "Workflow cannot list Launchplane product profiles.",
                                },
                                "authz": _authz_diagnostic_payload(
                                    identity=identity,
                                    authz_policy_sha256_value=resolved_authz_policy_sha256,
                                    authz_policy_source=resolved_authz_policy_source,
                                ),
                            },
                        )
                    driver_id_filter = str((query.get("driver_id") or [""])[0] or "").strip()
                    product_profile_payload = control_plane_product_read_service.build_product_profile_list_service_payload(
                        record_store=record_store,
                        driver_id=driver_id_filter,
                    )
                    return _json_response(
                        start_response=start_response,
                        status_code=200,
                        payload={
                            "status": "ok",
                            "trace_id": request_trace_id,
                            **product_profile_payload,
                        },
                    )
                if action == "product_environment.read":

                    def product_action_allowed(
                        requested_action: str, requested_product: str, requested_context: str
                    ) -> bool:
                        return authz_policy.allows(
                            identity=identity,
                            action=requested_action,
                            product=requested_product,
                            context=requested_context,
                        )

                    if params.get("agent_context") == "true":
                        if not agent_context_allowed(
                            authz_policy=authz_policy,
                            identity=identity,
                        ):
                            return _json_response(
                                start_response=start_response,
                                status_code=403,
                                payload={
                                    "status": "rejected",
                                    "trace_id": request_trace_id,
                                    "error": {
                                        "code": "authorization_denied",
                                        "message": "Workflow cannot read Launchplane agent context.",
                                    },
                                },
                            )
                        repository_filter = str(
                            (query.get("repository") or [""])[0] or ""
                        ).strip()
                        agent_context = build_agent_context_service_payload(
                            generated_at=_utc_now_timestamp(),
                            repository=repository_filter,
                            product_store=record_store,
                            work_request_store=_every_code_work_request_store(record_store),
                            preview_readiness_store=_every_code_work_request_store(record_store),
                            action_allowed=agent_context_action_allowed(
                                authz_policy=authz_policy,
                                identity=identity,
                            ),
                            planning_facts_provider=work_graph_planning_facts_provider,
                        )
                        return _json_response(
                            start_response=start_response,
                            status_code=200,
                            payload={
                                "status": "ok",
                                "trace_id": request_trace_id,
                                "context": agent_context.model_dump(mode="json"),
                            },
                        )

                    if params.get("repo_product_mapping") == "true":
                        return handle_repo_product_mapping_read(
                            authz_policy=authz_policy,
                            identity=identity,
                            trace_id=request_trace_id,
                            product_store=record_store,
                            work_request_store=_every_code_work_request_store(record_store),
                            utc_now=_utc_now_timestamp,
                            json_response=_json_response,
                            start_response=start_response,
                        )

                    if control_plane_product_read_service.is_product_environment_detail_request(
                        params
                    ):
                        product_read_result = control_plane_product_read_service.build_product_environment_read_service_result(
                            record_store=record_store,
                            params=params,
                            action_allowed=product_action_allowed,
                        )
                        if not product_action_allowed(
                            "product_environment.read",
                            product_read_result.authorization_product,
                            product_read_result.authorization_context,
                        ):
                            return _json_response(
                                start_response=start_response,
                                status_code=403,
                                payload={
                                    "status": "rejected",
                                    "trace_id": request_trace_id,
                                    "error": {
                                        "code": "authorization_denied",
                                        "message": product_read_result.denial_message,
                                    },
                                },
                            )
                        return _json_response(
                            start_response=start_response,
                            status_code=200,
                            payload={
                                "status": "ok",
                                "trace_id": request_trace_id,
                                **product_read_result.payload,
                            },
                        )
                    if not product_action_allowed(
                        "product_environment.read",
                        "launchplane",
                        _LAUNCHPLANE_SERVICE_CONTEXT,
                    ):
                        return _json_response(
                            start_response=start_response,
                            status_code=403,
                            payload={
                                "status": "rejected",
                                "trace_id": request_trace_id,
                                "error": {
                                    "code": "authorization_denied",
                                    "message": "Workflow cannot list product overviews.",
                                },
                            },
                        )
                    product_list_payload = control_plane_product_read_service.build_product_environment_list_service_payload(
                        record_store=record_store,
                        action_allowed=product_action_allowed,
                    )
                    return _json_response(
                        start_response=start_response,
                        status_code=200,
                        payload={
                            "status": "ok",
                            "trace_id": request_trace_id,
                            **product_list_payload,
                        },
                    )
                context_name = params["context"]
                if not authz_policy.allows(
                    identity=identity,
                    action=action,
                    product="launchplane",
                    context=context_name,
                ):
                    return _json_response(
                        start_response=start_response,
                        status_code=403,
                        payload={
                            "status": "rejected",
                            "trace_id": request_trace_id,
                            "error": {
                                "code": "authorization_denied",
                                "message": "Workflow cannot read recent operations for the requested context.",
                            },
                        },
                    )
                deployments = record_store.list_deployment_records(
                    context_name=context_name, limit=10
                )
                promotions = record_store.list_promotion_records(
                    context_name=context_name, limit=10
                )
                previews = record_store.list_preview_records(context_name=context_name, limit=10)
                recent_inventory = [
                    record
                    for record in record_store.list_environment_inventory()
                    if record.context == context_name
                ]
                return _json_response(
                    start_response=start_response,
                    status_code=200,
                    payload={
                        "status": "ok",
                        "trace_id": request_trace_id,
                        "context": context_name,
                        "storage_backend": storage_backend,
                        "inventory": [
                            record.model_dump(mode="json") for record in recent_inventory
                        ],
                        "recent_deployments": [
                            record.model_dump(mode="json") for record in deployments
                        ],
                        "recent_promotions": [
                            record.model_dump(mode="json") for record in promotions
                        ],
                        "recent_previews": [record.model_dump(mode="json") for record in previews],
                    },
                )
            payload = _read_json_request(environ)
            request_idempotency_key = _idempotency_key(environ)
            request_scope = _idempotency_scope(identity)
            request_fingerprint = _idempotency_request_fingerprint(route_path=path, payload=payload)
            driver_result: BaseModel | dict[str, object] | None = None
            result: dict[str, object] = {}
            if path == "/v1/every-code/work-requests/create":
                every_code_request = EveryCodeWorkRequestCreateEnvelope.model_validate(payload)
                if not authz_policy.allows(
                    identity=identity,
                    action="every_code_work_request.write",
                    product="launchplane",
                    context=_LAUNCHPLANE_SERVICE_CONTEXT,
                ):
                    return _json_response(
                        start_response=start_response,
                        status_code=403,
                        payload={
                            "status": "rejected",
                            "trace_id": request_trace_id,
                            "error": {
                                "code": "authorization_denied",
                                "message": "Workflow cannot create Every Code work requests.",
                            },
                        },
                    )
                idempotent_response = _check_idempotent_request(
                    record_store=record_store,
                    scope=request_scope,
                    route_path=path,
                    idempotency_key=request_idempotency_key,
                    request_fingerprint=request_fingerprint,
                    start_response=start_response,
                    trace_id=request_trace_id,
                )
                if idempotent_response is not None:
                    return idempotent_response
                every_code_store = _every_code_work_request_store(record_store)
                record = _build_every_code_work_request_record(
                    every_code_request,
                    queued_at=every_code_request.queued_at.strip() or _utc_now_timestamp(),
                )
                every_code_store.write_every_code_work_request_record(record)
                result = {"request_id": record.request_id, "state": record.state}
                driver_result = {"request": record.model_dump(mode="json")}
            elif path == _MERGE_TRAIN_RUN_ONCE_ROUTE:
                merge_train_request = MergeTrainRunOnceEnvelope.model_validate(payload)
                policy = build_sellyouroutboard_main_merge_train_policy()
                repository_policy = policy.find_repository_policy(
                    repository=merge_train_request.repository,
                    base_branch=merge_train_request.base_branch,
                )
                if not authz_policy.allows(
                    identity=identity,
                    action=repository_policy.service_authz.action,
                    product=repository_policy.service_authz.product,
                    context=repository_policy.service_authz.context,
                ):
                    return _json_response(
                        start_response=start_response,
                        status_code=403,
                        payload={
                            "status": "rejected",
                            "trace_id": request_trace_id,
                            "error": {
                                "code": "authorization_denied",
                                "message": "Workflow cannot run the requested merge train policy.",
                            },
                        },
                    )
                token_env = repository_policy.github_token.env_var
                if not token_env:
                    return _json_response(
                        start_response=start_response,
                        status_code=503,
                        payload={
                            "status": "rejected",
                            "trace_id": request_trace_id,
                            "error": {
                                "code": "github_token_not_configured",
                                "message": "Merge train policy does not define a GitHub token environment variable.",
                            },
                        },
                    )
                token = os.environ.get(token_env, "").strip()
                if not token:
                    return _json_response(
                        start_response=start_response,
                        status_code=503,
                        payload={
                            "status": "rejected",
                            "trace_id": request_trace_id,
                            "error": {
                                "code": "github_token_not_configured",
                                "message": "Configured merge train GitHub token is not available.",
                            },
                        },
                    )
                transport = UrllibMergeTrainGitHubTransport(
                    token=token,
                    api_base_url=merge_train_request.github_api_base_url,
                )
                snapshot = GitHubMergeTrainSnapshotReader(
                    transport=transport
                ).read_merge_train_snapshot(
                    repository=merge_train_request.repository,
                    base_branch=merge_train_request.base_branch,
                )
                dry_run_result = build_merge_train_dry_run_result(
                    policy=policy, snapshot=snapshot
                )
                result = {
                    "repository": merge_train_request.repository,
                    "base_branch": merge_train_request.base_branch,
                    "mode": "mutate" if merge_train_request.mutate else "dry-run",
                    "dry_run_result": dry_run_result.model_dump(mode="json"),
                }
                worker_step_result = None
                if merge_train_request.mutate:
                    github_client = GitHubMergeTrainClient(transport=transport)
                    worker_step_result = run_merge_train_worker_step(
                        policy=policy,
                        snapshot=snapshot,
                        clients=MergeTrainWorkerClients(
                            label_client=github_client,
                            branch_client=github_client,
                            merge_client=github_client,
                        ),
                    )
                    result["worker_step_result"] = worker_step_result.model_dump(mode="json")
                    driver_result = worker_step_result
                else:
                    driver_result = result
                run_record = build_merge_train_run_record(
                    recorded_at=_utc_now_timestamp(),
                    trace_id=request_trace_id,
                    policy_sha256=policy.policy_sha256,
                    snapshot=snapshot,
                    dry_run_result=dry_run_result,
                    worker_step_result=worker_step_result,
                )
                record_store.write_merge_train_run_record(run_record)
                result["merge_train_run_id"] = run_record.run_id
            elif path == "/v1/agent/write-intents/evaluate":
                intent_request = AgentWriteIntentRequest.model_validate(payload)
                intent_authz_action = authz_action_for_agent_write_intent(
                    intent_request.intent
                )
                authorized = authz_policy.allows(
                    identity=identity,
                    action=intent_authz_action,
                    product=intent_request.product,
                    context=intent_request.context,
                )
                if intent_request.secret_bindings:
                    secret_authz_action = agent_write_intent_secret_action(intent_request)
                    authorized = authorized and authz_policy.allows(
                        identity=identity,
                        action=secret_authz_action,
                        product=intent_request.product,
                        context=intent_request.context,
                    )
                intent_audit = agent_authz_audit(
                    identity=identity,
                    action=intent_authz_action,
                    product=intent_request.product,
                    context=intent_request.context,
                    decision="allowed" if authorized else "denied",
                    reason_code="authorized" if authorized else "authorization_denied",
                    policy_source=resolved_authz_policy_source,
                    policy_sha256=resolved_authz_policy_sha256,
                )
                secret_evidence = _agent_write_intent_secret_evidence(
                    record_store=record_store,
                    request=intent_request,
                )
                evaluation = evaluate_agent_write_intent(
                    request=intent_request,
                    authorized=authorized,
                    audit=intent_audit,
                    secret_evidence=secret_evidence,
                )
                recorded_at = _utc_now_timestamp()
                intent_record = AgentWriteIntentRecord(
                    record_id=build_agent_write_intent_record_id(
                        recorded_at=recorded_at,
                        trace_id=request_trace_id,
                        request=intent_request,
                        evaluation=evaluation,
                    ),
                    recorded_at=recorded_at,
                    trace_id=request_trace_id,
                    idempotency_key=request_idempotency_key,
                    request=intent_request,
                    evaluation=evaluation,
                )
                _agent_write_intent_record_store(record_store).write_agent_write_intent_record(
                    intent_record
                )
                result = {
                    "intent": evaluation.model_dump(mode="json"),
                    "record": {
                        "record_id": intent_record.record_id,
                        "recorded_at": intent_record.recorded_at,
                    },
                }
                driver_result = result
            elif path == "/v1/every-code/work-requests/claim":
                if not authz_policy.allows(
                    identity=identity,
                    action="every_code_work_request.claim",
                    product="launchplane",
                    context=_LAUNCHPLANE_SERVICE_CONTEXT,
                ):
                    return _json_response(
                        start_response=start_response,
                        status_code=403,
                        payload={
                            "status": "rejected",
                            "trace_id": request_trace_id,
                            "error": {
                                "code": "authorization_denied",
                                "message": "Workflow cannot claim Every Code work requests.",
                            },
                        },
                    )
                idempotent_response = _check_idempotent_request(
                    record_store=record_store,
                    scope=request_scope,
                    route_path=path,
                    idempotency_key=request_idempotency_key,
                    request_fingerprint=request_fingerprint,
                    start_response=start_response,
                    trace_id=request_trace_id,
                )
                if idempotent_response is not None:
                    return idempotent_response
                return _handle_every_code_worker_write(
                    start_response=start_response,
                    trace_id=request_trace_id,
                    record_store=record_store,
                    path=path,
                    payload=payload,
                )
            elif path == "/v1/every-code/work-requests/rerun":
                if not authz_policy.allows(
                    identity=identity,
                    action="every_code_work_request.write",
                    product="launchplane",
                    context=_LAUNCHPLANE_SERVICE_CONTEXT,
                ):
                    return _json_response(
                        start_response=start_response,
                        status_code=403,
                        payload={
                            "status": "rejected",
                            "trace_id": request_trace_id,
                            "error": {
                                "code": "authorization_denied",
                                "message": "Workflow cannot rerun Every Code work requests.",
                            },
                        },
                    )
                idempotent_response = _check_idempotent_request(
                    record_store=record_store,
                    scope=request_scope,
                    route_path=path,
                    idempotency_key=request_idempotency_key,
                    request_fingerprint=request_fingerprint,
                    start_response=start_response,
                    trace_id=request_trace_id,
                )
                if idempotent_response is not None:
                    return idempotent_response
                return _handle_every_code_worker_write(
                    start_response=start_response,
                    trace_id=request_trace_id,
                    record_store=record_store,
                    path=path,
                    payload=payload,
                )
            elif path == "/v1/every-code/work-requests/status":
                if not authz_policy.allows(
                    identity=identity,
                    action="every_code_work_request.update",
                    product="launchplane",
                    context=_LAUNCHPLANE_SERVICE_CONTEXT,
                ):
                    return _json_response(
                        start_response=start_response,
                        status_code=403,
                        payload={
                            "status": "rejected",
                            "trace_id": request_trace_id,
                            "error": {
                                "code": "authorization_denied",
                                "message": "Workflow cannot update Every Code work requests.",
                            },
                        },
                    )
                idempotent_response = _check_idempotent_request(
                    record_store=record_store,
                    scope=request_scope,
                    route_path=path,
                    idempotency_key=request_idempotency_key,
                    request_fingerprint=request_fingerprint,
                    start_response=start_response,
                    trace_id=request_trace_id,
                )
                if idempotent_response is not None:
                    return idempotent_response
                return _handle_every_code_worker_write(
                    start_response=start_response,
                    trace_id=request_trace_id,
                    record_store=record_store,
                    path=path,
                    payload=payload,
                )
            elif path == "/v1/work-graph/rank":
                work_graph_rank_result = rank_work_graph_snapshot(
                    authz_policy=authz_policy,
                    identity=identity,
                    payload=payload,
                )
                if work_graph_rank_result is None:
                    return work_graph_rank_denied_response(
                        trace_id=request_trace_id,
                        json_response=_json_response,
                        start_response=start_response,
                    )
                result = work_graph_rank_result.result
                driver_result = work_graph_rank_result.driver_result
            elif path == "/v1/evidence/deployments":
                deployment_request = DeploymentEvidenceEnvelope.model_validate(payload)
                if not authz_policy.allows(
                    identity=identity,
                    action="deployment.write",
                    product=deployment_request.product,
                    context=deployment_request.deployment.context,
                ):
                    return _json_response(
                        start_response=start_response,
                        status_code=403,
                        payload={
                            "status": "rejected",
                            "trace_id": request_trace_id,
                            "error": {
                                "code": "authorization_denied",
                                "message": (
                                    "Workflow cannot write deployment evidence for the requested"
                                    " product/context."
                                ),
                            },
                        },
                    )
                idempotent_response = _check_idempotent_request(
                    record_store=record_store,
                    scope=request_scope,
                    route_path=path,
                    idempotency_key=request_idempotency_key,
                    request_fingerprint=request_fingerprint,
                    start_response=start_response,
                    trace_id=request_trace_id,
                )
                if idempotent_response is not None:
                    return idempotent_response
                result = dict[str, object](
                    apply_deployment_evidence(
                        record_store=record_store,
                        deployment_record=deployment_request.deployment,
                    )
                )
            elif path == "/v1/evidence/backup-gates":
                backup_gate_request = BackupGateEvidenceEnvelope.model_validate(payload)
                if not authz_policy.allows(
                    identity=identity,
                    action="backup_gate.write",
                    product=backup_gate_request.product,
                    context=backup_gate_request.backup_gate.context,
                ):
                    return _json_response(
                        start_response=start_response,
                        status_code=403,
                        payload={
                            "status": "rejected",
                            "trace_id": request_trace_id,
                            "error": {
                                "code": "authorization_denied",
                                "message": (
                                    "Workflow cannot write backup gate evidence for the requested"
                                    " product/context."
                                ),
                            },
                        },
                    )
                idempotent_response = _check_idempotent_request(
                    record_store=record_store,
                    scope=request_scope,
                    route_path=path,
                    idempotency_key=request_idempotency_key,
                    request_fingerprint=request_fingerprint,
                    start_response=start_response,
                    trace_id=request_trace_id,
                )
                if idempotent_response is not None:
                    return idempotent_response
                record_store.write_backup_gate_record(backup_gate_request.backup_gate)
                result = {"backup_gate_record_id": backup_gate_request.backup_gate.record_id}
            elif path == "/v1/product-config/apply":
                product_config_request, product_config_response = (
                    validate_product_config_apply_request(
                        authz_policy=authz_policy,
                        identity=identity,
                        payload=payload,
                        trace_id=request_trace_id,
                        json_response=_json_response,
                        start_response=start_response,
                    )
                )
                if product_config_response is not None:
                    return product_config_response
                assert product_config_request is not None
                idempotent_response = _check_idempotent_request(
                    record_store=record_store,
                    scope=request_scope,
                    route_path=path,
                    idempotency_key=request_idempotency_key,
                    request_fingerprint=request_fingerprint,
                    start_response=start_response,
                    trace_id=request_trace_id,
                )
                if idempotent_response is not None:
                    return idempotent_response
                if not isinstance(record_store, PostgresRecordStore):
                    return product_config_database_required_response(
                        trace_id=request_trace_id,
                        json_response=_json_response,
                        start_response=start_response,
                    )
                product_config_result = apply_product_config_route(
                    record_store=record_store,
                    request=product_config_request,
                    actor=_identity_actor(identity),
                    trace_id=request_trace_id,
                    json_response=_json_response,
                    start_response=start_response,
                )
                if not isinstance(product_config_result, ProductConfigRouteResult):
                    return product_config_result
                driver_result = product_config_result.driver_result
            elif path == "/v1/authz-policies/github-actions/grants":
                authz_grant_request = control_plane_authz_grant_service.AuthzPolicyGitHubActionsGrantEnvelope.model_validate(
                    payload
                )
                if not isinstance(record_store, PostgresRecordStore):
                    return _json_response(
                        start_response=start_response,
                        status_code=503,
                        payload={
                            "status": "rejected",
                            "trace_id": request_trace_id,
                            "error": {
                                "code": "database_required",
                                "message": "Authz policy grant writes require Launchplane database storage.",
                            },
                        },
                    )
                if not authz_policy.allows(
                    identity=identity,
                    action="launchplane_service_deploy.execute",
                    product=authz_grant_request.product,
                    context=_LAUNCHPLANE_SERVICE_CONTEXT,
                ):
                    return _json_response(
                        start_response=start_response,
                        status_code=403,
                        payload={
                            "status": "rejected",
                            "trace_id": request_trace_id,
                            "error": {
                                "code": "authorization_denied",
                                "message": "Workflow cannot write Launchplane authz policy grants.",
                            },
                        },
                    )
                if authz_grant_request.mode == "apply":
                    idempotent_response = _check_idempotent_request(
                        record_store=record_store,
                        scope=request_scope,
                        route_path=path,
                        idempotency_key=request_idempotency_key,
                        request_fingerprint=request_fingerprint,
                        start_response=start_response,
                        trace_id=request_trace_id,
                    )
                    if idempotent_response is not None:
                        return idempotent_response
                try:
                    (
                        current_policy,
                        current_record,
                        diff,
                    ) = control_plane_authz_grant_service.plan_github_actions_authz_policy_grant(
                        record_store=record_store,
                        grant=authz_grant_request.grant,
                    )
                    audit = control_plane_authz_grant_service.authz_policy_grant_audit_payload(
                        request=authz_grant_request,
                        identity=identity,
                        previous_record=current_record,
                        new_record=None,
                        changed=bool(diff["changed"]),
                        trace_id=request_trace_id,
                        now_timestamp=_now_timestamp,
                    )
                    authz_policy_record = current_record
                    changed = bool(diff["changed"])
                    if authz_grant_request.mode == "apply":
                        (
                            updated_policy,
                            authz_policy_record,
                            changed,
                            diff,
                            audit,
                        ) = control_plane_authz_grant_service.write_github_actions_authz_policy_grant(
                            record_store=record_store,
                            request=authz_grant_request,
                            identity=identity,
                            trace_id=request_trace_id,
                            now_timestamp=_now_timestamp,
                        )
                    else:
                        updated_policy = current_policy
                        authz_policy_record = LaunchplaneAuthzPolicyRecord(
                            record_id=current_record.record_id,
                            status=current_record.status,
                            source=current_record.source,
                            updated_at=current_record.updated_at,
                            policy_sha256=current_record.policy_sha256,
                            policy=current_record.policy,
                            audit=audit,
                        )
                except ValueError:
                    return _json_response(
                        start_response=start_response,
                        status_code=503,
                        payload={
                            "status": "rejected",
                            "trace_id": request_trace_id,
                            "error": {
                                "code": "authz_policy_unavailable",
                                "message": "Launchplane active authz policy is unavailable.",
                            },
                        },
                    )
                if authz_grant_request.mode == "apply":
                    authz_policy = updated_policy
                    resolved_authz_policy_sha256 = authz_policy_record.policy_sha256
                    resolved_authz_policy_source = "db"
                result, driver_result = (
                    control_plane_authz_grant_service.build_authz_policy_grant_service_result(
                        authz_policy_record=authz_policy_record,
                        changed=changed,
                        mode=authz_grant_request.mode,
                        diff=diff,
                        audit=audit,
                    )
                )
            elif path == "/v1/authz-policies/github-humans/grants":
                human_authz_grant_request = control_plane_authz_grant_service.AuthzPolicyGitHubHumanGrantEnvelope.model_validate(
                    payload
                )
                if not isinstance(record_store, PostgresRecordStore):
                    return _json_response(
                        start_response=start_response,
                        status_code=503,
                        payload={
                            "status": "rejected",
                            "trace_id": request_trace_id,
                            "error": {
                                "code": "database_required",
                                "message": "Authz human policy grant writes require Launchplane database storage.",
                            },
                        },
                    )
                if not authz_policy.allows(
                    identity=identity,
                    action="launchplane_service_deploy.execute",
                    product=human_authz_grant_request.product,
                    context=_LAUNCHPLANE_SERVICE_CONTEXT,
                ):
                    return _json_response(
                        start_response=start_response,
                        status_code=403,
                        payload={
                            "status": "rejected",
                            "trace_id": request_trace_id,
                            "error": {
                                "code": "authorization_denied",
                                "message": "Workflow cannot write Launchplane authz human policy grants.",
                            },
                        },
                    )
                if human_authz_grant_request.mode == "apply":
                    idempotent_response = _check_idempotent_request(
                        record_store=record_store,
                        scope=request_scope,
                        route_path=path,
                        idempotency_key=request_idempotency_key,
                        request_fingerprint=request_fingerprint,
                        start_response=start_response,
                        trace_id=request_trace_id,
                    )
                    if idempotent_response is not None:
                        return idempotent_response
                try:
                    (
                        current_policy,
                        current_record,
                        diff,
                    ) = control_plane_authz_grant_service.plan_github_human_authz_policy_grant(
                        record_store=record_store,
                        grant=human_authz_grant_request.grant,
                    )
                    audit = control_plane_authz_grant_service.authz_policy_grant_audit_payload(
                        request=human_authz_grant_request,
                        identity=identity,
                        previous_record=current_record,
                        new_record=None,
                        changed=bool(diff["changed"]),
                        trace_id=request_trace_id,
                        now_timestamp=_now_timestamp,
                    )
                    authz_policy_record = current_record
                    changed = bool(diff["changed"])
                    if human_authz_grant_request.mode == "apply":
                        (
                            updated_policy,
                            authz_policy_record,
                            changed,
                            diff,
                            audit,
                        ) = control_plane_authz_grant_service.write_github_human_authz_policy_grant(
                            record_store=record_store,
                            request=human_authz_grant_request,
                            identity=identity,
                            trace_id=request_trace_id,
                            now_timestamp=_now_timestamp,
                        )
                    else:
                        updated_policy = current_policy
                        authz_policy_record = LaunchplaneAuthzPolicyRecord(
                            record_id=current_record.record_id,
                            status=current_record.status,
                            source=current_record.source,
                            updated_at=current_record.updated_at,
                            policy_sha256=current_record.policy_sha256,
                            policy=current_record.policy,
                            audit=audit,
                        )
                except ValueError:
                    return _json_response(
                        start_response=start_response,
                        status_code=503,
                        payload={
                            "status": "rejected",
                            "trace_id": request_trace_id,
                            "error": {
                                "code": "authz_policy_unavailable",
                                "message": "Launchplane active authz policy is unavailable.",
                            },
                        },
                    )
                if human_authz_grant_request.mode == "apply":
                    authz_policy = updated_policy
                    resolved_authz_policy_sha256 = authz_policy_record.policy_sha256
                    resolved_authz_policy_source = "db"
                result, driver_result = (
                    control_plane_authz_grant_service.build_authz_policy_grant_service_result(
                        authz_policy_record=authz_policy_record,
                        changed=changed,
                        mode=human_authz_grant_request.mode,
                        diff=diff,
                        audit=audit,
                    )
                )
            elif path == "/v1/authz-policies/terminal-agents/grants":
                terminal_authz_grant_request = control_plane_authz_grant_service.AuthzPolicyTerminalAgentGrantEnvelope.model_validate(
                    payload
                )
                if not isinstance(record_store, PostgresRecordStore):
                    return _json_response(
                        start_response=start_response,
                        status_code=503,
                        payload={
                            "status": "rejected",
                            "trace_id": request_trace_id,
                            "error": {
                                "code": "database_required",
                                "message": "Authz terminal-agent policy grant writes require Launchplane database storage.",
                            },
                        },
                    )
                if not authz_policy.allows(
                    identity=identity,
                    action="launchplane_service_deploy.execute",
                    product=terminal_authz_grant_request.product,
                    context=_LAUNCHPLANE_SERVICE_CONTEXT,
                ):
                    return _json_response(
                        start_response=start_response,
                        status_code=403,
                        payload={
                            "status": "rejected",
                            "trace_id": request_trace_id,
                            "error": {
                                "code": "authorization_denied",
                                "message": "Workflow cannot write Launchplane authz terminal-agent policy grants.",
                            },
                        },
                    )
                if terminal_authz_grant_request.mode == "apply":
                    idempotent_response = _check_idempotent_request(
                        record_store=record_store,
                        scope=request_scope,
                        route_path=path,
                        idempotency_key=request_idempotency_key,
                        request_fingerprint=request_fingerprint,
                        start_response=start_response,
                        trace_id=request_trace_id,
                    )
                    if idempotent_response is not None:
                        return idempotent_response
                try:
                    (
                        current_policy,
                        current_record,
                        diff,
                    ) = control_plane_authz_grant_service.plan_terminal_agent_authz_policy_grant(
                        record_store=record_store,
                        grant=terminal_authz_grant_request.grant,
                    )
                    audit = control_plane_authz_grant_service.authz_policy_grant_audit_payload(
                        request=terminal_authz_grant_request,
                        identity=identity,
                        previous_record=current_record,
                        new_record=None,
                        changed=bool(diff["changed"]),
                        trace_id=request_trace_id,
                        now_timestamp=_now_timestamp,
                    )
                    authz_policy_record = current_record
                    changed = bool(diff["changed"])
                    if terminal_authz_grant_request.mode == "apply":
                        (
                            updated_policy,
                            authz_policy_record,
                            changed,
                            diff,
                            audit,
                        ) = control_plane_authz_grant_service.write_terminal_agent_authz_policy_grant(
                            record_store=record_store,
                            request=terminal_authz_grant_request,
                            identity=identity,
                            trace_id=request_trace_id,
                            now_timestamp=_now_timestamp,
                        )
                    else:
                        updated_policy = current_policy
                        authz_policy_record = LaunchplaneAuthzPolicyRecord(
                            record_id=current_record.record_id,
                            status=current_record.status,
                            source=current_record.source,
                            updated_at=current_record.updated_at,
                            policy_sha256=current_record.policy_sha256,
                            policy=current_record.policy,
                            audit=audit,
                        )
                except ValueError:
                    return _json_response(
                        start_response=start_response,
                        status_code=503,
                        payload={
                            "status": "rejected",
                            "trace_id": request_trace_id,
                            "error": {
                                "code": "authz_policy_unavailable",
                                "message": "Launchplane active authz policy is unavailable.",
                            },
                        },
                    )
                if terminal_authz_grant_request.mode == "apply":
                    authz_policy = updated_policy
                    resolved_authz_policy_sha256 = authz_policy_record.policy_sha256
                    resolved_authz_policy_source = "db"
                result, driver_result = (
                    control_plane_authz_grant_service.build_authz_policy_grant_service_result(
                        authz_policy_record=authz_policy_record,
                        changed=changed,
                        mode=terminal_authz_grant_request.mode,
                        diff=diff,
                        audit=audit,
                    )
                )
            elif path == "/v1/runtime-key-safety/policies/apply":
                runtime_policy_request, runtime_policy_response = (
                    validate_runtime_key_safety_policy_request(
                        authz_policy=authz_policy,
                        identity=identity,
                        payload=payload,
                        trace_id=request_trace_id,
                        json_response=_json_response,
                        start_response=start_response,
                    )
                )
                if runtime_policy_response is not None:
                    return runtime_policy_response
                assert runtime_policy_request is not None
                if not isinstance(record_store, PostgresRecordStore):
                    return runtime_key_safety_database_required_response(
                        trace_id=request_trace_id,
                        json_response=_json_response,
                        start_response=start_response,
                    )
                idempotent_response = _check_idempotent_request(
                    record_store=record_store,
                    scope=request_scope,
                    route_path=path,
                    idempotency_key=request_idempotency_key,
                    request_fingerprint=request_fingerprint,
                    start_response=start_response,
                    trace_id=request_trace_id,
                )
                if idempotent_response is not None:
                    return idempotent_response
                runtime_policy_result = apply_runtime_key_safety_policy_route(
                    record_store=record_store,
                    request=runtime_policy_request,
                    now_timestamp=_now_timestamp,
                    record_slug=_record_slug,
                )
                assert isinstance(runtime_policy_result, RuntimeKeySafetyPolicyRouteResult)
                result = runtime_policy_result.result
                driver_result = runtime_policy_result.driver_result
            elif path == "/v1/live-target-runtime/apply":
                live_target_runtime_request = LiveTargetRuntimeApplyEnvelope.model_validate(payload)
                action = (
                    "live_target_runtime.apply"
                    if live_target_runtime_request.apply_changes
                    else "live_target_runtime.plan"
                )
                if not authz_policy.allows(
                    identity=identity,
                    action=action,
                    product=live_target_runtime_request.product,
                    context=live_target_runtime_request.context,
                ):
                    return _json_response(
                        start_response=start_response,
                        status_code=403,
                        payload={
                            "status": "rejected",
                            "trace_id": request_trace_id,
                            "error": {
                                "code": "authorization_denied",
                                "message": (
                                    "Workflow cannot plan or apply live target runtime for"
                                    " the requested product/context."
                                ),
                            },
                        },
                    )
                idempotent_response = _check_idempotent_request(
                    record_store=record_store,
                    scope=request_scope,
                    route_path=path,
                    idempotency_key=request_idempotency_key,
                    request_fingerprint=request_fingerprint,
                    start_response=start_response,
                    trace_id=request_trace_id,
                )
                if idempotent_response is not None:
                    return idempotent_response
                if not isinstance(record_store, PostgresRecordStore):
                    return _json_response(
                        start_response=start_response,
                        status_code=503,
                        payload={
                            "status": "rejected",
                            "trace_id": request_trace_id,
                            "error": {
                                "code": "database_required",
                                "message": (
                                    "Live target runtime apply requires DB-backed Launchplane"
                                    " storage."
                                ),
                            },
                        },
                    )
                try:
                    driver_result = control_plane_live_target_runtime.apply_live_target_runtime_environment(
                        control_plane_root=resolved_root,
                        context_name=live_target_runtime_request.context,
                        instance_name=live_target_runtime_request.instance,
                        apply_changes=live_target_runtime_request.apply_changes,
                        deploy=live_target_runtime_request.deploy,
                        no_cache=live_target_runtime_request.no_cache,
                        deploy_timeout_seconds=(live_target_runtime_request.deploy_timeout_seconds),
                        deploy_trigger=(
                            control_plane_live_target_runtime.trigger_and_wait_for_dokploy_target_deploy
                        ),
                    )
                except control_plane_live_target_runtime.LiveTargetRuntimeError as error:
                    status_code = 400
                    if error.code in {
                        "runtime_key_safety_unavailable",
                        "runtime_environment_unavailable",
                        "dokploy_target_read_failed",
                    }:
                        status_code = 503
                    return _json_response(
                        start_response=start_response,
                        status_code=status_code,
                        payload={
                            "status": "rejected",
                            "trace_id": request_trace_id,
                            "error": {
                                "code": error.code,
                                "message": str(error),
                            },
                        },
                    )
                tracked_target = driver_result.get("tracked_target")
                result = {}
                if isinstance(tracked_target, dict):
                    result = {
                        "target_id": str(tracked_target.get("target_id", "")),
                        "target_type": str(tracked_target.get("target_type", "")),
                    }
            elif path == "/v1/product-onboarding/apply":
                onboarding_request = ProductOnboardingApplyEnvelope.model_validate(payload)
                if not isinstance(record_store, PostgresRecordStore):
                    return _json_response(
                        start_response=start_response,
                        status_code=503,
                        payload={
                            "status": "rejected",
                            "trace_id": request_trace_id,
                            "error": {
                                "code": "database_required",
                                "message": "Product onboarding writes require Launchplane database storage.",
                            },
                        },
                    )
                if not authz_policy.allows(
                    identity=identity,
                    action="launchplane_service_deploy.execute",
                    product=onboarding_request.product,
                    context=_LAUNCHPLANE_SERVICE_CONTEXT,
                ):
                    return _json_response(
                        start_response=start_response,
                        status_code=403,
                        payload={
                            "status": "rejected",
                            "trace_id": request_trace_id,
                            "error": {
                                "code": "authorization_denied",
                                "message": "Workflow cannot apply Launchplane product onboarding manifests.",
                            },
                        },
                    )
                idempotent_response = _check_idempotent_request(
                    record_store=record_store,
                    scope=request_scope,
                    route_path=path,
                    idempotency_key=request_idempotency_key,
                    request_fingerprint=request_fingerprint,
                    start_response=start_response,
                    trace_id=request_trace_id,
                )
                if idempotent_response is not None:
                    return idempotent_response
                onboarding_result = apply_product_onboarding_manifest(
                    record_store=record_store,
                    manifest=onboarding_request.manifest,
                )
                result, driver_result = (
                    control_plane_product_onboarding_service.build_product_onboarding_service_result(
                        onboarding_result
                    )
                )
            elif path == "/v1/drivers/launchplane/self-deploy":
                self_deploy_request = LaunchplaneSelfDeployEnvelope.model_validate(payload)
                if not authz_policy.allows(
                    identity=identity,
                    action="launchplane_service_deploy.execute",
                    product=self_deploy_request.product,
                    context=_LAUNCHPLANE_SERVICE_CONTEXT,
                ):
                    return _json_response(
                        start_response=start_response,
                        status_code=403,
                        payload={
                            "status": "rejected",
                            "trace_id": request_trace_id,
                            "error": {
                                "code": "authorization_denied",
                                "message": "Workflow cannot execute Launchplane self deploy.",
                            },
                        },
                    )
                idempotent_response = _check_idempotent_request(
                    record_store=record_store,
                    scope=request_scope,
                    route_path=path,
                    idempotency_key=request_idempotency_key,
                    request_fingerprint=request_fingerprint,
                    start_response=start_response,
                    trace_id=request_trace_id,
                )
                if idempotent_response is not None:
                    return idempotent_response
                result = _request_launchplane_self_deploy(
                    control_plane_root_path=resolved_root,
                    request=self_deploy_request.deploy,
                )
            elif path == _GENERIC_WEB_DEPLOY_ROUTE.route_path:
                generic_web_deploy_request = (
                    _GENERIC_WEB_DEPLOY_ROUTE.envelope_model.model_validate(payload)
                )
                resolved_driver_context = _resolve_descriptor_product_driver_context(
                    record_store=record_store,
                    route_path=path,
                    product=generic_web_deploy_request.deploy.product,
                    instance=generic_web_deploy_request.deploy.instance,
                    require_profile=True,
                )
                if resolved_driver_context.profile is None or resolved_driver_context.lane is None:
                    raise ProductDriverMismatchError(
                        "Generic web deploy requires a product profile lane."
                    )
                profile = resolved_driver_context.profile
                lane = resolved_driver_context.lane
                authorization_response = _driver_route_authorization_response(
                    authz_policy=authz_policy,
                    identity=identity,
                    route_path=path,
                    product=profile.product,
                    context=lane.context,
                    denial_message=_GENERIC_WEB_DEPLOY_ROUTE.denial_message,
                    start_response=start_response,
                    trace_id=request_trace_id,
                )
                if authorization_response is not None:
                    return authorization_response
                idempotent_response = _check_idempotent_request(
                    record_store=record_store,
                    scope=request_scope,
                    route_path=path,
                    idempotency_key=request_idempotency_key,
                    request_fingerprint=request_fingerprint,
                    start_response=start_response,
                    trace_id=request_trace_id,
                )
                if idempotent_response is not None:
                    return idempotent_response
                driver_result = execute_generic_web_deploy(
                    control_plane_root=resolved_root,
                    record_store=record_store,
                    request=generic_web_deploy_request.deploy,
                    profile=profile,
                    lane=lane,
                )
                result = {"deployment_record_id": driver_result.deployment_record_id}
            elif path == _GENERIC_WEB_PROD_PROMOTION_ROUTE.route_path:
                generic_web_promotion_request = (
                    _GENERIC_WEB_PROD_PROMOTION_ROUTE.envelope_model.model_validate(payload)
                )
                _profile, _source_lane, destination_lane = resolve_generic_web_promotion_lanes(
                    record_store=record_store,
                    request=generic_web_promotion_request.promotion,
                )
                if (
                    isinstance(identity, GitHubHumanIdentity)
                    and not generic_web_promotion_request.promotion.dry_run
                ):
                    return _json_response(
                        start_response=start_response,
                        status_code=403,
                        payload={
                            "status": "rejected",
                            "trace_id": request_trace_id,
                            "error": {
                                "code": "authorization_denied",
                                "message": "Launchplane UI can only dry-run generic-web prod promotions.",
                            },
                        },
                    )
                authorization_response = _driver_route_authorization_response(
                    authz_policy=authz_policy,
                    identity=identity,
                    route_path=path,
                    product=generic_web_promotion_request.product,
                    context=destination_lane.context,
                    denial_message=_GENERIC_WEB_PROD_PROMOTION_ROUTE.denial_message,
                    start_response=start_response,
                    trace_id=request_trace_id,
                )
                if authorization_response is not None:
                    return authorization_response
                idempotent_response = _check_idempotent_request(
                    record_store=record_store,
                    scope=request_scope,
                    route_path=path,
                    idempotency_key=request_idempotency_key,
                    request_fingerprint=request_fingerprint,
                    start_response=start_response,
                    trace_id=request_trace_id,
                )
                if idempotent_response is not None:
                    return idempotent_response
                driver_result = execute_generic_web_prod_promotion(
                    control_plane_root=resolved_root,
                    record_store=record_store,
                    request=generic_web_promotion_request.promotion,
                )
                result = {
                    "promotion_record_id": driver_result.promotion_record_id,
                    "deployment_record_id": driver_result.deployment_record_id,
                    "backup_record_id": driver_result.backup_record_id,
                    "inventory_record_id": driver_result.inventory_record_id,
                    "promotion_status": driver_result.promotion_status,
                    "deployment_status": driver_result.deployment_status,
                    "source_health_status": driver_result.source_health_status,
                    "destination_health_status": driver_result.destination_health_status,
                    "backup_status": driver_result.backup_status,
                    "release_status": driver_result.release_status,
                    "release_tag": driver_result.release_tag,
                    "release_url": driver_result.release_url,
                    "dry_run": driver_result.dry_run,
                }
            elif path == _GENERIC_WEB_PROD_PROMOTION_WORKFLOW_ROUTE.route_path:
                generic_web_workflow_request = (
                    _GENERIC_WEB_PROD_PROMOTION_WORKFLOW_ROUTE.envelope_model.model_validate(
                        payload
                    )
                )
                resolved_driver_context = _resolve_descriptor_product_driver_context(
                    record_store=record_store,
                    route_path=path,
                    product=generic_web_workflow_request.product,
                    context=generic_web_workflow_request.workflow.context,
                    require_profile=True,
                )
                if resolved_driver_context.profile is None or resolved_driver_context.lane is None:
                    raise ProductDriverMismatchError(
                        "Generic web promotion workflow requires a product profile lane."
                    )
                profile = resolved_driver_context.profile
                lane = resolved_driver_context.lane
                authorization_response = _driver_route_authorization_response(
                    authz_policy=authz_policy,
                    identity=identity,
                    route_path=path,
                    product=profile.product,
                    context=lane.context,
                    denial_message=_GENERIC_WEB_PROD_PROMOTION_WORKFLOW_ROUTE.denial_message,
                    start_response=start_response,
                    trace_id=request_trace_id,
                )
                if authorization_response is not None:
                    return authorization_response
                idempotent_response = _check_idempotent_request(
                    record_store=record_store,
                    scope=request_scope,
                    route_path=path,
                    idempotency_key=request_idempotency_key,
                    request_fingerprint=request_fingerprint,
                    start_response=start_response,
                    trace_id=request_trace_id,
                )
                if idempotent_response is not None:
                    return idempotent_response
                driver_result = dispatch_generic_web_promotion_workflow(
                    control_plane_root=resolved_root,
                    profile=profile,
                    request=generic_web_workflow_request.workflow,
                )
                result = driver_result.model_dump(mode="json")
            elif path in _PREVIEW_DESIRED_STATE_ROUTE_PATHS:
                generic_web_desired_state_request, profile, authorization_response = (
                    _authorize_generic_web_preview_route(
                        route_metadata=_generic_web_preview_desired_state_route_metadata(path),
                        payload=payload,
                        record_store=record_store,
                        authz_policy=authz_policy,
                        identity=identity,
                        start_response=start_response,
                        trace_id=request_trace_id,
                    )
                )
                if authorization_response is not None:
                    return authorization_response
                idempotent_response = _check_idempotent_request(
                    record_store=record_store,
                    scope=request_scope,
                    route_path=path,
                    idempotency_key=request_idempotency_key,
                    request_fingerprint=request_fingerprint,
                    start_response=start_response,
                    trace_id=request_trace_id,
                )
                if idempotent_response is not None:
                    return idempotent_response
                driver_result = discover_generic_web_preview_desired_state(
                    control_plane_root=resolved_root,
                    record_store=record_store,
                    request=generic_web_desired_state_request.desired_state,
                    discovered_at=_utc_now_timestamp(),
                    profile=profile,
                )
                preview_desired_state_id = _write_preview_desired_state_if_supported(
                    record_store=record_store,
                    record=driver_result,
                )
                result = {"preview_desired_state_id": preview_desired_state_id}
            elif path in _PREVIEW_INVENTORY_ROUTE_PATHS:
                generic_web_inventory_request, profile, authorization_response = (
                    _authorize_generic_web_preview_route(
                        route_metadata=_generic_web_preview_inventory_route_metadata(path),
                        payload=payload,
                        record_store=record_store,
                        authz_policy=authz_policy,
                        identity=identity,
                        start_response=start_response,
                        trace_id=request_trace_id,
                    )
                )
                if authorization_response is not None:
                    return authorization_response
                driver_result = execute_generic_web_preview_inventory(
                    control_plane_root=resolved_root,
                    record_store=record_store,
                    request=generic_web_inventory_request.inventory,
                    profile=profile,
                )
                preview_inventory_scan_id = _write_preview_inventory_scan_if_supported(
                    record_store=record_store,
                    context=driver_result.context,
                    source=driver_result.source,
                    preview_slugs=tuple(item.previewSlug for item in driver_result.previews),
                )
                result = {"preview_inventory_scan_id": preview_inventory_scan_id}
            elif path in _PREVIEW_REFRESH_ROUTE_PATHS:
                generic_web_refresh_request, profile, authorization_response = (
                    _authorize_generic_web_preview_route(
                        route_metadata=_generic_web_preview_refresh_route_metadata(path),
                        payload=payload,
                        record_store=record_store,
                        authz_policy=authz_policy,
                        identity=identity,
                        start_response=start_response,
                        trace_id=request_trace_id,
                    )
                )
                if authorization_response is not None:
                    return authorization_response
                idempotent_response = _check_idempotent_request(
                    record_store=record_store,
                    scope=request_scope,
                    route_path=path,
                    idempotency_key=request_idempotency_key,
                    request_fingerprint=request_fingerprint,
                    start_response=start_response,
                    trace_id=request_trace_id,
                )
                if idempotent_response is not None:
                    return idempotent_response
                _generic_web_preview_anchor_pr_number(
                    request=generic_web_refresh_request.refresh,
                    profile=profile,
                )
                driver_result = execute_generic_web_preview_refresh(
                    control_plane_root=resolved_root,
                    record_store=record_store,
                    request=generic_web_refresh_request.refresh,
                    profile=profile,
                )
                driver_result = GenericWebPreviewRefreshResult.model_validate(driver_result)
                result = _apply_generic_web_preview_refresh_records(
                    control_plane_root_path=resolved_root,
                    record_store=record_store,
                    request=generic_web_refresh_request.refresh,
                    driver_result=driver_result,
                    profile=profile,
                )
            elif path in _PREVIEW_READINESS_ROUTE_PATHS:
                generic_web_readiness_request, profile, authorization_response = (
                    _authorize_generic_web_preview_route(
                        route_metadata=_generic_web_preview_readiness_route_metadata(path),
                        payload=payload,
                        record_store=record_store,
                        authz_policy=authz_policy,
                        identity=identity,
                        start_response=start_response,
                        trace_id=request_trace_id,
                    )
                )
                if authorization_response is not None:
                    return authorization_response
                driver_result = evaluate_generic_web_preview_readiness(
                    control_plane_root=resolved_root,
                    record_store=record_store,
                    request=generic_web_readiness_request.readiness,
                    checked_at=_utc_now_timestamp(),
                    profile=profile,
                )
                result = {}
            elif path in _PREVIEW_DESTROY_ROUTE_PATHS:
                generic_web_destroy_request, profile, authorization_response = (
                    _authorize_generic_web_preview_route(
                        route_metadata=_generic_web_preview_destroy_route_metadata(path),
                        payload=payload,
                        record_store=record_store,
                        authz_policy=authz_policy,
                        identity=identity,
                        start_response=start_response,
                        trace_id=request_trace_id,
                    )
                )
                if authorization_response is not None:
                    return authorization_response
                idempotent_response = _check_idempotent_request(
                    record_store=record_store,
                    scope=request_scope,
                    route_path=path,
                    idempotency_key=request_idempotency_key,
                    request_fingerprint=request_fingerprint,
                    start_response=start_response,
                    trace_id=request_trace_id,
                )
                if idempotent_response is not None:
                    return idempotent_response
                driver_result = execute_generic_web_preview_destroy(
                    control_plane_root=resolved_root,
                    record_store=record_store,
                    request=generic_web_destroy_request.destroy,
                    profile=profile,
                )
                result = {}
            elif path == _ODOO_POST_DEPLOY_ROUTE.route_path:
                odoo_post_deploy_request = _ODOO_POST_DEPLOY_ROUTE.envelope_model.model_validate(
                    payload
                )
                _, authorization_response = _resolve_and_authorize_descriptor_route(
                    route_metadata=_ODOO_POST_DEPLOY_ROUTE,
                    record_store=record_store,
                    authz_policy=authz_policy,
                    identity=identity,
                    product=odoo_post_deploy_request.product,
                    authorization_context=odoo_post_deploy_request.post_deploy.context,
                    start_response=start_response,
                    trace_id=request_trace_id,
                )
                if authorization_response is not None:
                    return authorization_response
                idempotent_response = _check_idempotent_request(
                    record_store=record_store,
                    scope=request_scope,
                    route_path=path,
                    idempotency_key=request_idempotency_key,
                    request_fingerprint=request_fingerprint,
                    start_response=start_response,
                    trace_id=request_trace_id,
                )
                if idempotent_response is not None:
                    return idempotent_response
                driver_result = execute_odoo_post_deploy(
                    control_plane_root=resolved_root,
                    record_store=record_store,
                    request=odoo_post_deploy_request.post_deploy,
                )
                result = {
                    "transition": (
                        f"odoo-post-deploy:{driver_result.context}:{driver_result.instance}:{driver_result.phase}"
                    )
                }
            elif path == _ODOO_ARTIFACT_PUBLISH_ROUTE.route_path:
                odoo_publish_request = _ODOO_ARTIFACT_PUBLISH_ROUTE.envelope_model.model_validate(
                    payload
                )
                _, authorization_response = _resolve_and_authorize_descriptor_route(
                    route_metadata=_ODOO_ARTIFACT_PUBLISH_ROUTE,
                    record_store=record_store,
                    authz_policy=authz_policy,
                    identity=identity,
                    product=odoo_publish_request.product,
                    authorization_context=odoo_publish_request.publish.context,
                    start_response=start_response,
                    trace_id=request_trace_id,
                )
                if authorization_response is not None:
                    return authorization_response
                idempotent_response = _check_idempotent_request(
                    record_store=record_store,
                    scope=request_scope,
                    route_path=path,
                    idempotency_key=request_idempotency_key,
                    request_fingerprint=request_fingerprint,
                    start_response=start_response,
                    trace_id=request_trace_id,
                )
                if idempotent_response is not None:
                    return idempotent_response
                driver_result = ingest_odoo_artifact_publish_evidence(
                    record_store=cast(FilesystemRecordStore, record_store),
                    request=odoo_publish_request.publish,
                )
                result = {
                    "artifact_id": driver_result.artifact_id,
                    "publish_status": driver_result.status,
                    "image_repository": driver_result.image_repository,
                    "image_digest": driver_result.image_digest,
                    "source_commit": driver_result.source_commit,
                }
            elif path == _ODOO_ARTIFACT_PUBLISH_INPUTS_ROUTE.route_path:
                odoo_inputs_request = (
                    _ODOO_ARTIFACT_PUBLISH_INPUTS_ROUTE.envelope_model.model_validate(payload)
                )
                _, authorization_response = _resolve_and_authorize_descriptor_route(
                    route_metadata=_ODOO_ARTIFACT_PUBLISH_INPUTS_ROUTE,
                    record_store=record_store,
                    authz_policy=authz_policy,
                    identity=identity,
                    product=odoo_inputs_request.product,
                    authorization_context=odoo_inputs_request.inputs.context,
                    start_response=start_response,
                    trace_id=request_trace_id,
                )
                if authorization_response is not None:
                    return authorization_response
                idempotent_response = _check_idempotent_request(
                    record_store=record_store,
                    scope=request_scope,
                    route_path=path,
                    idempotency_key=request_idempotency_key,
                    request_fingerprint=request_fingerprint,
                    start_response=start_response,
                    trace_id=request_trace_id,
                )
                if idempotent_response is not None:
                    return idempotent_response
                result = build_odoo_artifact_publish_inputs(
                    control_plane_root=resolved_root,
                    request=odoo_inputs_request.inputs,
                )
                driver_result = result
            elif path == _ODOO_PROD_BACKUP_GATE_ROUTE.route_path:
                odoo_backup_gate_request = (
                    _ODOO_PROD_BACKUP_GATE_ROUTE.envelope_model.model_validate(payload)
                )
                _, authorization_response = _resolve_and_authorize_descriptor_route(
                    route_metadata=_ODOO_PROD_BACKUP_GATE_ROUTE,
                    record_store=record_store,
                    authz_policy=authz_policy,
                    identity=identity,
                    product=odoo_backup_gate_request.product,
                    authorization_context=odoo_backup_gate_request.backup_gate.context,
                    start_response=start_response,
                    trace_id=request_trace_id,
                )
                if authorization_response is not None:
                    return authorization_response
                idempotent_response = _check_idempotent_request(
                    record_store=record_store,
                    scope=request_scope,
                    route_path=path,
                    idempotency_key=request_idempotency_key,
                    request_fingerprint=request_fingerprint,
                    start_response=start_response,
                    trace_id=request_trace_id,
                )
                if idempotent_response is not None:
                    return idempotent_response
                driver_result = execute_odoo_prod_backup_gate(
                    control_plane_root=resolved_root,
                    record_store=record_store,
                    request=odoo_backup_gate_request.backup_gate,
                )
                result = {
                    "backup_record_id": driver_result.backup_record_id,
                    "backup_status": driver_result.backup_status,
                    "backup_root": driver_result.backup_root,
                    "database_dump_path": driver_result.database_dump_path,
                    "filestore_archive_path": driver_result.filestore_archive_path,
                    "manifest_path": driver_result.manifest_path,
                }
            elif path == _ODOO_PROD_PROMOTION_ROUTE.route_path:
                odoo_promotion_request = _ODOO_PROD_PROMOTION_ROUTE.envelope_model.model_validate(
                    payload
                )
                _, authorization_response = _resolve_and_authorize_descriptor_route(
                    route_metadata=_ODOO_PROD_PROMOTION_ROUTE,
                    record_store=record_store,
                    authz_policy=authz_policy,
                    identity=identity,
                    product=odoo_promotion_request.product,
                    authorization_context=odoo_promotion_request.promotion.context,
                    start_response=start_response,
                    trace_id=request_trace_id,
                )
                if authorization_response is not None:
                    return authorization_response
                idempotent_response = _check_idempotent_request(
                    record_store=record_store,
                    scope=request_scope,
                    route_path=path,
                    idempotency_key=request_idempotency_key,
                    request_fingerprint=request_fingerprint,
                    start_response=start_response,
                    trace_id=request_trace_id,
                )
                if idempotent_response is not None:
                    return idempotent_response
                driver_result = execute_odoo_prod_promotion(
                    control_plane_root=resolved_root,
                    state_dir=state_dir,
                    database_url=database_url,
                    record_store=cast(FilesystemRecordStore, record_store),
                    request=odoo_promotion_request.promotion,
                )
                result = {
                    "promotion_record_id": driver_result.promotion_record_id,
                    "deployment_record_id": driver_result.deployment_record_id,
                    "backup_record_id": driver_result.backup_record_id,
                    "release_tuple_id": driver_result.release_tuple_id,
                    "promotion_status": driver_result.promotion_status,
                    "deployment_status": driver_result.deployment_status,
                    "post_deploy_status": driver_result.post_deploy_status,
                    "destination_health_status": driver_result.destination_health_status,
                }
            elif path == _ODOO_PROD_ROLLBACK_ROUTE.route_path:
                odoo_rollback_request = _ODOO_PROD_ROLLBACK_ROUTE.envelope_model.model_validate(
                    payload
                )
                _, authorization_response = _resolve_and_authorize_descriptor_route(
                    route_metadata=_ODOO_PROD_ROLLBACK_ROUTE,
                    record_store=record_store,
                    authz_policy=authz_policy,
                    identity=identity,
                    product=odoo_rollback_request.product,
                    authorization_context=odoo_rollback_request.rollback.context,
                    start_response=start_response,
                    trace_id=request_trace_id,
                )
                if authorization_response is not None:
                    return authorization_response
                idempotent_response = _check_idempotent_request(
                    record_store=record_store,
                    scope=request_scope,
                    route_path=path,
                    idempotency_key=request_idempotency_key,
                    request_fingerprint=request_fingerprint,
                    start_response=start_response,
                    trace_id=request_trace_id,
                )
                if idempotent_response is not None:
                    return idempotent_response
                driver_result = execute_odoo_prod_rollback(
                    control_plane_root=resolved_root,
                    record_store=record_store,
                    request=odoo_rollback_request.rollback,
                )
                result = {
                    "promotion_record_id": driver_result.promotion_record_id,
                    "deployment_record_id": driver_result.deployment_record_id,
                    "rollback_status": driver_result.rollback_status,
                    "rollback_health_status": driver_result.rollback_health_status,
                }
            elif path == _VERIREEL_TESTING_DEPLOY_ROUTE.route_path:
                verireel_testing_deploy_request = (
                    _VERIREEL_TESTING_DEPLOY_ROUTE.envelope_model.model_validate(payload)
                )
                _resolve_descriptor_product_driver_context(
                    record_store=record_store,
                    route_path=path,
                    product=verireel_testing_deploy_request.product,
                    context=verireel_testing_deploy_request.deploy.context,
                    instance=verireel_testing_deploy_request.deploy.instance,
                )
                authorization_response = _driver_route_authorization_response(
                    authz_policy=authz_policy,
                    identity=identity,
                    route_path=path,
                    product=verireel_testing_deploy_request.product,
                    context=verireel_testing_deploy_request.deploy.context,
                    denial_message=_VERIREEL_TESTING_DEPLOY_ROUTE.denial_message,
                    start_response=start_response,
                    trace_id=request_trace_id,
                )
                if authorization_response is not None:
                    return authorization_response
                idempotent_response = _check_idempotent_request(
                    record_store=record_store,
                    scope=request_scope,
                    route_path=path,
                    idempotency_key=request_idempotency_key,
                    request_fingerprint=request_fingerprint,
                    start_response=start_response,
                    trace_id=request_trace_id,
                )
                if idempotent_response is not None:
                    return idempotent_response
                driver_result = execute_verireel_stable_deploy(
                    control_plane_root=resolved_root,
                    record_store=cast(FilesystemRecordStore, record_store),
                    request=verireel_testing_deploy_request.deploy,
                )
                result = {"deployment_record_id": driver_result.deployment_record_id}
            elif path == _VERIREEL_TESTING_VERIFICATION_ROUTE.route_path:
                verireel_testing_verification_request = (
                    _VERIREEL_TESTING_VERIFICATION_ROUTE.envelope_model.model_validate(payload)
                )
                _resolve_descriptor_product_driver_context(
                    record_store=record_store,
                    route_path=path,
                    product=verireel_testing_verification_request.product,
                    context=verireel_testing_verification_request.verification.context,
                    instance=verireel_testing_verification_request.verification.instance,
                )
                authorization_response = _driver_route_authorization_response(
                    authz_policy=authz_policy,
                    identity=identity,
                    route_path=path,
                    product=verireel_testing_verification_request.product,
                    context=verireel_testing_verification_request.verification.context,
                    denial_message=_VERIREEL_TESTING_VERIFICATION_ROUTE.denial_message,
                    start_response=start_response,
                    trace_id=request_trace_id,
                )
                if authorization_response is not None:
                    return authorization_response
                idempotent_response = _check_idempotent_request(
                    record_store=record_store,
                    scope=request_scope,
                    route_path=path,
                    idempotency_key=request_idempotency_key,
                    request_fingerprint=request_fingerprint,
                    start_response=start_response,
                    trace_id=request_trace_id,
                )
                if idempotent_response is not None:
                    return idempotent_response
                result = dict[str, object](
                    _apply_verireel_testing_verification_records(
                        record_store=record_store,
                        request=verireel_testing_verification_request.verification,
                    )
                )
            elif path == _VERIREEL_STABLE_ENVIRONMENT_ROUTE.route_path:
                verireel_environment_request = (
                    _VERIREEL_STABLE_ENVIRONMENT_ROUTE.envelope_model.model_validate(payload)
                )
                _resolve_descriptor_product_driver_context(
                    record_store=record_store,
                    route_path=path,
                    product=verireel_environment_request.product,
                    context=verireel_environment_request.environment.context,
                    instance=verireel_environment_request.environment.instance,
                )
                authorization_response = _driver_route_authorization_response(
                    authz_policy=authz_policy,
                    identity=identity,
                    route_path=path,
                    product=verireel_environment_request.product,
                    context=verireel_environment_request.environment.context,
                    denial_message=_VERIREEL_STABLE_ENVIRONMENT_ROUTE.denial_message,
                    start_response=start_response,
                    trace_id=request_trace_id,
                )
                if authorization_response is not None:
                    return authorization_response
                driver_result = resolve_verireel_stable_environment(
                    control_plane_root=resolved_root,
                    request=verireel_environment_request.environment,
                )
                result = {}
            elif path == _VERIREEL_RUNTIME_VERIFICATION_ROUTE.route_path:
                verireel_runtime_verification_request = (
                    _VERIREEL_RUNTIME_VERIFICATION_ROUTE.envelope_model.model_validate(payload)
                )
                _resolve_descriptor_product_driver_context(
                    record_store=record_store,
                    route_path=path,
                    product=verireel_runtime_verification_request.product,
                    context=verireel_runtime_verification_request.verification.context,
                    instance=verireel_runtime_verification_request.verification.instance,
                )
                authorization_response = _driver_route_authorization_response(
                    authz_policy=authz_policy,
                    identity=identity,
                    route_path=path,
                    product=verireel_runtime_verification_request.product,
                    context=verireel_runtime_verification_request.verification.context,
                    denial_message=_VERIREEL_RUNTIME_VERIFICATION_ROUTE.denial_message,
                    start_response=start_response,
                    trace_id=request_trace_id,
                )
                if authorization_response is not None:
                    return authorization_response
                driver_result = execute_verireel_rollout_verification(
                    control_plane_root=resolved_root,
                    request=verireel_runtime_verification_request.verification,
                )
                result = {}
            elif path == _VERIREEL_APP_MAINTENANCE_ROUTE.route_path:
                verireel_maintenance_request = (
                    _VERIREEL_APP_MAINTENANCE_ROUTE.envelope_model.model_validate(payload)
                )
                _resolve_descriptor_product_driver_context(
                    record_store=record_store,
                    route_path=path,
                    product=verireel_maintenance_request.product,
                )
                authorization_response = _driver_route_authorization_response(
                    authz_policy=authz_policy,
                    identity=identity,
                    route_path=path,
                    product=verireel_maintenance_request.product,
                    context=verireel_maintenance_request.maintenance.context,
                    denial_message=_VERIREEL_APP_MAINTENANCE_ROUTE.denial_message,
                    start_response=start_response,
                    trace_id=request_trace_id,
                )
                if authorization_response is not None:
                    return authorization_response
                idempotent_response = _check_idempotent_request(
                    record_store=record_store,
                    scope=request_scope,
                    route_path=path,
                    idempotency_key=request_idempotency_key,
                    request_fingerprint=request_fingerprint,
                    start_response=start_response,
                    trace_id=request_trace_id,
                )
                if idempotent_response is not None:
                    return idempotent_response
                driver_result = execute_verireel_app_maintenance(
                    control_plane_root=resolved_root,
                    request=verireel_maintenance_request.maintenance,
                )
                result = driver_result.model_dump(mode="json")
            elif path == _VERIREEL_PROD_DEPLOY_ROUTE.route_path:
                verireel_prod_deploy_request = (
                    _VERIREEL_PROD_DEPLOY_ROUTE.envelope_model.model_validate(payload)
                )
                _resolve_descriptor_product_driver_context(
                    record_store=record_store,
                    route_path=path,
                    product=verireel_prod_deploy_request.product,
                    context=verireel_prod_deploy_request.deploy.context,
                    instance=verireel_prod_deploy_request.deploy.instance,
                )
                authorization_response = _driver_route_authorization_response(
                    authz_policy=authz_policy,
                    identity=identity,
                    route_path=path,
                    product=verireel_prod_deploy_request.product,
                    context=verireel_prod_deploy_request.deploy.context,
                    denial_message=_VERIREEL_PROD_DEPLOY_ROUTE.denial_message,
                    start_response=start_response,
                    trace_id=request_trace_id,
                )
                if authorization_response is not None:
                    return authorization_response
                idempotent_response = _check_idempotent_request(
                    record_store=record_store,
                    scope=request_scope,
                    route_path=path,
                    idempotency_key=request_idempotency_key,
                    request_fingerprint=request_fingerprint,
                    start_response=start_response,
                    trace_id=request_trace_id,
                )
                if idempotent_response is not None:
                    return idempotent_response
                driver_result = execute_verireel_stable_deploy(
                    control_plane_root=resolved_root,
                    record_store=cast(FilesystemRecordStore, record_store),
                    request=verireel_prod_deploy_request.deploy,
                )
                result = {"deployment_record_id": driver_result.deployment_record_id}
            elif path == _VERIREEL_PROD_BACKUP_GATE_ROUTE.route_path:
                verireel_prod_backup_gate_request = (
                    _VERIREEL_PROD_BACKUP_GATE_ROUTE.envelope_model.model_validate(payload)
                )
                _resolve_descriptor_product_driver_context(
                    record_store=record_store,
                    route_path=path,
                    product=verireel_prod_backup_gate_request.product,
                    context=verireel_prod_backup_gate_request.backup_gate.context,
                    instance=verireel_prod_backup_gate_request.backup_gate.instance,
                )
                authorization_response = _driver_route_authorization_response(
                    authz_policy=authz_policy,
                    identity=identity,
                    route_path=path,
                    product=verireel_prod_backup_gate_request.product,
                    context=verireel_prod_backup_gate_request.backup_gate.context,
                    denial_message=_VERIREEL_PROD_BACKUP_GATE_ROUTE.denial_message,
                    start_response=start_response,
                    trace_id=request_trace_id,
                )
                if authorization_response is not None:
                    return authorization_response
                idempotent_response = _check_idempotent_request(
                    record_store=record_store,
                    scope=request_scope,
                    route_path=path,
                    idempotency_key=request_idempotency_key,
                    request_fingerprint=request_fingerprint,
                    start_response=start_response,
                    trace_id=request_trace_id,
                )
                if idempotent_response is not None:
                    return idempotent_response
                driver_result = execute_verireel_prod_backup_gate(
                    control_plane_root=resolved_root,
                    record_store=cast(FilesystemRecordStore, record_store),
                    request=verireel_prod_backup_gate_request.backup_gate,
                    run_async=True,
                )
                result = {"backup_gate_record_id": driver_result.backup_record_id}
            elif path == _VERIREEL_PROD_PROMOTION_ROUTE.route_path:
                verireel_prod_promotion_request = (
                    _VERIREEL_PROD_PROMOTION_ROUTE.envelope_model.model_validate(payload)
                )
                _resolve_descriptor_product_driver_context(
                    record_store=record_store,
                    route_path=path,
                    product=verireel_prod_promotion_request.product,
                    context=verireel_prod_promotion_request.promotion.context,
                    instance=verireel_prod_promotion_request.promotion.from_instance,
                )
                _resolve_descriptor_product_driver_context(
                    record_store=record_store,
                    route_path=path,
                    product=verireel_prod_promotion_request.product,
                    context=verireel_prod_promotion_request.promotion.context,
                    instance=verireel_prod_promotion_request.promotion.to_instance,
                )
                authorization_response = _driver_route_authorization_response(
                    authz_policy=authz_policy,
                    identity=identity,
                    route_path=path,
                    product=verireel_prod_promotion_request.product,
                    context=verireel_prod_promotion_request.promotion.context,
                    denial_message=_VERIREEL_PROD_PROMOTION_ROUTE.denial_message,
                    start_response=start_response,
                    trace_id=request_trace_id,
                )
                if authorization_response is not None:
                    return authorization_response
                idempotent_response = _check_idempotent_request(
                    record_store=record_store,
                    scope=request_scope,
                    route_path=path,
                    idempotency_key=request_idempotency_key,
                    request_fingerprint=request_fingerprint,
                    start_response=start_response,
                    trace_id=request_trace_id,
                )
                if idempotent_response is not None:
                    return idempotent_response
                driver_result = execute_verireel_prod_promotion(
                    control_plane_root=resolved_root,
                    record_store=cast(FilesystemRecordStore, record_store),
                    request=verireel_prod_promotion_request.promotion,
                )
                result = {
                    "promotion_record_id": driver_result.promotion_record_id,
                    "deployment_record_id": driver_result.deployment_record_id,
                }
            elif path == _VERIREEL_PROD_ROLLBACK_ROUTE.route_path:
                verireel_prod_rollback_request = (
                    _VERIREEL_PROD_ROLLBACK_ROUTE.envelope_model.model_validate(payload)
                )
                _resolve_descriptor_product_driver_context(
                    record_store=record_store,
                    route_path=path,
                    product=verireel_prod_rollback_request.product,
                    context=verireel_prod_rollback_request.rollback.context,
                    instance=verireel_prod_rollback_request.rollback.instance,
                )
                authorization_response = _driver_route_authorization_response(
                    authz_policy=authz_policy,
                    identity=identity,
                    route_path=path,
                    product=verireel_prod_rollback_request.product,
                    context=verireel_prod_rollback_request.rollback.context,
                    denial_message=_VERIREEL_PROD_ROLLBACK_ROUTE.denial_message,
                    start_response=start_response,
                    trace_id=request_trace_id,
                )
                if authorization_response is not None:
                    return authorization_response
                idempotent_response = _check_idempotent_request(
                    record_store=record_store,
                    scope=request_scope,
                    route_path=path,
                    idempotency_key=request_idempotency_key,
                    request_fingerprint=request_fingerprint,
                    start_response=start_response,
                    trace_id=request_trace_id,
                )
                if idempotent_response is not None:
                    return idempotent_response
                driver_result = execute_verireel_prod_rollback(
                    control_plane_root=resolved_root,
                    record_store=cast(FilesystemRecordStore, record_store),
                    request=verireel_prod_rollback_request.rollback,
                )
                result = {
                    "promotion_record_id": driver_result.promotion_record_id,
                    "backup_record_id": driver_result.backup_record_id,
                }
            elif path == _VERIREEL_PREVIEW_REFRESH_ROUTE.route_path:
                verireel_preview_refresh_request = (
                    _VERIREEL_PREVIEW_REFRESH_ROUTE.envelope_model.model_validate(payload)
                )
                _resolve_descriptor_product_driver_context(
                    record_store=record_store,
                    route_path=path,
                    product=verireel_preview_refresh_request.product,
                )
                authorization_response = _driver_route_authorization_response(
                    authz_policy=authz_policy,
                    identity=identity,
                    route_path=path,
                    product=verireel_preview_refresh_request.product,
                    context=verireel_preview_refresh_request.refresh.context,
                    denial_message=_VERIREEL_PREVIEW_REFRESH_ROUTE.denial_message,
                    start_response=start_response,
                    trace_id=request_trace_id,
                )
                if authorization_response is not None:
                    return authorization_response
                idempotent_response = _check_idempotent_request(
                    record_store=record_store,
                    scope=request_scope,
                    route_path=path,
                    idempotency_key=request_idempotency_key,
                    request_fingerprint=request_fingerprint,
                    start_response=start_response,
                    trace_id=request_trace_id,
                )
                if idempotent_response is not None:
                    return idempotent_response
                driver_result = execute_verireel_preview_refresh(
                    control_plane_root=resolved_root,
                    record_store=record_store
                    if isinstance(record_store, PostgresRecordStore)
                    else None,
                    request=verireel_preview_refresh_request.refresh,
                )
                result = _apply_verireel_preview_refresh_records(
                    control_plane_root_path=resolved_root,
                    record_store=record_store,
                    request=verireel_preview_refresh_request.refresh,
                    driver_result=driver_result,
                )
            elif path == _VERIREEL_PREVIEW_INVENTORY_ROUTE.route_path:
                verireel_preview_inventory_request = (
                    _VERIREEL_PREVIEW_INVENTORY_ROUTE.envelope_model.model_validate(payload)
                )
                _resolve_descriptor_product_driver_context(
                    record_store=record_store,
                    route_path=path,
                    product=verireel_preview_inventory_request.product,
                )
                authorization_response = _driver_route_authorization_response(
                    authz_policy=authz_policy,
                    identity=identity,
                    route_path=path,
                    product=verireel_preview_inventory_request.product,
                    context=verireel_preview_inventory_request.inventory.context,
                    denial_message=_VERIREEL_PREVIEW_INVENTORY_ROUTE.denial_message,
                    start_response=start_response,
                    trace_id=request_trace_id,
                )
                if authorization_response is not None:
                    return authorization_response
                driver_result = execute_verireel_preview_inventory(
                    control_plane_root=resolved_root,
                    request=verireel_preview_inventory_request.inventory,
                )
                preview_inventory_scan_id = _write_preview_inventory_scan_if_supported(
                    record_store=record_store,
                    context=driver_result.context,
                    source="verireel-preview-inventory",
                    preview_slugs=tuple(item.previewSlug for item in driver_result.previews),
                )
                result = {"preview_inventory_scan_id": preview_inventory_scan_id}
            elif path == _VERIREEL_PREVIEW_DESTROY_ROUTE.route_path:
                verireel_preview_destroy_request = (
                    _VERIREEL_PREVIEW_DESTROY_ROUTE.envelope_model.model_validate(payload)
                )
                _resolve_descriptor_product_driver_context(
                    record_store=record_store,
                    route_path=path,
                    product=verireel_preview_destroy_request.product,
                )
                authorization_response = _driver_route_authorization_response(
                    authz_policy=authz_policy,
                    identity=identity,
                    route_path=path,
                    product=verireel_preview_destroy_request.product,
                    context=verireel_preview_destroy_request.destroy.context,
                    denial_message=_VERIREEL_PREVIEW_DESTROY_ROUTE.denial_message,
                    start_response=start_response,
                    trace_id=request_trace_id,
                )
                if authorization_response is not None:
                    return authorization_response
                idempotent_response = _check_idempotent_request(
                    record_store=record_store,
                    scope=request_scope,
                    route_path=path,
                    idempotency_key=request_idempotency_key,
                    request_fingerprint=request_fingerprint,
                    start_response=start_response,
                    trace_id=request_trace_id,
                )
                if idempotent_response is not None:
                    return idempotent_response
                driver_result = execute_verireel_preview_destroy(
                    control_plane_root=resolved_root,
                    request=verireel_preview_destroy_request.destroy,
                )
                result = _apply_verireel_preview_destroy_records(
                    record_store=record_store,
                    request=verireel_preview_destroy_request.destroy,
                    driver_result=driver_result,
                )
            elif path == _VERIREEL_PREVIEW_VERIFICATION_ROUTE.route_path:
                verireel_preview_verification_request = (
                    _VERIREEL_PREVIEW_VERIFICATION_ROUTE.envelope_model.model_validate(payload)
                )
                _resolve_descriptor_product_driver_context(
                    record_store=record_store,
                    route_path=path,
                    product=verireel_preview_verification_request.product,
                )
                authorization_response = _driver_route_authorization_response(
                    authz_policy=authz_policy,
                    identity=identity,
                    route_path=path,
                    product=verireel_preview_verification_request.product,
                    context=verireel_preview_verification_request.verification.context,
                    denial_message=_VERIREEL_PREVIEW_VERIFICATION_ROUTE.denial_message,
                    start_response=start_response,
                    trace_id=request_trace_id,
                )
                if authorization_response is not None:
                    return authorization_response
                idempotent_response = _check_idempotent_request(
                    record_store=record_store,
                    scope=request_scope,
                    route_path=path,
                    idempotency_key=request_idempotency_key,
                    request_fingerprint=request_fingerprint,
                    start_response=start_response,
                    trace_id=request_trace_id,
                )
                if idempotent_response is not None:
                    return idempotent_response
                result = _apply_verireel_preview_verification_records(
                    control_plane_root_path=resolved_root,
                    record_store=record_store,
                    request=verireel_preview_verification_request.verification,
                )
            elif path == "/v1/product-profiles/context-cutover/apply":
                context_cutover_request = control_plane_product_context_cutover.ProductContextCutoverRequest.model_validate(
                    payload
                )
                if not isinstance(record_store, PostgresRecordStore):
                    return _json_response(
                        start_response=start_response,
                        status_code=503,
                        payload={
                            "status": "rejected",
                            "trace_id": request_trace_id,
                            "error": {
                                "code": "database_required",
                                "message": "Product context cutover requires Launchplane database storage.",
                            },
                        },
                    )
                if not authz_policy.allows(
                    identity=identity,
                    action="product_profile.write",
                    product=context_cutover_request.product,
                    context=_LAUNCHPLANE_SERVICE_CONTEXT,
                ):
                    return _json_response(
                        start_response=start_response,
                        status_code=403,
                        payload={
                            "status": "rejected",
                            "trace_id": request_trace_id,
                            "error": {
                                "code": "authorization_denied",
                                "message": "Workflow cannot cut over the requested product profile context.",
                            },
                        },
                    )
                idempotent_response = _check_idempotent_request(
                    record_store=record_store,
                    scope=request_scope,
                    route_path=path,
                    idempotency_key=request_idempotency_key,
                    request_fingerprint=request_fingerprint,
                    start_response=start_response,
                    trace_id=request_trace_id,
                )
                if idempotent_response is not None:
                    return idempotent_response
                profile = record_store.read_product_profile_record(context_cutover_request.product)
                if not _product_profile_context_cutover_contexts_allowed(
                    profile=profile,
                    source_context=context_cutover_request.source_context,
                    target_context=context_cutover_request.target_context,
                    preview_context="",
                ):
                    return _json_response(
                        start_response=start_response,
                        status_code=403,
                        payload={
                            "status": "rejected",
                            "trace_id": request_trace_id,
                            "error": {
                                "code": "context_not_in_product_boundary",
                                "message": "Requested cutover contexts are not owned by the product profile.",
                            },
                        },
                    )
                try:
                    driver_result = (
                        control_plane_product_context_cutover.apply_product_context_cutover(
                            record_store=record_store,
                            request=context_cutover_request,
                        )
                    )
                except ValueError:
                    return _json_response(
                        start_response=start_response,
                        status_code=400,
                        payload={
                            "status": "rejected",
                            "trace_id": request_trace_id,
                            "error": {
                                "code": "invalid_context_cutover_request",
                                "message": "Product context cutover context_cutover_request is invalid.",
                            },
                        },
                    )
                result = {"product_profile": context_cutover_request.product}
            elif path == "/v1/product-profiles/legacy-context-cleanup/apply":
                legacy_cleanup_request = control_plane_product_context_cutover.LegacyContextCleanupRequest.model_validate(
                    payload
                )
                if not isinstance(record_store, PostgresRecordStore):
                    return _json_response(
                        start_response=start_response,
                        status_code=503,
                        payload={
                            "status": "rejected",
                            "trace_id": request_trace_id,
                            "error": {
                                "code": "database_required",
                                "message": "Legacy context cleanup requires Launchplane database storage.",
                            },
                        },
                    )
                if not authz_policy.allows(
                    identity=identity,
                    action="product_profile.write",
                    product=legacy_cleanup_request.product,
                    context=_LAUNCHPLANE_SERVICE_CONTEXT,
                ):
                    return _json_response(
                        start_response=start_response,
                        status_code=403,
                        payload={
                            "status": "rejected",
                            "trace_id": request_trace_id,
                            "error": {
                                "code": "authorization_denied",
                                "message": "Workflow cannot clean up the requested legacy product context.",
                            },
                        },
                    )
                idempotent_response = _check_idempotent_request(
                    record_store=record_store,
                    scope=request_scope,
                    route_path=path,
                    idempotency_key=request_idempotency_key,
                    request_fingerprint=request_fingerprint,
                    start_response=start_response,
                    trace_id=request_trace_id,
                )
                if idempotent_response is not None:
                    return idempotent_response
                try:
                    driver_result = (
                        control_plane_product_context_cutover.apply_legacy_context_cleanup(
                            record_store=record_store,
                            request=legacy_cleanup_request,
                        )
                    )
                except control_plane_product_context_cutover.LegacyContextCleanupBoundaryError:
                    return _json_response(
                        start_response=start_response,
                        status_code=403,
                        payload={
                            "status": "rejected",
                            "trace_id": request_trace_id,
                            "error": {
                                "code": "context_not_in_product_boundary",
                                "message": "Requested cleanup contexts are not in the product cleanup boundary.",
                            },
                        },
                    )
                except ValueError:
                    return _json_response(
                        start_response=start_response,
                        status_code=400,
                        payload={
                            "status": "rejected",
                            "trace_id": request_trace_id,
                            "error": {
                                "code": "invalid_legacy_context_cleanup_request",
                                "message": "Legacy context cleanup legacy_cleanup_request is invalid.",
                            },
                        },
                    )
                result = {"product_profile": legacy_cleanup_request.product}
            elif path == "/v1/product-profiles":
                product_profile_request = LaunchplaneProductProfileRecord.model_validate(payload)
                if not authz_policy.allows(
                    identity=identity,
                    action="product_profile.write",
                    product=product_profile_request.product,
                    context=_LAUNCHPLANE_SERVICE_CONTEXT,
                ):
                    return _json_response(
                        start_response=start_response,
                        status_code=403,
                        payload={
                            "status": "rejected",
                            "trace_id": request_trace_id,
                            "error": {
                                "code": "authorization_denied",
                                "message": "Workflow cannot write the requested product profile.",
                            },
                        },
                    )
                idempotent_response = _check_idempotent_request(
                    record_store=record_store,
                    scope=request_scope,
                    route_path=path,
                    idempotency_key=request_idempotency_key,
                    request_fingerprint=request_fingerprint,
                    start_response=start_response,
                    trace_id=request_trace_id,
                )
                if idempotent_response is not None:
                    return idempotent_response
                record_store.write_product_profile_record(product_profile_request)
                result = {"product_profile": product_profile_request.product}
            elif path == "/v1/evidence/promotions":
                promotion_request = PromotionEvidenceEnvelope.model_validate(payload)
                if not authz_policy.allows(
                    identity=identity,
                    action="promotion.write",
                    product=promotion_request.product,
                    context=promotion_request.promotion.context,
                ):
                    return _json_response(
                        start_response=start_response,
                        status_code=403,
                        payload={
                            "status": "rejected",
                            "trace_id": request_trace_id,
                            "error": {
                                "code": "authorization_denied",
                                "message": (
                                    "Workflow cannot write promotion evidence for the requested"
                                    " product/context."
                                ),
                            },
                        },
                    )
                idempotent_response = _check_idempotent_request(
                    record_store=record_store,
                    scope=request_scope,
                    route_path=path,
                    idempotency_key=request_idempotency_key,
                    request_fingerprint=request_fingerprint,
                    start_response=start_response,
                    trace_id=request_trace_id,
                )
                if idempotent_response is not None:
                    return idempotent_response
                result = dict[str, object](
                    apply_promotion_evidence(
                        record_store=record_store,
                        promotion_record=promotion_request.promotion,
                    )
                )
            elif path == "/v1/evidence/previews/generations":
                preview_generation_request = PreviewGenerationEvidenceEnvelope.model_validate(
                    payload
                )
                if not authz_policy.allows(
                    identity=identity,
                    action="preview_generation.write",
                    product=preview_generation_request.product,
                    context=preview_generation_request.preview.context,
                ):
                    return _json_response(
                        start_response=start_response,
                        status_code=403,
                        payload={
                            "status": "rejected",
                            "trace_id": request_trace_id,
                            "error": {
                                "code": "authorization_denied",
                                "message": (
                                    "Workflow cannot write preview generation evidence for the"
                                    " requested product/context."
                                ),
                            },
                        },
                    )
                idempotent_response = _check_idempotent_request(
                    record_store=record_store,
                    scope=request_scope,
                    route_path=path,
                    idempotency_key=request_idempotency_key,
                    request_fingerprint=request_fingerprint,
                    start_response=start_response,
                    trace_id=request_trace_id,
                )
                if idempotent_response is not None:
                    return idempotent_response
                result = apply_launchplane_generation_evidence(
                    control_plane_root_path=resolved_root,
                    record_store=record_store,
                    preview_request=preview_generation_request.preview,
                    generation_request=preview_generation_request.generation,
                )
            elif path == "/v1/previews/lifecycle-plan":
                preview_lifecycle_plan_request = PreviewLifecyclePlanEnvelope.model_validate(
                    payload
                )
                if not authz_policy.allows(
                    identity=identity,
                    action="preview_lifecycle.plan",
                    product=preview_lifecycle_plan_request.product,
                    context=preview_lifecycle_plan_request.context,
                ):
                    return _json_response(
                        start_response=start_response,
                        status_code=403,
                        payload={
                            "status": "rejected",
                            "trace_id": request_trace_id,
                            "error": {
                                "code": "authorization_denied",
                                "message": (
                                    "Workflow cannot plan preview lifecycle for the requested"
                                    " product/context."
                                ),
                            },
                        },
                    )
                idempotent_response = _check_idempotent_request(
                    record_store=record_store,
                    scope=request_scope,
                    route_path=path,
                    idempotency_key=request_idempotency_key,
                    request_fingerprint=request_fingerprint,
                    start_response=start_response,
                    trace_id=request_trace_id,
                )
                if idempotent_response is not None:
                    return idempotent_response
                driver_result = build_preview_lifecycle_plan(
                    product=preview_lifecycle_plan_request.product,
                    context=preview_lifecycle_plan_request.context,
                    planned_at=_utc_now_timestamp(),
                    source=preview_lifecycle_plan_request.source,
                    desired_previews=preview_lifecycle_plan_request.desired_previews,
                    desired_state_id=preview_lifecycle_plan_request.desired_state_id,
                    latest_inventory_scan=_latest_preview_inventory_scan(
                        record_store=record_store,
                        context_name=preview_lifecycle_plan_request.context,
                    ),
                )
                preview_lifecycle_plan_id = _write_preview_lifecycle_plan_if_supported(
                    record_store=record_store,
                    record=driver_result,
                )
                result = {"preview_lifecycle_plan_id": preview_lifecycle_plan_id}
            elif path == "/v1/previews/desired-state":
                preview_desired_state_request = PreviewDesiredStateEnvelope.model_validate(payload)
                if not authz_policy.allows(
                    identity=identity,
                    action="preview_desired_state.discover",
                    product=preview_desired_state_request.product,
                    context=preview_desired_state_request.context,
                ):
                    return _json_response(
                        start_response=start_response,
                        status_code=403,
                        payload={
                            "status": "rejected",
                            "trace_id": request_trace_id,
                            "error": {
                                "code": "authorization_denied",
                                "message": (
                                    "Workflow cannot discover preview desired state for the requested"
                                    " product/context."
                                ),
                            },
                        },
                    )
                idempotent_response = _check_idempotent_request(
                    record_store=record_store,
                    scope=request_scope,
                    route_path=path,
                    idempotency_key=request_idempotency_key,
                    request_fingerprint=request_fingerprint,
                    start_response=start_response,
                    trace_id=request_trace_id,
                )
                if idempotent_response is not None:
                    return idempotent_response
                driver_result = discover_github_preview_desired_state(
                    control_plane_root=resolved_root,
                    product=preview_desired_state_request.product,
                    context=preview_desired_state_request.context,
                    source=preview_desired_state_request.source,
                    discovered_at=_utc_now_timestamp(),
                    repository=preview_desired_state_request.repository,
                    label=preview_desired_state_request.label,
                    anchor_repo=preview_desired_state_request.anchor_repo,
                    preview_slug_prefix=preview_desired_state_request.preview_slug_prefix,
                    max_pages=preview_desired_state_request.max_pages,
                )
                preview_desired_state_id = _write_preview_desired_state_if_supported(
                    record_store=record_store,
                    record=driver_result,
                )
                result = {"preview_desired_state_id": preview_desired_state_id}
            elif path == "/v1/previews/pr-feedback":
                preview_pr_feedback_request = PreviewPrFeedbackEnvelope.model_validate(payload)
                if not _allows_preview_pr_feedback_write(
                    authz_policy=authz_policy,
                    identity=identity,
                    product=preview_pr_feedback_request.product,
                    context=preview_pr_feedback_request.context,
                    status=preview_pr_feedback_request.status,
                ):
                    feedback_authz_action = "preview_pr_feedback.write"
                    return _json_response(
                        start_response=start_response,
                        status_code=403,
                        payload={
                            "status": "rejected",
                            "trace_id": request_trace_id,
                            "error": {
                                "code": "authorization_denied",
                                "message": (
                                    "Workflow cannot write preview PR feedback for the requested"
                                    " product/context."
                                ),
                            },
                            "authz": _authz_diagnostic_payload(
                                identity=identity,
                                authz_policy_sha256_value=resolved_authz_policy_sha256,
                                authz_policy_source=resolved_authz_policy_source,
                                action=feedback_authz_action,
                                product=preview_pr_feedback_request.product,
                                context=preview_pr_feedback_request.context,
                            ),
                        },
                    )
                idempotent_response = _check_idempotent_request(
                    record_store=record_store,
                    scope=request_scope,
                    route_path=path,
                    idempotency_key=request_idempotency_key,
                    request_fingerprint=request_fingerprint,
                    start_response=start_response,
                    trace_id=request_trace_id,
                )
                if idempotent_response is not None:
                    return idempotent_response
                driver_result = build_preview_pr_feedback_record(
                    control_plane_root=resolved_root,
                    product=preview_pr_feedback_request.product,
                    context=preview_pr_feedback_request.context,
                    source=preview_pr_feedback_request.source,
                    requested_at=_utc_now_timestamp(),
                    repository=preview_pr_feedback_request.repository,
                    anchor_repo=preview_pr_feedback_request.anchor_repo,
                    anchor_pr_number=preview_pr_feedback_request.anchor_pr_number,
                    anchor_pr_url=preview_pr_feedback_request.anchor_pr_url,
                    status=preview_pr_feedback_request.status,
                    marker=preview_pr_feedback_request.marker,
                    preview_url=preview_pr_feedback_request.preview_url,
                    immutable_image_reference=preview_pr_feedback_request.immutable_image_reference,
                    refresh_image_reference=preview_pr_feedback_request.refresh_image_reference,
                    revision=preview_pr_feedback_request.revision,
                    run_url=preview_pr_feedback_request.run_url,
                    failure_summary=preview_pr_feedback_request.failure_summary,
                    every_code_record_store=(
                        record_store if _supports_every_code_work_requests(record_store) else None
                    ),
                )
                preview_pr_feedback_id = _write_preview_pr_feedback_if_supported(
                    record_store=record_store,
                    record=driver_result,
                )
                result = {"preview_pr_feedback_id": preview_pr_feedback_id}
            elif path == "/v1/previews/lifecycle-cleanup":
                preview_lifecycle_cleanup_request = PreviewLifecycleCleanupEnvelope.model_validate(
                    payload
                )
                if not authz_policy.allows(
                    identity=identity,
                    action="preview_lifecycle.cleanup",
                    product=preview_lifecycle_cleanup_request.product,
                    context=preview_lifecycle_cleanup_request.context,
                ):
                    return _json_response(
                        start_response=start_response,
                        status_code=403,
                        payload={
                            "status": "rejected",
                            "trace_id": request_trace_id,
                            "error": {
                                "code": "authorization_denied",
                                "message": (
                                    "Workflow cannot clean preview lifecycle for the requested"
                                    " product/context."
                                ),
                            },
                        },
                    )
                idempotent_response = _check_idempotent_request(
                    record_store=record_store,
                    scope=request_scope,
                    route_path=path,
                    idempotency_key=request_idempotency_key,
                    request_fingerprint=request_fingerprint,
                    start_response=start_response,
                    trace_id=request_trace_id,
                )
                if idempotent_response is not None:
                    return idempotent_response
                plan = _latest_preview_lifecycle_plan(
                    record_store=record_store,
                    context_name=preview_lifecycle_cleanup_request.context,
                    plan_id=preview_lifecycle_cleanup_request.plan_id,
                )
                if plan is None or plan.product != preview_lifecycle_cleanup_request.product:
                    return _json_response(
                        start_response=start_response,
                        status_code=404,
                        payload={
                            "status": "rejected",
                            "trace_id": request_trace_id,
                            "error": {
                                "code": "not_found",
                                "message": "Preview lifecycle cleanup requires an existing plan for the requested product/context.",
                            },
                        },
                    )
                cleanup_driver_id = (
                    "verireel" if preview_lifecycle_cleanup_request.product == "verireel" else ""
                )
                cleanup_slug_template = "pr-{number}"
                try:
                    cleanup_profile = record_store.read_product_profile_record(
                        preview_lifecycle_cleanup_request.product
                    )
                    cleanup_driver_id = cleanup_profile.driver_id
                    cleanup_slug_template = cleanup_profile.preview.slug_template
                except FileNotFoundError:
                    pass
                driver_result = build_preview_lifecycle_cleanup_record(
                    plan=plan,
                    requested_at=_utc_now_timestamp(),
                    source=preview_lifecycle_cleanup_request.source,
                    apply=preview_lifecycle_cleanup_request.apply,
                    destroy_reason=preview_lifecycle_cleanup_request.destroy_reason,
                    control_plane_root=resolved_root,
                    record_store=record_store,
                    timeout_seconds=preview_lifecycle_cleanup_request.timeout_seconds,
                    driver_id=cleanup_driver_id,
                    preview_slug_template=cleanup_slug_template,
                )
                preview_lifecycle_cleanup_id = _write_preview_lifecycle_cleanup_if_supported(
                    record_store=record_store,
                    record=driver_result,
                )
                result = {"preview_lifecycle_cleanup_id": preview_lifecycle_cleanup_id}
            else:
                preview_destroyed_request = PreviewDestroyedEvidenceEnvelope.model_validate(payload)
                if not authz_policy.allows(
                    identity=identity,
                    action="preview_destroyed.write",
                    product=preview_destroyed_request.product,
                    context=preview_destroyed_request.destroy.context,
                ):
                    return _json_response(
                        start_response=start_response,
                        status_code=403,
                        payload={
                            "status": "rejected",
                            "trace_id": request_trace_id,
                            "error": {
                                "code": "authorization_denied",
                                "message": (
                                    "Workflow cannot write preview destroyed evidence for the"
                                    " requested product/context."
                                ),
                            },
                        },
                    )
                idempotent_response = _check_idempotent_request(
                    record_store=record_store,
                    scope=request_scope,
                    route_path=path,
                    idempotency_key=request_idempotency_key,
                    request_fingerprint=request_fingerprint,
                    start_response=start_response,
                    trace_id=request_trace_id,
                )
                if idempotent_response is not None:
                    return idempotent_response
                result = apply_launchplane_destroy_preview(
                    record_store=record_store,
                    request=preview_destroyed_request.destroy,
                )
        except (PermissionError, InvalidTokenError):
            return _json_response(
                start_response=start_response,
                status_code=401,
                payload={
                    "status": "rejected",
                    "trace_id": request_trace_id,
                    "error": {
                        "code": "authentication_required",
                        "message": "A valid GitHub OIDC token or browser session is required.",
                    },
                },
            )
        except FileNotFoundError:
            return _not_found_response(
                start_response=start_response,
                trace_id=request_trace_id,
                path=path,
            )
        except ValidationError:
            return _json_response(
                start_response=start_response,
                status_code=400,
                payload={
                    "status": "rejected",
                    "trace_id": request_trace_id,
                    "error": {
                        "code": "invalid_request",
                        "message": "Request payload failed validation.",
                    },
                },
            )
        except ProductDriverMismatchError:
            return _json_response(
                start_response=start_response,
                status_code=403,
                payload={
                    "status": "rejected",
                    "trace_id": request_trace_id,
                    "error": {
                        "code": "product_driver_mismatch",
                        "message": "Product is not configured for the requested driver route.",
                    },
                },
            )
        except MergeTrainGitHubError:
            return _json_response(
                start_response=start_response,
                status_code=502,
                payload={
                    "status": "rejected",
                    "trace_id": request_trace_id,
                    "error": {
                        "code": "github_request_failed",
                        "message": "GitHub merge train request failed; retry after upstream recovers.",
                    },
                },
            )
        except (ValueError, click.ClickException):
            return _json_response(
                start_response=start_response,
                status_code=400,
                payload={
                    "status": "rejected",
                    "trace_id": request_trace_id,
                    "error": {
                        "code": "invalid_request",
                        "message": "Request could not be completed.",
                    },
                },
            )
        except Exception:  # noqa: BLE001
            _LOGGER.exception(
                "Unexpected Launchplane service error", extra={"trace_id": request_trace_id}
            )
            return _json_response(
                start_response=start_response,
                status_code=500,
                payload={
                    "status": "rejected",
                    "trace_id": request_trace_id,
                    "error": {
                        "code": "internal_error",
                        "message": "Unexpected Launchplane service error. Use trace_id to inspect service logs.",
                    },
                },
            )
        accepted_payload = _accepted_payload(
            trace_id=request_trace_id,
            result=result,
            driver_result=driver_result,
        )
        should_store_idempotency = _should_store_idempotency_record(
            path=path,
            driver_result=driver_result,
        )
        if method == "POST" and request_idempotency_key and should_store_idempotency:
            _write_idempotency_record(
                record_store=record_store,
                scope=request_scope,
                route_path=path,
                idempotency_key=request_idempotency_key,
                request_fingerprint=request_fingerprint,
                response_status_code=202,
                response_trace_id=request_trace_id,
                response_payload=accepted_payload,
            )
        return _json_response(
            start_response=start_response,
            status_code=202,
            payload=accepted_payload,
        )

    return app


def serve_launchplane_service(
    *,
    state_dir: Path,
    policy_file: Path,
    host: str,
    port: int,
    audience: str,
    database_url: str | None = None,
) -> None:
    from control_plane.service_auth import GitHubOidcVerifier

    authz_policy = load_authz_policy(policy_file)
    verifier = GitHubOidcVerifier(audience=audience)
    work_graph_project_config = load_github_project_planning_facts_config_from_env(dict(os.environ))
    work_graph_planning_facts_provider = (
        (lambda: build_github_project_planning_facts(work_graph_project_config))
        if work_graph_project_config is not None
        else None
    )
    application = create_launchplane_service_app(
        state_dir=state_dir,
        verifier=verifier,
        authz_policy=authz_policy,
        database_url=database_url,
        work_graph_planning_facts_provider=work_graph_planning_facts_provider,
    )
    with make_server(
        host,
        port,
        cast(WSGIApplication, application),
        server_class=ThreadingWSGIServer,
    ) as server:
        click.echo(f"Launchplane service listening on http://{host}:{port}")
        server.serve_forever()
