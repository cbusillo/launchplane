from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
import re
import secrets
import uuid
from pathlib import Path
from typing import Any, BinaryIO, Callable, Iterable, Literal, Protocol, cast
from urllib.parse import parse_qs

import click
from a2wsgi import WSGIMiddleware
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from jwt import InvalidTokenError
from starlette.types import ASGIApp

from control_plane.http_app import (
    LaunchplaneAuthzPolicyRuntime,
    create_launchplane_fastapi_app,
    resolve_launchplane_authz_policy,
)
from control_plane import runtime_environments as control_plane_runtime_environments
from control_plane import secrets as control_plane_secrets
from control_plane.contracts.deployment_record import DeploymentRecord
from control_plane.contracts.every_code_work_request import (
    EveryCodeWorkRequestRecord,
    close_every_code_work_request_for_issue,
    close_every_code_work_request_for_pull_request,
)
from control_plane.contracts.every_code_preview_gate_record import (
    EveryCodePreviewGateRecord,
)
from control_plane.contracts.every_code_pr_feedback_record import (
    EveryCodePrFeedbackKind,
    EveryCodePrFeedbackRecord,
    build_every_code_pr_feedback_id,
)
from control_plane.contracts.idempotency_record import LaunchplaneIdempotencyRecord
from control_plane.contracts.idempotency_record import build_launchplane_idempotency_record_id
from control_plane.contracts.verireel_prod_backup_gate import (
    VeriReelProdBackupGateRequest,
)
from control_plane.contracts.merge_train_batch import (
    MergeTrainBatchCandidate,
    MergeTrainBatchCandidateRecord,
    MergeTrainBatchLandingPlan,
    MergeTrainBatchLandingPlanRecord,
    build_merge_train_batch_candidate,
    build_merge_train_batch_candidate_record,
    build_merge_train_batch_landing_plan,
    build_merge_train_batch_landing_plan_record,
)
from control_plane.contracts.merge_train_stack_collapse import (
    MergeTrainStackCollapsePlan,
    MergeTrainStackCollapsePlanRecord,
    build_merge_train_stack_collapse_plan,
    build_merge_train_stack_collapse_plan_record,
    execute_merge_train_stack_collapse_plan,
    reconcile_merge_train_stack_children_after_root_landing,
)
from control_plane.contracts.merge_train_run_record import build_merge_train_run_record
from control_plane.contracts.merge_train_policy import MergeTrainPolicy
from control_plane.contracts.merge_train_pr_feedback_record import (
    MergeTrainPrFeedbackEvent,
    MergeTrainPrFeedbackRecord,
    build_merge_train_pr_feedback_id,
    merge_train_pr_feedback_marker,
)
from control_plane.merge_train_policy_source import (
    MergeTrainPolicyStoreMissingError,
    resolve_merge_train_policy_record,
)
from control_plane.contracts.odoo_instance_override_record import (
    OdooConfigParameterOverride,
    OdooInstanceOverrideRecord,
    OdooOverrideValue,
    OdooWebsiteBootstrapPayload,
)
from control_plane.contracts.odoo_stable_bootstrap_operation import (
    OdooStableBootstrapOperationRecord,
    build_odoo_stable_bootstrap_operation_id,
)
from control_plane.contracts.odoo_stable_target_replacement import (
    OdooStableTargetReplacementApplyRequest,
    OdooStableTargetReplacementRequest,
)
from control_plane.contracts.odoo_stable_target_replacement_operation import (
    OdooStableTargetReplacementOperationRecord,
    build_odoo_stable_target_replacement_operation_id,
)
from control_plane.contracts.preview_mutation_request import (
    PreviewDestroyMutationRequest,
    PreviewGenerationMutationRequest,
    PreviewMutationRequest,
)
from control_plane.contracts.preview_inventory_scan_record import PreviewInventoryScanRecord
from control_plane.contracts.preview_lifecycle_plan_record import (
    PreviewLifecyclePlanRecord,
)
from control_plane.contracts.preview_lifecycle_cleanup_record import PreviewLifecycleCleanupRecord
from control_plane.contracts.preview_pr_feedback_record import (
    PreviewPrFeedbackRecord,
    PreviewPrFeedbackStatus,
)
from control_plane.contracts.preview_pr_feedback_notifications import (
    PreviewPrFeedbackNotificationAttemptRecord,
    PreviewPrFeedbackNotificationDeliveryStatus,
    PreviewPrFeedbackNotificationDestination,
    PreviewPrFeedbackNotificationEvent,
    PreviewPrFeedbackNotificationPolicyRecord,
    build_preview_pr_feedback_notification_attempt_id,
    preview_pr_feedback_notification_event,
)
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductLaneProfile,
)
from control_plane.contracts.promotion_record import (
    HealthcheckEvidence,
    PostDeployUpdateEvidence,
    ReleaseStatus,
)
from control_plane.drivers.registry import list_driver_descriptors, read_driver_descriptor
from control_plane.every_code_work_request_write import (
    EveryCodeWorkRequestCreateEnvelope,
    build_every_code_work_request_record,
)
from control_plane.notifications import post_discord_webhook, public_discord_url_error
from control_plane.drivers.dispatch import (
    _DescriptorDriverDispatchContext as _DescriptorDriverDispatchContext,
    _DescriptorDriverDispatchRoute as _DescriptorDriverDispatchRoute,
    _DescriptorDriverDispatchResult as _DescriptorDriverDispatchResult,
    _DriverRouteEnvelopeT as _DriverRouteEnvelopeT,
    _DriverRouteExecutionMetadata as _DriverRouteExecutionMetadata,
    _ProductRouteEnvelope as _ProductRouteEnvelope,
    _ResolvedProductDriverContext as _ResolvedProductDriverContext,
    _image_reference_tail as _image_reference_tail,
    _json_response as _json_response,
    _normalize_preview_verification_checked_urls as _normalize_preview_verification_checked_urls,
    _normalize_release_status as _normalize_release_status,
    _repo_token as _repo_token,
    _validate_driver_envelope_product as _validate_driver_envelope_product,
    DriverRouteDependencyNotFoundError as DriverRouteDependencyNotFoundError,
    ProductDriverMismatchError as ProductDriverMismatchError,
)
from control_plane.drivers.generic_web_dispatch import (
    GenericWebDeployEnvelope as GenericWebDeployEnvelope,
    GenericWebProdPromotionEnvelope as GenericWebProdPromotionEnvelope,
    GenericWebPromotionWorkflowEnvelope as GenericWebPromotionWorkflowEnvelope,
    GenericWebRollbackEnvelope as GenericWebRollbackEnvelope,
    GenericWebRollbackPlanEnvelope as GenericWebRollbackPlanEnvelope,
    GenericWebStableVerificationEnvelope as GenericWebStableVerificationEnvelope,
    GenericWebStableVerificationRequest as GenericWebStableVerificationRequest,
    GenericWebSourceRefDeployEnvelope as GenericWebSourceRefDeployEnvelope,
    _GENERIC_WEB_DEPLOY_ROUTE as _GENERIC_WEB_DEPLOY_ROUTE,
    _GENERIC_WEB_PROD_PROMOTION_ROUTE as _GENERIC_WEB_PROD_PROMOTION_ROUTE,
    _GENERIC_WEB_PROD_PROMOTION_WORKFLOW_ROUTE as _GENERIC_WEB_PROD_PROMOTION_WORKFLOW_ROUTE,
    _GENERIC_WEB_ROLLBACK_PLAN_ROUTE as _GENERIC_WEB_ROLLBACK_PLAN_ROUTE,
    _GENERIC_WEB_ROLLBACK_ROUTE as _GENERIC_WEB_ROLLBACK_ROUTE,
    _GENERIC_WEB_STABLE_VERIFICATION_ROUTE as _GENERIC_WEB_STABLE_VERIFICATION_ROUTE,
    _GENERIC_WEB_SOURCE_REF_DEPLOY_ROUTE as _GENERIC_WEB_SOURCE_REF_DEPLOY_ROUTE,
    _apply_generic_web_stable_verification_records as _apply_generic_web_stable_verification_records,
    _generic_web_post_deploy_executor_for_profile as _generic_web_post_deploy_executor_for_profile,
    _handle_generic_web_deploy as _handle_generic_web_deploy,
    _handle_generic_web_prod_promotion as _handle_generic_web_prod_promotion,
    _handle_generic_web_promotion_workflow as _handle_generic_web_promotion_workflow,
    _handle_generic_web_rollback as _handle_generic_web_rollback,
    _handle_generic_web_rollback_plan as _handle_generic_web_rollback_plan,
    _handle_generic_web_stable_verification as _handle_generic_web_stable_verification,
    _handle_generic_web_source_ref_deploy as _handle_generic_web_source_ref_deploy,
    _stable_verification_health_evidence as _stable_verification_health_evidence,
    _reject_human_live_generic_web_prod_promotion as _reject_human_live_generic_web_prod_promotion,
    _validate_generic_web_source_ref_deploy_lane as _validate_generic_web_source_ref_deploy_lane,
    _validate_stable_verification_request as _validate_stable_verification_request,
    _validate_generic_web_prod_promotion_lanes as _validate_generic_web_prod_promotion_lanes,
)
from control_plane.drivers.generic_web_preview_dispatch import (
    GenericWebPreviewDesiredStateEnvelope as GenericWebPreviewDesiredStateEnvelope,
    GenericWebPreviewDestroyEnvelope as GenericWebPreviewDestroyEnvelope,
    GenericWebPreviewInventoryEnvelope as GenericWebPreviewInventoryEnvelope,
    GenericWebPreviewReadinessEnvelope as GenericWebPreviewReadinessEnvelope,
    GenericWebPreviewRefreshEnvelope as GenericWebPreviewRefreshEnvelope,
    GenericWebPreviewVerificationEnvelope as GenericWebPreviewVerificationEnvelope,
    GenericWebPreviewVerificationRequest as GenericWebPreviewVerificationRequest,
    GenericWebPreviewVerificationResult as GenericWebPreviewVerificationResult,
    _GENERIC_WEB_PREVIEW_DESIRED_STATE_ROUTE as _GENERIC_WEB_PREVIEW_DESIRED_STATE_ROUTE,
    _GENERIC_WEB_PREVIEW_DESTROY_ROUTE as _GENERIC_WEB_PREVIEW_DESTROY_ROUTE,
    _GENERIC_WEB_PREVIEW_INVENTORY_ROUTE as _GENERIC_WEB_PREVIEW_INVENTORY_ROUTE,
    _GENERIC_WEB_PREVIEW_READINESS_ROUTE as _GENERIC_WEB_PREVIEW_READINESS_ROUTE,
    _GENERIC_WEB_PREVIEW_REFRESH_ROUTE as _GENERIC_WEB_PREVIEW_REFRESH_ROUTE,
    _GENERIC_WEB_PREVIEW_VERIFICATION_ROUTE as _GENERIC_WEB_PREVIEW_VERIFICATION_ROUTE,
    _apply_generic_web_preview_refresh_records as _apply_generic_web_preview_refresh_records,
    _apply_generic_web_preview_verification_records as _apply_generic_web_preview_verification_records,
    _generic_web_preview_anchor_head_sha as _generic_web_preview_anchor_head_sha,
    _generic_web_preview_anchor_pr_number as _generic_web_preview_anchor_pr_number,
    _generic_web_preview_anchor_pr_url as _generic_web_preview_anchor_pr_url,
    _generic_web_preview_anchor_repo as _generic_web_preview_anchor_repo,
    _generic_web_preview_manifest_fingerprint as _generic_web_preview_manifest_fingerprint,
    _generic_web_preview_refresh_failure_summary as _generic_web_preview_refresh_failure_summary,
    _generic_web_preview_refresh_mutation_requests as _generic_web_preview_refresh_mutation_requests,
    _generic_web_preview_refresh_states as _generic_web_preview_refresh_states,
    _generic_web_preview_refresh_timing as _generic_web_preview_refresh_timing,
    _handle_generic_web_preview_desired_state as _handle_generic_web_preview_desired_state,
    _handle_generic_web_preview_destroy as _handle_generic_web_preview_destroy,
    _handle_generic_web_preview_inventory as _handle_generic_web_preview_inventory,
    _handle_generic_web_preview_readiness as _handle_generic_web_preview_readiness,
    _handle_generic_web_preview_refresh as _handle_generic_web_preview_refresh,
    _handle_generic_web_preview_verification as _handle_generic_web_preview_verification,
    _validate_generic_web_preview_profile as _validate_generic_web_preview_profile,
    _write_preview_desired_state_if_supported as _write_preview_desired_state_if_supported,
    _write_preview_inventory_scan_if_supported as _write_preview_inventory_scan_if_supported,
)
from control_plane.launchplane_mutations import (
    LaunchplaneMutationStore,
    apply_launchplane_destroy_preview,
    apply_launchplane_generation_evidence,
    control_plane_root,
)
from control_plane.service_auth import (
    AgentAuthzDecision,
    BearerIdentityConfig,
    GitHubActionsIdentity,
    GitHubHumanIdentity,
    LaunchplaneAuthzPolicy,
    LaunchplaneIdentity,
    LocalAdminIdentity,
    LocalOperatorIdentity,
    TerminalAgentIdentity,
    TokenVerifier,
    agent_authz_audit,
    bearer_identity_from_token,
    load_authz_policy,
    read_bearer_token,
)
from control_plane.service_human_auth import (
    GitHubOAuthClient,
    GitHubOAuthConfig,
    HumanSessionManager,
    HumanSessionStore,
    InMemoryHumanSessionStore,
    LaunchplaneHumanSession,
    OAuthLoginStateStore,
    build_pkce_verifier,
    load_github_oauth_config_from_env,
)
from control_plane.storage.factory import build_shared_record_store
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.ui_static_http import serve_ui_route
from control_plane.work_graph_github_projects import (
    build_github_project_planning_facts,
    load_github_project_planning_facts_config_from_env,
)
from control_plane.work_graph_issue_inbox import (
    build_github_issue_inbox_read_model,
    load_github_issue_inbox_config_from_env,
    reconcile_github_issue_inbox,
)
from control_plane.workflows.launchplane_self_deploy import (
    LaunchplaneSelfDeployRequest,
    execute_launchplane_self_deploy,
)
from control_plane.merge_train import MergeTrainDryRunResult
from control_plane.merge_train import MergeTrainDryRunSnapshot
from control_plane.merge_train import build_merge_train_dry_run_result
from control_plane.merge_train import discover_merge_train_stack
from control_plane.merge_train_github import (
    GitHubMergeTrainClient,
    GitHubMergeTrainSnapshotReader,
    MergeTrainGitHubError,
    MergeTrainGitHubStaleHeadError,
    MergeTrainGitHubTransport,
    UrllibMergeTrainGitHubTransport,
)
from control_plane.workflows.merge_train_worker import (
    MergeTrainWorkerClients,
    run_merge_train_worker_step,
)
from control_plane.workflows.merge_train_controller import (
    latest_merge_train_batch_candidate_progress_record as controller_latest_merge_train_batch_candidate_progress_record,
    latest_merge_train_batch_landing_progress_record as controller_latest_merge_train_batch_landing_progress_record,
    latest_merge_train_stack_collapse_progress_record as controller_latest_merge_train_stack_collapse_progress_record,
    merge_train_batch_landing_entry_rank as controller_merge_train_batch_landing_entry_rank,
)
from control_plane.workflows.evidence_ingestion import (
    EvidenceIngestionStore,
    apply_deployment_evidence,
)
from control_plane.workflows.generic_web_preview import (
    GenericWebPreviewDesiredStateRequest,
    GenericWebPreviewInventoryRequest,
    GenericWebPreviewProfileStore,
    discover_generic_web_preview_desired_state,
    execute_generic_web_preview_inventory,
)
from control_plane.workflows.odoo_preview_runtime import (
    ODOO_PREVIEW_REQUIRED_ENV_KEYS,
    OdooPreviewApplyInputsRequest,
    OdooPreviewDokployApplyRequest,
    build_odoo_preview_apply_inputs,
    execute_odoo_preview_dokploy_apply,
)
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
    ProductProfileListStore,
    create_github_issue_comment,
    find_github_issue_comment_by_marker,
    _github_comment_url,
    launchplane_anchor_repo_context,
    resolve_launchplane_github_token,
    update_github_issue_comment,
    verify_github_webhook_signature,
)
from control_plane.workflows.odoo_artifact_publish import (
    OdooArtifactPublishEvidenceStore,
    OdooArtifactPublishEvidenceRequest,
    OdooArtifactPublishInputsDependencyNotFoundError,
    OdooArtifactPublishInputsRequest,
    build_odoo_artifact_publish_inputs,
    ingest_odoo_artifact_publish_evidence,
)
from control_plane.workflows.odoo_post_deploy import (
    OdooPostDeployRequest,
    execute_odoo_post_deploy,
)
from control_plane.contracts.odoo_stable_bootstrap import (
    OdooStableBootstrapRequest,
)
from control_plane.workflows.odoo_prod_backup_gate import (
    OdooProdBackupGateRequest,
    OdooProdBackupGateStore,
    execute_odoo_prod_backup_gate,
)
from control_plane.workflows.odoo_prod_promotion import (
    OdooProdPromotionRequest,
    OdooProdPromotionStore,
    execute_odoo_prod_promotion,
)
from control_plane.workflows.odoo_prod_promotion_inputs import (
    OdooProdPromotionInputsRequest,
    OdooProdPromotionInputsStore,
    resolve_odoo_prod_promotion_inputs,
)
from control_plane.workflows.odoo_prod_promotion_run import (
    OdooProdPromotionRunRequest,
    OdooProdPromotionRunStore,
    execute_odoo_prod_promotion_run,
)
from control_plane.workflows.odoo_prod_rollback import (
    OdooProdRollbackRequest,
    execute_odoo_prod_rollback,
)
from control_plane.workflows.odoo_stable_target_replacement import (
    OdooStableTargetReplacementStore,
    build_odoo_stable_target_replacement_plan,
)
from control_plane.workflows.verireel_stable_deploy import (
    VeriReelStableDeployRequest,
    VeriReelStableDeployStore,
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
    VeriReelProdBackupGateOperationStore,
    enqueue_verireel_prod_backup_gate,
)
from control_plane.workflows.verireel_prod_promotion import (
    VeriReelProdPromotionRequest,
    VeriReelProdPromotionStore,
    execute_verireel_prod_promotion,
)
from control_plane.workflows.verireel_prod_rollback import (
    VeriReelProdRollbackRequest,
    VeriReelProdRollbackStore,
    execute_verireel_prod_rollback,
)
from control_plane.workflows.verireel_preview_driver import (
    VeriReelPreviewDestroyRequest,
    VeriReelPreviewDestroyResult,
    VeriReelPreviewRefreshConfigError,
    VeriReelPreviewInventoryRequest,
    VeriReelPreviewRefreshRequest,
    VeriReelPreviewRefreshResult,
    VeriReelPreviewRefreshTransportError,
    execute_verireel_preview_destroy,
    execute_verireel_preview_inventory,
    execute_verireel_preview_refresh,
)


_LAUNCHPLANE_SERVICE_CONTEXT = "launchplane"
_WHOLE_PRODUCT_CONTEXT = "*"
_MERGE_TRAIN_BATCH_CANDIDATE_RUN_ONCE_ROUTE = "/v1/work-graph/merge-train/batch-candidate/run-once"
_MERGE_TRAIN_BATCH_LANDING_RUN_ONCE_ROUTE = "/v1/work-graph/merge-train/batch-landing/run-once"
_MERGE_TRAIN_STACK_COLLAPSE_RUN_ONCE_ROUTE = "/v1/work-graph/merge-train/stack-collapse/run-once"
_MERGE_TRAIN_CONTROLLER_RUN_ONCE_ROUTE = "/v1/work-graph/merge-train/controller/run-once"
_MERGE_TRAIN_PR_FEEDBACK_ROUTE = "/v1/work-graph/merge-train/pr-feedback"
_MERGE_TRAIN_RUN_ONCE_ROUTE = "/v1/work-graph/merge-train/run-once"
_EVERY_CODE_GITHUB_WEBHOOK_SECRET_ENV_KEY = "LAUNCHPLANE_EVERY_CODE_GITHUB_WEBHOOK_SECRET"
_NATIVE_FASTAPI_DRIVER_ROUTE_PATHS = frozenset({"/v1/drivers/ingress/route-apply"})


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


class MergeTrainBatchCandidateRunOnceEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    repository: str
    base_branch: str = "main"
    mode: Literal["plan", "build", "observe"] = "plan"
    candidate_record_id: str = ""
    github_api_base_url: str = "https://api.github.com"

    @model_validator(mode="after")
    def _validate_envelope(self) -> "MergeTrainBatchCandidateRunOnceEnvelope":
        self.repository = self.repository.strip()
        self.base_branch = self.base_branch.strip()
        self.candidate_record_id = self.candidate_record_id.strip()
        self.github_api_base_url = self.github_api_base_url.strip() or "https://api.github.com"
        if not self.repository:
            raise ValueError("merge train batch candidate requires repository")
        if "/" not in self.repository:
            raise ValueError("merge train repository must be owner/name")
        if not self.base_branch:
            raise ValueError("merge train batch candidate requires base_branch")
        if self.mode in {"build", "observe"} and not self.candidate_record_id:
            raise ValueError("build and observe require candidate_record_id")
        return self


class MergeTrainBatchLandingRunOnceEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    repository: str
    base_branch: str = "main"
    mode: Literal["plan", "land"] = "plan"
    candidate_record_id: str = ""
    landing_plan_record_id: str = ""
    stack_collapse_plan_record_id: str = ""
    github_api_base_url: str = "https://api.github.com"

    @model_validator(mode="after")
    def _validate_envelope(self) -> "MergeTrainBatchLandingRunOnceEnvelope":
        self.repository = self.repository.strip()
        self.base_branch = self.base_branch.strip()
        self.candidate_record_id = self.candidate_record_id.strip()
        self.landing_plan_record_id = self.landing_plan_record_id.strip()
        self.stack_collapse_plan_record_id = self.stack_collapse_plan_record_id.strip()
        self.github_api_base_url = self.github_api_base_url.strip() or "https://api.github.com"
        if not self.repository:
            raise ValueError("merge train batch landing requires repository")
        if "/" not in self.repository:
            raise ValueError("merge train repository must be owner/name")
        if not self.base_branch:
            raise ValueError("merge train batch landing requires base_branch")
        if self.mode == "plan" and not self.candidate_record_id:
            raise ValueError("landing plan mode requires candidate_record_id")
        if self.mode == "land" and not self.landing_plan_record_id:
            raise ValueError("landing land mode requires landing_plan_record_id")
        return self


class MergeTrainStackCollapseRunOnceEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    repository: str
    base_branch: str = "main"
    mode: Literal["execute", "admit"] = "execute"
    stack_collapse_plan_record_id: str
    github_api_base_url: str = "https://api.github.com"

    @model_validator(mode="after")
    def _validate_envelope(self) -> "MergeTrainStackCollapseRunOnceEnvelope":
        self.repository = self.repository.strip()
        self.base_branch = self.base_branch.strip()
        self.stack_collapse_plan_record_id = self.stack_collapse_plan_record_id.strip()
        self.github_api_base_url = self.github_api_base_url.strip() or "https://api.github.com"
        if not self.repository:
            raise ValueError("merge train stack collapse requires repository")
        if "/" not in self.repository:
            raise ValueError("merge train repository must be owner/name")
        if not self.base_branch:
            raise ValueError("merge train stack collapse requires base_branch")
        if not self.stack_collapse_plan_record_id:
            raise ValueError("merge train stack collapse requires stack_collapse_plan_record_id")
        return self


class MergeTrainControllerRunOnceEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    repository: str
    base_branch: str = "main"
    mutate: bool = False
    github_api_base_url: str = "https://api.github.com"

    @model_validator(mode="after")
    def _validate_envelope(self) -> "MergeTrainControllerRunOnceEnvelope":
        self.repository = self.repository.strip()
        self.base_branch = self.base_branch.strip()
        self.github_api_base_url = self.github_api_base_url.strip() or "https://api.github.com"
        if not self.repository:
            raise ValueError("merge train controller requires repository")
        if "/" not in self.repository:
            raise ValueError("merge train repository must be owner/name")
        if not self.base_branch:
            raise ValueError("merge train controller requires base_branch")
        return self


class MergeTrainPrFeedbackEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    repository: str
    base_branch: str = "main"
    pull_request_number: int = Field(gt=0)
    event: MergeTrainPrFeedbackEvent
    source: str = ""
    controller_action: str = ""
    controller_record_id: str = ""
    message: str = ""

    @model_validator(mode="after")
    def _validate_envelope(self) -> "MergeTrainPrFeedbackEnvelope":
        self.repository = self.repository.strip()
        self.base_branch = self.base_branch.strip()
        self.source = self.source.strip()
        self.controller_action = self.controller_action.strip()
        self.controller_record_id = self.controller_record_id.strip()
        self.message = self.message.strip()
        if not self.repository:
            raise ValueError("merge train PR feedback requires repository")
        if "/" not in self.repository:
            raise ValueError("merge train repository must be owner/name")
        if not self.base_branch:
            raise ValueError("merge train PR feedback requires base_branch")
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


class _IdempotencyCapableStore(Protocol):
    def read_idempotency_record(
        self,
        *,
        scope: str,
        route_path: str,
        idempotency_key: str,
    ) -> LaunchplaneIdempotencyRecord: ...

    def write_idempotency_record(self, record: LaunchplaneIdempotencyRecord) -> object: ...


class _TestLaunchplaneServiceRecordStore(Protocol):
    @property
    def backend_name(self) -> str: ...

    def close(self) -> None: ...


class _OdooStableBootstrapOperationStore(Protocol):
    def write_odoo_stable_bootstrap_operation_record(
        self, record: OdooStableBootstrapOperationRecord
    ) -> object: ...

    def create_odoo_stable_bootstrap_operation_record_if_no_active_lane(
        self, record: OdooStableBootstrapOperationRecord
    ) -> tuple[OdooStableBootstrapOperationRecord, bool]: ...

    def read_odoo_stable_bootstrap_operation_record(
        self, operation_id: str
    ) -> OdooStableBootstrapOperationRecord: ...

    def list_odoo_stable_bootstrap_operation_records(
        self,
        *,
        product: str = "",
        context_name: str = "",
        instance_name: str = "",
        idempotency_key: str = "",
        statuses: tuple[str, ...] = (),
        limit: int | None = None,
    ) -> tuple[OdooStableBootstrapOperationRecord, ...]: ...


class _OdooStableTargetReplacementOperationStore(Protocol):
    def write_odoo_stable_target_replacement_operation_record(
        self, record: OdooStableTargetReplacementOperationRecord
    ) -> object: ...

    def create_odoo_stable_target_replacement_operation_record_if_no_active_lane(
        self, record: OdooStableTargetReplacementOperationRecord
    ) -> tuple[OdooStableTargetReplacementOperationRecord, bool]: ...

    def read_odoo_stable_target_replacement_operation_record(
        self, operation_id: str
    ) -> OdooStableTargetReplacementOperationRecord: ...

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
    ) -> tuple[OdooStableTargetReplacementOperationRecord, ...]: ...


_StartResponse = Callable[[str, list[tuple[str, str]]], None]
_WsgiApp = Callable[[dict[str, object], _StartResponse], list[bytes]]
_EveryCodeWebhookResponse = tuple[int, dict[str, object]]


_LOGGER = logging.getLogger(__name__)


class OdooPreviewApplyEnvelope(_ProductRouteEnvelope):
    schema_version: int = Field(default=1, ge=1)
    apply: OdooPreviewDokployApplyRequest

    @model_validator(mode="after")
    def _validate_alignment(self) -> "OdooPreviewApplyEnvelope":
        _validate_driver_envelope_product(self.product, label="Odoo preview apply")
        if self.product.strip() != self.apply.dry_run_plan.product.strip():
            raise ValueError("Odoo preview apply requires matching product values.")
        return self


class OdooPreviewApplyInputsEnvelope(_ProductRouteEnvelope):
    schema_version: int = Field(default=1, ge=1)
    inputs: OdooPreviewApplyInputsRequest

    @model_validator(mode="after")
    def _validate_alignment(self) -> "OdooPreviewApplyInputsEnvelope":
        _validate_driver_envelope_product(self.product, label="Odoo preview apply inputs")
        if self.product.strip() != self.inputs.product.strip():
            raise ValueError("Odoo preview apply inputs require matching product values.")
        return self


class OdooPreviewApplyConfigError(click.ClickException):
    def __init__(self, *, context: str, instance: str, missing_keys: tuple[str, ...]) -> None:
        super().__init__("Odoo preview apply runtime environment is incomplete.")
        self.context = context
        self.instance = instance
        self.missing_keys = tuple(sorted(missing_keys))


class MergeTrainControllerRequestError(ValueError):
    pass


def _odoo_preview_service_environment_values(
    *,
    control_plane_root_path: Path,
    profile: LaunchplaneProductProfileRecord,
    apply_request: OdooPreviewDokployApplyRequest,
    database_url: str | None,
) -> dict[str, str]:
    plan = apply_request.dry_run_plan
    if plan.operation == "destroy":
        return {}
    preview_profile = profile.preview
    template_instance = preview_profile.template_instance.strip()
    environment_values = control_plane_runtime_environments.resolve_runtime_environment_values(
        control_plane_root=control_plane_root_path,
        context_name=preview_profile.context,
        instance_name=template_instance,
        database_url=database_url,
    )
    environment_values.update(preview_profile.override_env)
    environment_values["ODOO_PROJECT_NAME"] = plan.compose_name
    environment_values["ODOO_STACK_NAME"] = plan.compose_name
    environment_values["ODOO_DB_NAME"] = _odoo_preview_identifier(plan.compose_name, suffix="db")
    environment_values["ODOO_DATA_VOLUME"] = _odoo_preview_identifier(
        plan.compose_name, suffix="data"
    )
    environment_values["ODOO_LOG_VOLUME"] = _odoo_preview_identifier(
        plan.compose_name, suffix="logs"
    )
    environment_values["ODOO_DB_VOLUME"] = _odoo_preview_identifier(
        plan.compose_name, suffix="db-volume"
    )
    for key in preview_profile.preview_url_env_keys:
        environment_values[key] = plan.preview_url
    for key in preview_profile.preview_domain_env_keys:
        environment_values[key] = plan.domain_host
    missing_env_keys = tuple(
        key for key in ODOO_PREVIEW_REQUIRED_ENV_KEYS if not environment_values.get(key, "").strip()
    )
    if missing_env_keys:
        raise OdooPreviewApplyConfigError(
            context=preview_profile.context,
            instance=template_instance,
            missing_keys=missing_env_keys,
        )
    return environment_values


def _odoo_preview_identifier(value: str, *, suffix: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9]+", "_", value.strip()).strip("_").lower()
    if not normalized:
        normalized = "odoo_preview"
    suffix_identifier = re.sub(r"[^a-zA-Z0-9]+", "_", suffix.strip()).strip("_").lower()
    return f"{normalized}_{suffix_identifier}" if suffix_identifier else normalized


def _odoo_preview_apply_inputs_response_result(
    *,
    control_plane_root: Path,
    record_store: object,
    profile: LaunchplaneProductProfileRecord,
    request: OdooPreviewApplyInputsRequest,
    database_url: str | None,
) -> dict[str, object]:
    driver_result = build_odoo_preview_apply_inputs(
        control_plane_root=control_plane_root,
        record_store=cast(Any, record_store),
        profile=profile,
        request=request,
        database_url=database_url,
    )
    return driver_result.model_dump(mode="json")


_ODOO_PREVIEW_APPLY_ROUTE = _DriverRouteExecutionMetadata(
    route_path="/v1/drivers/odoo/preview-apply",
    envelope_model=OdooPreviewApplyEnvelope,
    denial_message="Workflow cannot apply Odoo preview provider state for the requested product/context.",
)


_ODOO_PREVIEW_APPLY_INPUTS_ROUTE = _DriverRouteExecutionMetadata(
    route_path="/v1/drivers/odoo/preview-apply-inputs",
    envelope_model=OdooPreviewApplyInputsEnvelope,
    denial_message="Workflow cannot read Odoo preview apply inputs for the requested product/context.",
)


_PREVIEW_DESIRED_STATE_ROUTE_PATHS = frozenset(
    {_GENERIC_WEB_PREVIEW_DESIRED_STATE_ROUTE.route_path}
)
_PREVIEW_INVENTORY_ROUTE_PATHS = frozenset({_GENERIC_WEB_PREVIEW_INVENTORY_ROUTE.route_path})
_PREVIEW_REFRESH_ROUTE_PATHS = frozenset({_GENERIC_WEB_PREVIEW_REFRESH_ROUTE.route_path})
_PREVIEW_READINESS_ROUTE_PATHS = frozenset({_GENERIC_WEB_PREVIEW_READINESS_ROUTE.route_path})
_PREVIEW_DESTROY_ROUTE_PATHS = frozenset({_GENERIC_WEB_PREVIEW_DESTROY_ROUTE.route_path})
_PREVIEW_DESTROY_IDEMPOTENCY_ROUTE_PATHS = frozenset(
    {
        _GENERIC_WEB_PREVIEW_DESTROY_ROUTE.route_path,
        "/v1/drivers/verireel/preview-destroy",
    }
)
_GENERIC_WEB_BASE_DRIVER_SHARED_ROUTE_PATHS = frozenset(
    {
        _GENERIC_WEB_DEPLOY_ROUTE.route_path,
        _GENERIC_WEB_SOURCE_REF_DEPLOY_ROUTE.route_path,
        _GENERIC_WEB_PROD_PROMOTION_ROUTE.route_path,
        _GENERIC_WEB_PROD_PROMOTION_WORKFLOW_ROUTE.route_path,
        _GENERIC_WEB_ROLLBACK_PLAN_ROUTE.route_path,
        _GENERIC_WEB_ROLLBACK_ROUTE.route_path,
        _GENERIC_WEB_STABLE_VERIFICATION_ROUTE.route_path,
    }
)
_GENERIC_WEB_BASE_DRIVER_PREVIEW_ROUTE_PATHS = frozenset(
    _PREVIEW_DESIRED_STATE_ROUTE_PATHS
    | _PREVIEW_INVENTORY_ROUTE_PATHS
    | _PREVIEW_REFRESH_ROUTE_PATHS
    | _PREVIEW_READINESS_ROUTE_PATHS
    | _PREVIEW_DESTROY_ROUTE_PATHS
)
_GENERIC_WEB_BASE_DRIVER_ROUTE_PATHS = frozenset(
    _GENERIC_WEB_BASE_DRIVER_SHARED_ROUTE_PATHS | _GENERIC_WEB_BASE_DRIVER_PREVIEW_ROUTE_PATHS
)


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


class PreviewLifecycleSweepEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    source: str = "launchplane-preview-lifecycle"
    product: str = ""
    apply: bool = False
    destroy_reason: str = "launchplane_preview_lifecycle_cleanup"
    timeout_seconds: int = Field(default=300, ge=1)
    max_pages: int = Field(default=10, ge=1, le=20)

    @model_validator(mode="after")
    def _validate_request(self) -> "PreviewLifecycleSweepEnvelope":
        if not self.source.strip():
            raise ValueError("preview lifecycle sweep requires source")
        if not self.destroy_reason.strip():
            raise ValueError("preview lifecycle sweep requires destroy_reason")
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
    dry_run: bool = False

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


class OdooPostDeployEnvelope(_ProductRouteEnvelope):
    schema_version: int = Field(default=1, ge=1)
    post_deploy: OdooPostDeployRequest

    @model_validator(mode="after")
    def _validate_alignment(self) -> "OdooPostDeployEnvelope":
        _validate_driver_envelope_product(self.product, label="Odoo post-deploy")
        return self


class OdooConfigParameterOverrideRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    product: str
    context: str
    instance: str
    key: str
    value: str
    source_label: str = "launchplane-service"

    @model_validator(mode="after")
    def _validate_request(self) -> "OdooConfigParameterOverrideRequest":
        self.product = self.product.strip()
        self.context = self.context.strip().lower()
        self.instance = self.instance.strip().lower()
        self.key = self.key.strip().lower()
        self.source_label = self.source_label.strip() or "launchplane-service"
        if not self.product:
            raise ValueError("Odoo config-parameter override requires product.")
        if not self.context:
            raise ValueError("Odoo config-parameter override requires context.")
        if not self.instance:
            raise ValueError("Odoo config-parameter override requires instance.")
        if not self.key:
            raise ValueError("Odoo config-parameter override requires key.")
        if self.key != "web.base.url":
            raise ValueError("Only web.base.url overrides are currently service-writable.")
        if not self.value.strip():
            raise ValueError("Odoo config-parameter override requires value.")
        return self


class OdooWebsiteBootstrapOverrideRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    product: str
    context: str
    instance: str
    website_bootstrap: OdooWebsiteBootstrapPayload
    source_label: str = "launchplane-service"

    @model_validator(mode="after")
    def _validate_request(self) -> "OdooWebsiteBootstrapOverrideRequest":
        self.product = self.product.strip()
        self.context = self.context.strip().lower()
        self.instance = self.instance.strip().lower()
        self.source_label = self.source_label.strip() or "launchplane-service"
        if not self.product:
            raise ValueError("Odoo website-bootstrap override requires product.")
        if not self.context:
            raise ValueError("Odoo website-bootstrap override requires context.")
        if not self.instance:
            raise ValueError("Odoo website-bootstrap override requires instance.")
        return self


class OdooConfigParameterOverrideEnvelope(_ProductRouteEnvelope):
    schema_version: int = Field(default=1, ge=1)
    override: OdooConfigParameterOverrideRequest

    @model_validator(mode="after")
    def _validate_alignment(self) -> "OdooConfigParameterOverrideEnvelope":
        _validate_driver_envelope_product(self.product, label="Odoo config-parameter override")
        if self.product.strip() != self.override.product.strip():
            raise ValueError("Odoo config-parameter override requires matching product values.")
        return self


class OdooWebsiteBootstrapOverrideEnvelope(_ProductRouteEnvelope):
    schema_version: int = Field(default=1, ge=1)
    override: OdooWebsiteBootstrapOverrideRequest

    @model_validator(mode="after")
    def _validate_alignment(self) -> "OdooWebsiteBootstrapOverrideEnvelope":
        _validate_driver_envelope_product(self.product, label="Odoo website-bootstrap override")
        if self.product.strip() != self.override.product.strip():
            raise ValueError("Odoo website-bootstrap override requires matching product values.")
        return self


class OdooStableBootstrapEnvelope(_ProductRouteEnvelope):
    schema_version: int = Field(default=1, ge=1)
    bootstrap: OdooStableBootstrapRequest

    @model_validator(mode="after")
    def _validate_alignment(self) -> "OdooStableBootstrapEnvelope":
        _validate_driver_envelope_product(self.product, label="Odoo stable bootstrap")
        if self.product.strip() != self.bootstrap.product.strip():
            raise ValueError("Odoo stable bootstrap requires matching product values.")
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


_ODOO_CONFIG_PARAMETER_OVERRIDE_ROUTE = _DriverRouteExecutionMetadata(
    route_path="/v1/drivers/odoo/config-parameter-override",
    envelope_model=OdooConfigParameterOverrideEnvelope,
    denial_message=(
        "Workflow cannot write Odoo config-parameter overrides for the requested product/context."
    ),
)


_ODOO_WEBSITE_BOOTSTRAP_OVERRIDE_ROUTE = _DriverRouteExecutionMetadata(
    route_path="/v1/drivers/odoo/website-bootstrap-override",
    envelope_model=OdooWebsiteBootstrapOverrideEnvelope,
    denial_message=(
        "Workflow cannot write Odoo website-bootstrap overrides for the requested product/context."
    ),
)


_ODOO_STABLE_BOOTSTRAP_ROUTE = _DriverRouteExecutionMetadata(
    route_path="/v1/drivers/odoo/stable-bootstrap",
    envelope_model=OdooStableBootstrapEnvelope,
    denial_message=(
        "Workflow cannot execute Odoo stable bootstrap for the requested product/context."
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


class OdooProdPromotionInputsEnvelope(_ProductRouteEnvelope):
    schema_version: int = Field(default=1, ge=1)
    inputs: OdooProdPromotionInputsRequest

    @model_validator(mode="after")
    def _validate_alignment(self) -> "OdooProdPromotionInputsEnvelope":
        _validate_driver_envelope_product(self.product, label="Odoo prod promotion inputs")
        return self


class OdooProdPromotionRunEnvelope(_ProductRouteEnvelope):
    schema_version: int = Field(default=1, ge=1)
    run: OdooProdPromotionRunRequest

    @model_validator(mode="after")
    def _validate_alignment(self) -> "OdooProdPromotionRunEnvelope":
        _validate_driver_envelope_product(self.product, label="Odoo prod promotion run")
        return self


_ODOO_PROD_BACKUP_GATE_ROUTE = _DriverRouteExecutionMetadata(
    route_path="/v1/drivers/odoo/prod-backup-gate",
    envelope_model=OdooProdBackupGateEnvelope,
    denial_message=(
        "Workflow cannot execute the Odoo prod backup-gate driver"
        " for the requested product/context."
    ),
)


_ODOO_PROD_PROMOTION_INPUTS_ROUTE = _DriverRouteExecutionMetadata(
    route_path="/v1/drivers/odoo/prod-promotion-inputs",
    envelope_model=OdooProdPromotionInputsEnvelope,
    denial_message=(
        "Workflow cannot read Odoo prod promotion inputs for the requested product/context."
    ),
)


_ODOO_PROD_PROMOTION_RUN_ROUTE = _DriverRouteExecutionMetadata(
    route_path="/v1/drivers/odoo/prod-promotion-run",
    envelope_model=OdooProdPromotionRunEnvelope,
    denial_message=(
        "Workflow cannot execute the Odoo prod promotion run for the requested product/context."
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


class OdooTargetReplacementPlanEnvelope(_ProductRouteEnvelope):
    schema_version: int = Field(default=1, ge=1)
    replacement: OdooStableTargetReplacementRequest

    @model_validator(mode="after")
    def _validate_alignment(self) -> "OdooTargetReplacementPlanEnvelope":
        _validate_driver_envelope_product(self.product, label="Odoo target replacement plan")
        if self.product.strip() != self.replacement.product.strip():
            raise ValueError("Odoo target replacement plan requires matching product values.")
        return self


_ODOO_TARGET_REPLACEMENT_PLAN_ROUTE = _DriverRouteExecutionMetadata(
    route_path="/v1/drivers/odoo/target-replacement-plan",
    envelope_model=OdooTargetReplacementPlanEnvelope,
    denial_message=(
        "Workflow cannot read the Odoo target replacement plan for the requested product/context."
    ),
)


class OdooTargetReplacementApplyEnvelope(_ProductRouteEnvelope):
    schema_version: int = Field(default=1, ge=1)
    replacement: OdooStableTargetReplacementApplyRequest

    @model_validator(mode="after")
    def _validate_alignment(self) -> "OdooTargetReplacementApplyEnvelope":
        _validate_driver_envelope_product(self.product, label="Odoo target replacement apply")
        if self.product.strip() != self.replacement.product.strip():
            raise ValueError("Odoo target replacement apply requires matching product values.")
        return self


_ODOO_TARGET_REPLACEMENT_APPLY_ROUTE = _DriverRouteExecutionMetadata(
    route_path="/v1/drivers/odoo/target-replacement-apply",
    envelope_model=OdooTargetReplacementApplyEnvelope,
    denial_message=(
        "Workflow cannot apply Odoo target replacement for the requested product/context."
    ),
)


_PREVIEW_VERIFICATION_ROUTE_PATHS = frozenset({_GENERIC_WEB_PREVIEW_VERIFICATION_ROUTE.route_path})
_GENERIC_WEB_BASE_DRIVER_PREVIEW_ROUTE_PATHS = frozenset(
    _GENERIC_WEB_BASE_DRIVER_PREVIEW_ROUTE_PATHS
    | _PREVIEW_VERIFICATION_ROUTE_PATHS
    | {_ODOO_PREVIEW_APPLY_INPUTS_ROUTE.route_path}
)
_GENERIC_WEB_BASE_DRIVER_ROUTE_PATHS = frozenset(
    _GENERIC_WEB_BASE_DRIVER_SHARED_ROUTE_PATHS
    | _GENERIC_WEB_BASE_DRIVER_PREVIEW_ROUTE_PATHS
    | {_ODOO_ARTIFACT_PUBLISH_INPUTS_ROUTE.route_path}
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
        _GENERIC_WEB_PROD_PROMOTION_ROUTE.route_path,
        _GENERIC_WEB_PROD_PROMOTION_WORKFLOW_ROUTE.route_path,
    }
)
_NON_IDEMPOTENT_DRIVER_RESULT_ROUTES = frozenset(
    {
        _GENERIC_WEB_PREVIEW_INVENTORY_ROUTE.route_path,
        _GENERIC_WEB_PREVIEW_READINESS_ROUTE.route_path,
        _ODOO_STABLE_BOOTSTRAP_ROUTE.route_path,
        _ODOO_PREVIEW_APPLY_INPUTS_ROUTE.route_path,
        _ODOO_TARGET_REPLACEMENT_PLAN_ROUTE.route_path,
        _ODOO_TARGET_REPLACEMENT_APPLY_ROUTE.route_path,
        _VERIREEL_STABLE_ENVIRONMENT_ROUTE.route_path,
        _VERIREEL_RUNTIME_VERIFICATION_ROUTE.route_path,
        _VERIREEL_PREVIEW_INVENTORY_ROUTE.route_path,
    }
)
_PENDING_RESULT_IDEMPOTENCY_SKIP_ROUTES = frozenset({_VERIREEL_PROD_BACKUP_GATE_ROUTE.route_path})


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
    action: str | None = None,
) -> list[bytes] | None:
    """Authorize descriptor routes against normalized product/context values."""

    normalized_product = product.strip()
    normalized_context = context.strip()
    if authz_policy.allows(
        identity=identity,
        action=action or _descriptor_driver_authz_action(route_path),
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


def _handle_odoo_artifact_publish(
    request: OdooArtifactPublishEnvelope,
    resolved_context: _ResolvedProductDriverContext,
    record_store: object,
    control_plane_root_path: Path,
) -> _DescriptorDriverDispatchResult:
    del resolved_context, control_plane_root_path
    driver_result = ingest_odoo_artifact_publish_evidence(
        record_store=cast(OdooArtifactPublishEvidenceStore, record_store),
        request=request.publish,
    )
    return _DescriptorDriverDispatchResult(
        result={
            "artifact_id": driver_result.artifact_id,
            "publish_status": driver_result.status,
            "image_repository": driver_result.image_repository,
            "image_digest": driver_result.image_digest,
            "source_commit": driver_result.source_commit,
        },
        driver_result=driver_result,
    )


def _handle_odoo_artifact_publish_inputs(
    request: OdooArtifactPublishInputsEnvelope,
    resolved_context: _ResolvedProductDriverContext,
    record_store: object,
    control_plane_root_path: Path,
) -> _DescriptorDriverDispatchResult:
    del record_store
    try:
        driver_result = build_odoo_artifact_publish_inputs(
            control_plane_root=control_plane_root_path,
            request=request.inputs,
            product_profile=resolved_context.profile,
        )
    except OdooArtifactPublishInputsDependencyNotFoundError as error:
        raise DriverRouteDependencyNotFoundError from error
    return _DescriptorDriverDispatchResult(result=driver_result, driver_result=driver_result)


def _handle_odoo_prod_promotion_inputs(
    request: OdooProdPromotionInputsEnvelope,
    resolved_context: _ResolvedProductDriverContext,
    record_store: object,
    control_plane_root_path: Path,
) -> _DescriptorDriverDispatchResult:
    del resolved_context, control_plane_root_path
    driver_result = resolve_odoo_prod_promotion_inputs(
        record_store=cast(OdooProdPromotionInputsStore, record_store),
        request=request.inputs,
    )
    return _DescriptorDriverDispatchResult(
        result={
            "artifact_id": driver_result.artifact_id,
            "backup_record_id": driver_result.backup_record_id,
            "release_tuple_id": driver_result.release_tuple_id,
            "source_git_ref": driver_result.source_git_ref,
            "image_repository": driver_result.image_repository,
            "image_digest": driver_result.image_digest,
            "input_status": driver_result.input_status,
        },
        driver_result=driver_result,
    )


def _handle_odoo_prod_backup_gate(
    request: OdooProdBackupGateEnvelope,
    resolved_context: _ResolvedProductDriverContext,
    record_store: object,
    control_plane_root_path: Path,
) -> _DescriptorDriverDispatchResult:
    del resolved_context
    driver_result = execute_odoo_prod_backup_gate(
        control_plane_root=control_plane_root_path,
        record_store=cast(OdooProdBackupGateStore, record_store),
        request=request.backup_gate,
    )
    return _DescriptorDriverDispatchResult(
        result={
            "backup_record_id": driver_result.backup_record_id,
            "backup_status": driver_result.backup_status,
            "backup_root": driver_result.backup_root,
            "database_dump_path": driver_result.database_dump_path,
            "filestore_archive_path": driver_result.filestore_archive_path,
            "manifest_path": driver_result.manifest_path,
        },
        driver_result=driver_result,
    )


def _dispatch_odoo_prod_promotion_run(
    request: OdooProdPromotionRunEnvelope,
    resolved_context: _ResolvedProductDriverContext,
    record_store: object,
    control_plane_root_path: Path,
    state_dir: Path,
    database_url: str | None,
    identity: LaunchplaneIdentity,
    request_scope: str,
    request_idempotency_key: str,
    request_fingerprint: str,
    start_response: _StartResponse,
    trace_id: str,
) -> tuple[dict[str, object], BaseModel | dict[str, object] | None] | list[bytes]:
    run_request = request.run.model_copy(update={"product": request.product})
    del (
        resolved_context,
        identity,
        request_scope,
        request_idempotency_key,
        request_fingerprint,
        start_response,
        trace_id,
    )
    driver_result = execute_odoo_prod_promotion_run(
        control_plane_root=control_plane_root_path,
        state_dir=state_dir,
        database_url=database_url,
        record_store=cast(OdooProdPromotionRunStore, record_store),
        request=run_request,
    )
    return driver_result.model_dump(mode="json"), driver_result


def _dispatch_odoo_prod_promotion(
    request: OdooProdPromotionEnvelope,
    resolved_context: _ResolvedProductDriverContext,
    record_store: object,
    control_plane_root_path: Path,
    state_dir: Path,
    database_url: str | None,
    identity: LaunchplaneIdentity,
    request_scope: str,
    request_idempotency_key: str,
    request_fingerprint: str,
    start_response: _StartResponse,
    trace_id: str,
) -> tuple[dict[str, object], BaseModel | dict[str, object] | None] | list[bytes]:
    del (
        resolved_context,
        identity,
        request_scope,
        request_idempotency_key,
        request_fingerprint,
        start_response,
        trace_id,
    )
    driver_result = execute_odoo_prod_promotion(
        control_plane_root=control_plane_root_path,
        state_dir=state_dir,
        database_url=database_url,
        record_store=cast(OdooProdPromotionStore, record_store),
        request=request.promotion,
    )
    return (
        {
            "promotion_record_id": driver_result.promotion_record_id,
            "deployment_record_id": driver_result.deployment_record_id,
            "backup_record_id": driver_result.backup_record_id,
            "release_tuple_id": driver_result.release_tuple_id,
            "promotion_status": driver_result.promotion_status,
            "deployment_status": driver_result.deployment_status,
            "post_deploy_status": driver_result.post_deploy_status,
            "destination_health_status": driver_result.destination_health_status,
        },
        driver_result,
    )


def _handle_odoo_prod_rollback(
    request: OdooProdRollbackEnvelope,
    resolved_context: _ResolvedProductDriverContext,
    record_store: object,
    control_plane_root_path: Path,
) -> _DescriptorDriverDispatchResult:
    del resolved_context
    driver_result = execute_odoo_prod_rollback(
        control_plane_root=control_plane_root_path,
        record_store=record_store,
        request=request.rollback,
    )
    return _DescriptorDriverDispatchResult(
        result={
            "promotion_record_id": driver_result.promotion_record_id,
            "deployment_record_id": driver_result.deployment_record_id,
            "release_tuple_id": driver_result.release_tuple_id,
            "rollback_status": driver_result.rollback_status,
            "rollback_health_status": driver_result.rollback_health_status,
            "post_deploy_status": driver_result.post_deploy_status,
        },
        driver_result=driver_result,
    )


def _handle_odoo_target_replacement_plan(
    request: OdooTargetReplacementPlanEnvelope,
    resolved_context: _ResolvedProductDriverContext,
    record_store: object,
    control_plane_root_path: Path,
) -> _DescriptorDriverDispatchResult:
    if resolved_context.lane is None:
        raise ProductDriverMismatchError(
            "Odoo target replacement plan requires a known product lane."
        )
    driver_result = build_odoo_stable_target_replacement_plan(
        control_plane_root=control_plane_root_path,
        record_store=cast(OdooStableTargetReplacementStore, record_store),
        request=request.replacement,
    )
    return _DescriptorDriverDispatchResult(result={}, driver_result=driver_result)


def _dispatch_odoo_target_replacement_apply(
    request: OdooTargetReplacementApplyEnvelope,
    resolved_context: _ResolvedProductDriverContext,
    record_store: object,
    control_plane_root_path: Path,
    state_dir: Path,
    database_url: str | None,
    identity: LaunchplaneIdentity,
    request_scope: str,
    request_idempotency_key: str,
    request_fingerprint: str,
    start_response: _StartResponse,
    trace_id: str,
) -> tuple[dict[str, object], BaseModel | dict[str, object] | None] | list[bytes]:
    del state_dir, database_url, identity
    if resolved_context.lane is None:
        raise ProductDriverMismatchError(
            "Odoo target replacement apply requires a known product lane."
        )
    if not request_idempotency_key:
        return _json_response(
            start_response=start_response,
            status_code=400,
            payload={
                "status": "rejected",
                "trace_id": trace_id,
                "error": {
                    "code": "idempotency_key_required",
                    "message": "Odoo target replacement operations require an Idempotency-Key header.",
                },
            },
        )

    replacement_operation_store = _odoo_stable_target_replacement_operation_store(record_store)
    existing_replacement_operation = (
        _find_odoo_stable_target_replacement_operation_by_idempotency_key(
            operation_store=replacement_operation_store,
            idempotency_key=request_idempotency_key,
            idempotency_scope=request_scope,
        )
    )
    result: dict[str, object]
    if existing_replacement_operation is not None:
        if existing_replacement_operation.request_fingerprint != request_fingerprint:
            return _json_response(
                start_response=start_response,
                status_code=409,
                payload={
                    "status": "rejected",
                    "trace_id": trace_id,
                    "error": {
                        "code": "idempotency_key_reused",
                        "message": "Idempotency-Key was already used for a different Odoo target replacement request.",
                    },
                },
            )
        driver_result = _target_replacement_operation_payload(existing_replacement_operation)
        result = {
            "odoo_stable_target_replacement_operation_id": existing_replacement_operation.operation_id,
            **(
                {"deployment_record_id": existing_replacement_operation.deployment_record_id}
                if existing_replacement_operation.deployment_record_id
                else {}
            ),
        }
    else:
        replacement_operation = _build_odoo_stable_target_replacement_operation_record(
            replacement_request=request.replacement,
            context=resolved_context.lane.context,
            idempotency_key=request_idempotency_key,
            idempotency_scope=request_scope,
            request_fingerprint=request_fingerprint,
            created_at=_utc_now_timestamp(),
        )
        replacement_operation, created_replacement_operation = (
            replacement_operation_store.create_odoo_stable_target_replacement_operation_record_if_no_active_lane(
                replacement_operation
            )
        )
        if not created_replacement_operation:
            return _json_response(
                start_response=start_response,
                status_code=409,
                payload={
                    "status": "rejected",
                    "trace_id": trace_id,
                    "error": {
                        "code": "odoo_stable_target_replacement_operation_active",
                        "message": "An Odoo target replacement operation is already active for this product/context/instance.",
                    },
                    "operation": _target_replacement_operation_payload(replacement_operation),
                },
            )
        driver_result = _target_replacement_operation_payload(replacement_operation)
        result = {"odoo_stable_target_replacement_operation_id": replacement_operation.operation_id}
        if replacement_operation.deployment_record_id:
            result["deployment_record_id"] = replacement_operation.deployment_record_id
        if (
            replacement_operation.result is not None
            and replacement_operation.result.release_tuple_id
        ):
            result["release_tuple_id"] = replacement_operation.result.release_tuple_id

    return result, driver_result


def _dispatch_odoo_stable_bootstrap(
    request: OdooStableBootstrapEnvelope,
    resolved_context: _ResolvedProductDriverContext,
    record_store: object,
    control_plane_root_path: Path,
    state_dir: Path,
    database_url: str | None,
    identity: LaunchplaneIdentity,
    request_scope: str,
    request_idempotency_key: str,
    request_fingerprint: str,
    start_response: _StartResponse,
    trace_id: str,
) -> tuple[dict[str, object], BaseModel | dict[str, object] | None] | list[bytes]:
    del resolved_context, state_dir, database_url, identity, request_scope
    if not request_idempotency_key:
        return _json_response(
            start_response=start_response,
            status_code=400,
            payload={
                "status": "rejected",
                "trace_id": trace_id,
                "error": {
                    "code": "idempotency_key_required",
                    "message": "Odoo stable bootstrap operations require an Idempotency-Key header.",
                },
            },
        )

    operation_store = _odoo_stable_bootstrap_operation_store(record_store)
    existing_operation = _find_odoo_stable_bootstrap_operation_by_idempotency_key(
        operation_store=operation_store,
        product=request.product,
        context=request.bootstrap.context,
        instance=request.bootstrap.instance,
        idempotency_key=request_idempotency_key,
    )
    if existing_operation is not None:
        if existing_operation.request_fingerprint != request_fingerprint:
            return _json_response(
                start_response=start_response,
                status_code=409,
                payload={
                    "status": "rejected",
                    "trace_id": trace_id,
                    "error": {
                        "code": "idempotency_key_reused",
                        "message": "Idempotency-Key was already used for a different Odoo stable bootstrap request.",
                    },
                },
            )
        driver_result = _operation_payload(existing_operation)
        result: dict[str, object] = {
            "odoo_stable_bootstrap_operation_id": existing_operation.operation_id,
            **(
                {"deployment_record_id": existing_operation.deployment_record_id}
                if existing_operation.deployment_record_id
                else {}
            ),
        }
        return result, driver_result

    operation = _build_odoo_stable_bootstrap_operation_record(
        bootstrap_request=request.bootstrap,
        idempotency_key=request_idempotency_key,
        request_fingerprint=request_fingerprint,
        created_at=_utc_now_timestamp(),
    )
    operation, created_operation = (
        operation_store.create_odoo_stable_bootstrap_operation_record_if_no_active_lane(operation)
    )
    if not created_operation:
        if operation.idempotency_key == request_idempotency_key:
            if operation.request_fingerprint != request_fingerprint:
                return _json_response(
                    start_response=start_response,
                    status_code=409,
                    payload={
                        "status": "rejected",
                        "trace_id": trace_id,
                        "error": {
                            "code": "idempotency_key_reused",
                            "message": "Idempotency-Key was already used for a different Odoo stable bootstrap request.",
                        },
                    },
                )
            driver_result = _operation_payload(operation)
            result = {
                "odoo_stable_bootstrap_operation_id": operation.operation_id,
                **(
                    {"deployment_record_id": operation.deployment_record_id}
                    if operation.deployment_record_id
                    else {}
                ),
            }
            return result, driver_result
        return _json_response(
            start_response=start_response,
            status_code=409,
            payload={
                "status": "rejected",
                "trace_id": trace_id,
                "error": {
                    "code": "odoo_stable_bootstrap_operation_active",
                    "message": "An Odoo stable bootstrap operation is already active for this product/context/instance.",
                },
                "operation": _operation_payload(operation),
            },
        )

    driver_result = _operation_payload(operation)
    result = {"odoo_stable_bootstrap_operation_id": operation.operation_id}
    return result, driver_result


def _handle_odoo_post_deploy(
    request: OdooPostDeployEnvelope,
    resolved_context: _ResolvedProductDriverContext,
    record_store: object,
    control_plane_root_path: Path,
) -> _DescriptorDriverDispatchResult:
    del resolved_context
    driver_result = execute_odoo_post_deploy(
        control_plane_root=control_plane_root_path,
        record_store=record_store,
        request=request.post_deploy,
    )
    return _DescriptorDriverDispatchResult(
        result={
            "transition": (
                f"odoo-post-deploy:{driver_result.context}:{driver_result.instance}:{driver_result.phase}"
            )
        },
        driver_result=driver_result,
    )


def _handle_odoo_config_parameter_override(
    request: OdooConfigParameterOverrideEnvelope,
    resolved_context: _ResolvedProductDriverContext,
    record_store: object,
    control_plane_root_path: Path,
) -> _DescriptorDriverDispatchResult:
    del resolved_context, control_plane_root_path
    override_record = _write_odoo_config_parameter_override(
        record_store=cast(_OdooInstanceOverrideStore, record_store),
        request=request.override,
    )
    result: dict[str, object] = {
        "context": override_record.context,
        "instance": override_record.instance,
        "config_parameter_keys": sorted(
            override.key for override in override_record.config_parameters
        ),
    }
    return _DescriptorDriverDispatchResult(result=result, driver_result=result)


def _handle_odoo_website_bootstrap_override(
    request: OdooWebsiteBootstrapOverrideEnvelope,
    resolved_context: _ResolvedProductDriverContext,
    record_store: object,
    control_plane_root_path: Path,
) -> _DescriptorDriverDispatchResult:
    del resolved_context, control_plane_root_path
    override_record = _write_odoo_website_bootstrap_override(
        record_store=cast(_OdooInstanceOverrideStore, record_store),
        request=request.override,
    )
    result: dict[str, object] = {
        "context": override_record.context,
        "instance": override_record.instance,
        "website_bootstrap": override_record.website_bootstrap is not None,
    }
    return _DescriptorDriverDispatchResult(result=result, driver_result=result)


def _validate_odoo_preview_apply_inputs_profile(
    request: OdooPreviewApplyInputsEnvelope,
    resolved_context: _ResolvedProductDriverContext,
    record_store: object,
    control_plane_root_path: Path,
) -> None:
    del request, record_store, control_plane_root_path
    if resolved_context.profile is None:
        raise ProductDriverMismatchError("Odoo preview apply inputs require a product profile.")


def _handle_odoo_preview_apply_inputs(
    request: OdooPreviewApplyInputsEnvelope,
    resolved_context: _ResolvedProductDriverContext,
    record_store: object,
    control_plane_root_path: Path,
) -> _DescriptorDriverDispatchResult:
    assert resolved_context.profile is not None
    database_url = getattr(record_store, "database_url", None)
    driver_result = _odoo_preview_apply_inputs_response_result(
        control_plane_root=control_plane_root_path,
        record_store=record_store,
        profile=resolved_context.profile,
        request=request.inputs,
        database_url=database_url,
    )
    return _DescriptorDriverDispatchResult(result=driver_result, driver_result=driver_result)


def _validate_odoo_preview_apply_profile(
    request: OdooPreviewApplyEnvelope,
    resolved_context: _ResolvedProductDriverContext,
    record_store: object,
    control_plane_root_path: Path,
) -> None:
    del record_store, control_plane_root_path
    profile = resolved_context.profile
    if profile is None:
        raise ProductDriverMismatchError("Odoo preview apply requires a product profile.")
    preview_profile = profile.preview
    if not preview_profile.enabled or not preview_profile.context.strip():
        raise ProductDriverMismatchError(
            "Odoo preview apply requires an enabled product preview profile."
        )
    if request.apply.dry_run_plan.repository.strip() != profile.repository.strip():
        raise ValueError("Odoo preview apply repository does not match product profile.")


def _handle_odoo_preview_apply(
    request: OdooPreviewApplyEnvelope,
    resolved_context: _ResolvedProductDriverContext,
    record_store: object,
    control_plane_root_path: Path,
) -> _DescriptorDriverDispatchResult:
    assert resolved_context.profile is not None
    database_url = getattr(record_store, "database_url", None)
    resolved_environment_values = _odoo_preview_service_environment_values(
        control_plane_root_path=control_plane_root_path,
        profile=resolved_context.profile,
        apply_request=request.apply,
        database_url=database_url,
    )
    service_apply_request = request.apply.model_copy(
        update={"environment_values": resolved_environment_values}
    )
    driver_result = execute_odoo_preview_dokploy_apply(
        control_plane_root=control_plane_root_path,
        request=service_apply_request,
        database_url=database_url,
    )
    return _DescriptorDriverDispatchResult(
        result=driver_result.model_dump(mode="json"),
        driver_result=driver_result,
    )


def _handle_verireel_preview_verification(
    request: VeriReelPreviewVerificationEnvelope,
    resolved_context: _ResolvedProductDriverContext,
    record_store: object,
    control_plane_root_path: Path,
) -> _DescriptorDriverDispatchResult:
    del resolved_context
    return _DescriptorDriverDispatchResult(
        result=_apply_verireel_preview_verification_records(
            control_plane_root_path=control_plane_root_path,
            record_store=record_store,
            request=request.verification,
        )
    )


def _handle_verireel_preview_inventory(
    request: VeriReelPreviewInventoryEnvelope,
    resolved_context: _ResolvedProductDriverContext,
    record_store: object,
    control_plane_root_path: Path,
) -> _DescriptorDriverDispatchResult:
    del resolved_context
    driver_result = execute_verireel_preview_inventory(
        control_plane_root=control_plane_root_path,
        request=request.inventory,
    )
    preview_inventory_scan_id = _write_preview_inventory_scan_if_supported(
        record_store=record_store,
        context=driver_result.context,
        source="verireel-preview-inventory",
        preview_slugs=tuple(item.previewSlug for item in driver_result.previews),
    )
    return _DescriptorDriverDispatchResult(
        result={"preview_inventory_scan_id": preview_inventory_scan_id},
        driver_result=driver_result,
    )


def _handle_verireel_preview_destroy(
    request: VeriReelPreviewDestroyEnvelope,
    resolved_context: _ResolvedProductDriverContext,
    record_store: object,
    control_plane_root_path: Path,
) -> _DescriptorDriverDispatchResult:
    del resolved_context
    driver_result = execute_verireel_preview_destroy(
        control_plane_root=control_plane_root_path,
        request=request.destroy,
    )
    return _DescriptorDriverDispatchResult(
        result=_apply_verireel_preview_destroy_records(
            record_store=record_store,
            request=request.destroy,
            driver_result=driver_result,
        ),
        driver_result=driver_result,
    )


def _handle_verireel_preview_refresh(
    request: VeriReelPreviewRefreshEnvelope,
    resolved_context: _ResolvedProductDriverContext,
    record_store: object,
    control_plane_root_path: Path,
) -> _DescriptorDriverDispatchResult:
    del resolved_context
    try:
        driver_result = execute_verireel_preview_refresh(
            control_plane_root=control_plane_root_path,
            record_store=record_store if isinstance(record_store, PostgresRecordStore) else None,
            request=request.refresh,
        )
    except VeriReelPreviewRefreshConfigError as error:
        now = _utc_now_timestamp()
        error_message = (
            str(error).strip() or "VeriReel preview refresh configuration is incomplete."
        )
        driver_result = VeriReelPreviewRefreshResult(
            refresh_status="fail",
            refresh_started_at=now,
            refresh_finished_at=now,
            application_name="",
            application_id="",
            preview_url=_verireel_preview_url_for_failed_records(request=request.refresh),
            error_message=error_message,
        )
    return _DescriptorDriverDispatchResult(
        result=_apply_verireel_preview_refresh_records(
            control_plane_root_path=control_plane_root_path,
            record_store=record_store,
            request=request.refresh,
            driver_result=driver_result,
        ),
        driver_result=driver_result,
    )


def _handle_verireel_testing_verification(
    request: VeriReelTestingVerificationEnvelope,
    resolved_context: _ResolvedProductDriverContext,
    record_store: object,
    control_plane_root_path: Path,
) -> _DescriptorDriverDispatchResult:
    del resolved_context, control_plane_root_path
    return _DescriptorDriverDispatchResult(
        result=dict[str, object](
            _apply_verireel_testing_verification_records(
                record_store=record_store,
                request=request.verification,
            )
        )
    )


def _handle_verireel_testing_deploy(
    request: VeriReelTestingDeployEnvelope,
    resolved_context: _ResolvedProductDriverContext,
    record_store: object,
    control_plane_root_path: Path,
) -> _DescriptorDriverDispatchResult:
    del resolved_context
    driver_result = execute_verireel_stable_deploy(
        control_plane_root=control_plane_root_path,
        record_store=cast(VeriReelStableDeployStore, record_store),
        request=request.deploy,
    )
    return _DescriptorDriverDispatchResult(
        result={"deployment_record_id": driver_result.deployment_record_id},
        driver_result=driver_result,
    )


def _handle_verireel_prod_deploy(
    request: VeriReelProdDeployEnvelope,
    resolved_context: _ResolvedProductDriverContext,
    record_store: object,
    control_plane_root_path: Path,
) -> _DescriptorDriverDispatchResult:
    del resolved_context
    driver_result = execute_verireel_stable_deploy(
        control_plane_root=control_plane_root_path,
        record_store=cast(VeriReelStableDeployStore, record_store),
        request=request.deploy,
    )
    return _DescriptorDriverDispatchResult(
        result={"deployment_record_id": driver_result.deployment_record_id},
        driver_result=driver_result,
    )


def _handle_verireel_prod_backup_gate(
    request: VeriReelProdBackupGateEnvelope,
    resolved_context: _ResolvedProductDriverContext,
    record_store: object,
    control_plane_root_path: Path,
) -> _DescriptorDriverDispatchResult:
    del resolved_context, control_plane_root_path
    driver_result = enqueue_verireel_prod_backup_gate(
        record_store=cast(VeriReelProdBackupGateOperationStore, record_store),
        request=request.backup_gate,
    )
    return _DescriptorDriverDispatchResult(
        result={"backup_gate_record_id": driver_result.backup_record_id},
        driver_result=driver_result,
    )


def _handle_verireel_prod_rollback(
    request: VeriReelProdRollbackEnvelope,
    resolved_context: _ResolvedProductDriverContext,
    record_store: object,
    control_plane_root_path: Path,
) -> _DescriptorDriverDispatchResult:
    del resolved_context
    driver_result = execute_verireel_prod_rollback(
        control_plane_root=control_plane_root_path,
        record_store=cast(VeriReelProdRollbackStore, record_store),
        request=request.rollback,
    )
    return _DescriptorDriverDispatchResult(
        result={
            "promotion_record_id": driver_result.promotion_record_id,
            "backup_record_id": driver_result.backup_record_id,
        },
        driver_result=driver_result,
    )


def _validate_verireel_prod_promotion_target_lane(
    request: VeriReelProdPromotionEnvelope,
    resolved_context: _ResolvedProductDriverContext,
    record_store: object,
    control_plane_root_path: Path,
) -> None:
    del resolved_context, control_plane_root_path
    _resolve_descriptor_product_driver_context(
        record_store=record_store,
        route_path=_VERIREEL_PROD_PROMOTION_ROUTE.route_path,
        product=request.product,
        context=request.promotion.context,
        instance=request.promotion.to_instance,
    )


def _handle_verireel_prod_promotion(
    request: VeriReelProdPromotionEnvelope,
    resolved_context: _ResolvedProductDriverContext,
    record_store: object,
    control_plane_root_path: Path,
) -> _DescriptorDriverDispatchResult:
    del resolved_context
    driver_result = execute_verireel_prod_promotion(
        control_plane_root=control_plane_root_path,
        record_store=cast(VeriReelProdPromotionStore, record_store),
        request=request.promotion,
    )
    return _DescriptorDriverDispatchResult(
        result={
            "promotion_record_id": driver_result.promotion_record_id,
            "deployment_record_id": driver_result.deployment_record_id,
        },
        driver_result=driver_result,
    )


def _handle_verireel_stable_environment(
    request: VeriReelStableEnvironmentEnvelope,
    resolved_context: _ResolvedProductDriverContext,
    record_store: object,
    control_plane_root_path: Path,
) -> _DescriptorDriverDispatchResult:
    del resolved_context, record_store
    driver_result = resolve_verireel_stable_environment(
        control_plane_root=control_plane_root_path,
        request=request.environment,
    )
    return _DescriptorDriverDispatchResult(result={}, driver_result=driver_result)


def _handle_verireel_runtime_verification(
    request: VeriReelRuntimeVerificationEnvelope,
    resolved_context: _ResolvedProductDriverContext,
    record_store: object,
    control_plane_root_path: Path,
) -> _DescriptorDriverDispatchResult:
    del resolved_context, record_store
    driver_result = execute_verireel_rollout_verification(
        control_plane_root=control_plane_root_path,
        request=request.verification,
    )
    return _DescriptorDriverDispatchResult(result={}, driver_result=driver_result)


def _handle_verireel_app_maintenance(
    request: VeriReelAppMaintenanceEnvelope,
    resolved_context: _ResolvedProductDriverContext,
    record_store: object,
    control_plane_root_path: Path,
) -> _DescriptorDriverDispatchResult:
    del resolved_context, record_store
    driver_result = execute_verireel_app_maintenance(
        control_plane_root=control_plane_root_path,
        request=request.maintenance,
    )
    return _DescriptorDriverDispatchResult(
        result=driver_result.model_dump(mode="json"),
        driver_result=driver_result,
    )


def _descriptor_driver_dispatch_routes() -> dict[str, _DescriptorDriverDispatchRoute[Any]]:
    return {
        _GENERIC_WEB_DEPLOY_ROUTE.route_path: _DescriptorDriverDispatchRoute(
            execution_metadata=_GENERIC_WEB_DEPLOY_ROUTE,
            context_resolver=lambda request: _DescriptorDriverDispatchContext(
                product=request.deploy.product,
                context="",
                instance=request.deploy.instance,
                require_profile=True,
            ),
            handler=_handle_generic_web_deploy,
        ),
        _GENERIC_WEB_SOURCE_REF_DEPLOY_ROUTE.route_path: _DescriptorDriverDispatchRoute(
            execution_metadata=_GENERIC_WEB_SOURCE_REF_DEPLOY_ROUTE,
            context_resolver=lambda request: _DescriptorDriverDispatchContext(
                product=request.product,
                context=request.deploy.context,
                instance=request.deploy.instance,
                require_profile=True,
            ),
            pre_idempotency_validator=_validate_generic_web_source_ref_deploy_lane,
            handler=_handle_generic_web_source_ref_deploy,
        ),
        _GENERIC_WEB_PROD_PROMOTION_WORKFLOW_ROUTE.route_path: _DescriptorDriverDispatchRoute(
            execution_metadata=_GENERIC_WEB_PROD_PROMOTION_WORKFLOW_ROUTE,
            context_resolver=lambda request: _DescriptorDriverDispatchContext(
                product=request.product,
                context=request.workflow.context,
                require_profile=True,
            ),
            handler=_handle_generic_web_promotion_workflow,
        ),
        _GENERIC_WEB_PROD_PROMOTION_ROUTE.route_path: _DescriptorDriverDispatchRoute(
            execution_metadata=_GENERIC_WEB_PROD_PROMOTION_ROUTE,
            context_resolver=lambda request: _DescriptorDriverDispatchContext(
                product=request.product,
                context="",
                instance=request.promotion.to_instance,
                require_profile=True,
            ),
            pre_idempotency_validator=_validate_generic_web_prod_promotion_lanes,
            pre_authorization_validator=_reject_human_live_generic_web_prod_promotion,
            handler=_handle_generic_web_prod_promotion,
        ),
        _GENERIC_WEB_ROLLBACK_PLAN_ROUTE.route_path: _DescriptorDriverDispatchRoute(
            execution_metadata=_GENERIC_WEB_ROLLBACK_PLAN_ROUTE,
            context_resolver=lambda request: _DescriptorDriverDispatchContext(
                product=request.product,
                context="",
                instance=request.rollback_plan.instance,
                require_profile=True,
            ),
            handler=_handle_generic_web_rollback_plan,
        ),
        _GENERIC_WEB_ROLLBACK_ROUTE.route_path: _DescriptorDriverDispatchRoute(
            execution_metadata=_GENERIC_WEB_ROLLBACK_ROUTE,
            context_resolver=lambda request: _DescriptorDriverDispatchContext(
                product=request.rollback.product,
                context="",
                instance=request.rollback.instance,
                require_profile=True,
            ),
            handler=_handle_generic_web_rollback,
        ),
        _GENERIC_WEB_STABLE_VERIFICATION_ROUTE.route_path: _DescriptorDriverDispatchRoute(
            execution_metadata=_GENERIC_WEB_STABLE_VERIFICATION_ROUTE,
            context_resolver=lambda request: _DescriptorDriverDispatchContext(
                product=request.product,
                context=request.verification.context,
                instance=request.verification.instance,
            ),
            handler=_handle_generic_web_stable_verification,
        ),
        _GENERIC_WEB_PREVIEW_DESIRED_STATE_ROUTE.route_path: _DescriptorDriverDispatchRoute(
            execution_metadata=_GENERIC_WEB_PREVIEW_DESIRED_STATE_ROUTE,
            context_resolver=lambda request: _DescriptorDriverDispatchContext(
                product=request.product,
                context="",
                use_preview_context_for_authorization=True,
                require_profile=True,
            ),
            handler=_handle_generic_web_preview_desired_state,
            pre_idempotency_validator=_validate_generic_web_preview_profile,
        ),
        _GENERIC_WEB_PREVIEW_INVENTORY_ROUTE.route_path: _DescriptorDriverDispatchRoute(
            execution_metadata=_GENERIC_WEB_PREVIEW_INVENTORY_ROUTE,
            context_resolver=lambda request: _DescriptorDriverDispatchContext(
                product=request.product,
                context="",
                use_preview_context_for_authorization=True,
                require_profile=True,
            ),
            handler=_handle_generic_web_preview_inventory,
            pre_idempotency_validator=_validate_generic_web_preview_profile,
            skip_pre_idempotency_check=True,
        ),
        _GENERIC_WEB_PREVIEW_REFRESH_ROUTE.route_path: _DescriptorDriverDispatchRoute(
            execution_metadata=_GENERIC_WEB_PREVIEW_REFRESH_ROUTE,
            context_resolver=lambda request: _DescriptorDriverDispatchContext(
                product=request.product,
                context="",
                use_preview_context_for_authorization=True,
                require_profile=True,
            ),
            handler=_handle_generic_web_preview_refresh,
            pre_idempotency_validator=_validate_generic_web_preview_profile,
        ),
        _GENERIC_WEB_PREVIEW_READINESS_ROUTE.route_path: _DescriptorDriverDispatchRoute(
            execution_metadata=_GENERIC_WEB_PREVIEW_READINESS_ROUTE,
            context_resolver=lambda request: _DescriptorDriverDispatchContext(
                product=request.product,
                context="",
                use_preview_context_for_authorization=True,
                require_profile=True,
            ),
            handler=_handle_generic_web_preview_readiness,
            pre_idempotency_validator=_validate_generic_web_preview_profile,
            skip_pre_idempotency_check=True,
        ),
        _GENERIC_WEB_PREVIEW_DESTROY_ROUTE.route_path: _DescriptorDriverDispatchRoute(
            execution_metadata=_GENERIC_WEB_PREVIEW_DESTROY_ROUTE,
            context_resolver=lambda request: _DescriptorDriverDispatchContext(
                product=request.product,
                context="",
                use_preview_context_for_authorization=True,
                require_profile=True,
            ),
            handler=_handle_generic_web_preview_destroy,
            pre_idempotency_validator=_validate_generic_web_preview_profile,
        ),
        _GENERIC_WEB_PREVIEW_VERIFICATION_ROUTE.route_path: _DescriptorDriverDispatchRoute(
            execution_metadata=_GENERIC_WEB_PREVIEW_VERIFICATION_ROUTE,
            context_resolver=lambda request: _DescriptorDriverDispatchContext(
                product=request.product,
                context="",
                authorization_context=request.verification.context,
                require_profile=True,
            ),
            handler=_handle_generic_web_preview_verification,
            pre_idempotency_validator=_validate_generic_web_preview_profile,
        ),
        _ODOO_ARTIFACT_PUBLISH_ROUTE.route_path: _DescriptorDriverDispatchRoute(
            execution_metadata=_ODOO_ARTIFACT_PUBLISH_ROUTE,
            context_resolver=lambda request: _DescriptorDriverDispatchContext(
                product=request.product,
                context="",
                authorization_context=request.publish.context,
            ),
            handler=_handle_odoo_artifact_publish,
        ),
        _ODOO_ARTIFACT_PUBLISH_INPUTS_ROUTE.route_path: _DescriptorDriverDispatchRoute(
            execution_metadata=_ODOO_ARTIFACT_PUBLISH_INPUTS_ROUTE,
            context_resolver=lambda request: _DescriptorDriverDispatchContext(
                product=request.product,
                context=request.inputs.context,
                instance=request.inputs.instance,
            ),
            handler=_handle_odoo_artifact_publish_inputs,
        ),
        _ODOO_PROD_PROMOTION_INPUTS_ROUTE.route_path: _DescriptorDriverDispatchRoute(
            execution_metadata=_ODOO_PROD_PROMOTION_INPUTS_ROUTE,
            context_resolver=lambda request: _DescriptorDriverDispatchContext(
                product=request.product,
                context="",
                authorization_context=request.inputs.context,
            ),
            handler=_handle_odoo_prod_promotion_inputs,
        ),
        _ODOO_PROD_BACKUP_GATE_ROUTE.route_path: _DescriptorDriverDispatchRoute(
            execution_metadata=_ODOO_PROD_BACKUP_GATE_ROUTE,
            context_resolver=lambda request: _DescriptorDriverDispatchContext(
                product=request.product,
                context=request.backup_gate.context,
                instance=request.backup_gate.instance,
            ),
            handler=_handle_odoo_prod_backup_gate,
        ),
        _ODOO_PROD_PROMOTION_RUN_ROUTE.route_path: _DescriptorDriverDispatchRoute(
            execution_metadata=_ODOO_PROD_PROMOTION_RUN_ROUTE,
            context_resolver=lambda request: _DescriptorDriverDispatchContext(
                product=request.product,
                context="",
                authorization_context=request.run.context,
            ),
            custom_dispatch_handler=_dispatch_odoo_prod_promotion_run,
        ),
        _ODOO_PROD_PROMOTION_ROUTE.route_path: _DescriptorDriverDispatchRoute(
            execution_metadata=_ODOO_PROD_PROMOTION_ROUTE,
            context_resolver=lambda request: _DescriptorDriverDispatchContext(
                product=request.product,
                context="",
                authorization_context=request.promotion.context,
            ),
            custom_dispatch_handler=_dispatch_odoo_prod_promotion,
        ),
        _ODOO_PROD_ROLLBACK_ROUTE.route_path: _DescriptorDriverDispatchRoute(
            execution_metadata=_ODOO_PROD_ROLLBACK_ROUTE,
            context_resolver=lambda request: _DescriptorDriverDispatchContext(
                product=request.product,
                context="",
                authorization_context=request.rollback.context,
            ),
            handler=_handle_odoo_prod_rollback,
        ),
        _ODOO_TARGET_REPLACEMENT_PLAN_ROUTE.route_path: _DescriptorDriverDispatchRoute(
            execution_metadata=_ODOO_TARGET_REPLACEMENT_PLAN_ROUTE,
            context_resolver=lambda request: _DescriptorDriverDispatchContext(
                product=request.product,
                context="",
                instance=request.replacement.instance,
                require_profile=True,
            ),
            handler=_handle_odoo_target_replacement_plan,
        ),
        _ODOO_TARGET_REPLACEMENT_APPLY_ROUTE.route_path: _DescriptorDriverDispatchRoute(
            execution_metadata=_ODOO_TARGET_REPLACEMENT_APPLY_ROUTE,
            context_resolver=lambda request: _DescriptorDriverDispatchContext(
                product=request.product,
                context="",
                instance=request.replacement.instance,
                require_profile=True,
            ),
            custom_dispatch_handler=_dispatch_odoo_target_replacement_apply,
        ),
        _ODOO_POST_DEPLOY_ROUTE.route_path: _DescriptorDriverDispatchRoute(
            execution_metadata=_ODOO_POST_DEPLOY_ROUTE,
            context_resolver=lambda request: _DescriptorDriverDispatchContext(
                product=request.product,
                context="",
                authorization_context=request.post_deploy.context,
            ),
            handler=_handle_odoo_post_deploy,
        ),
        _ODOO_CONFIG_PARAMETER_OVERRIDE_ROUTE.route_path: _DescriptorDriverDispatchRoute(
            execution_metadata=_ODOO_CONFIG_PARAMETER_OVERRIDE_ROUTE,
            context_resolver=lambda request: _DescriptorDriverDispatchContext(
                product=request.product,
                context=request.override.context,
                instance=request.override.instance,
            ),
            handler=_handle_odoo_config_parameter_override,
        ),
        _ODOO_WEBSITE_BOOTSTRAP_OVERRIDE_ROUTE.route_path: _DescriptorDriverDispatchRoute(
            execution_metadata=_ODOO_WEBSITE_BOOTSTRAP_OVERRIDE_ROUTE,
            context_resolver=lambda request: _DescriptorDriverDispatchContext(
                product=request.product,
                context=request.override.context,
                instance=request.override.instance,
            ),
            handler=_handle_odoo_website_bootstrap_override,
        ),
        _ODOO_STABLE_BOOTSTRAP_ROUTE.route_path: _DescriptorDriverDispatchRoute(
            execution_metadata=_ODOO_STABLE_BOOTSTRAP_ROUTE,
            context_resolver=lambda request: _DescriptorDriverDispatchContext(
                product=request.product,
                context=request.bootstrap.context,
                instance=request.bootstrap.instance,
                authorization_context=request.bootstrap.context,
                use_resolved_profile_product_for_authorization=False,
            ),
            custom_dispatch_handler=_dispatch_odoo_stable_bootstrap,
        ),
        _ODOO_PREVIEW_APPLY_INPUTS_ROUTE.route_path: _DescriptorDriverDispatchRoute(
            execution_metadata=_ODOO_PREVIEW_APPLY_INPUTS_ROUTE,
            context_resolver=lambda request: _DescriptorDriverDispatchContext(
                product=request.product,
                context="",
                use_preview_context_for_authorization=True,
                require_profile=True,
            ),
            handler=_handle_odoo_preview_apply_inputs,
            pre_idempotency_validator=_validate_odoo_preview_apply_inputs_profile,
        ),
        _ODOO_PREVIEW_APPLY_ROUTE.route_path: _DescriptorDriverDispatchRoute(
            execution_metadata=_ODOO_PREVIEW_APPLY_ROUTE,
            context_resolver=lambda request: _DescriptorDriverDispatchContext(
                product=request.product,
                context="",
                use_preview_context_for_authorization=True,
                require_profile=True,
            ),
            handler=_handle_odoo_preview_apply,
            pre_idempotency_validator=_validate_odoo_preview_apply_profile,
        ),
        _VERIREEL_PREVIEW_VERIFICATION_ROUTE.route_path: _DescriptorDriverDispatchRoute(
            execution_metadata=_VERIREEL_PREVIEW_VERIFICATION_ROUTE,
            context_resolver=lambda request: _DescriptorDriverDispatchContext(
                product=request.product,
                context="",
                authorization_context=request.verification.context,
            ),
            handler=_handle_verireel_preview_verification,
        ),
        _VERIREEL_PREVIEW_INVENTORY_ROUTE.route_path: _DescriptorDriverDispatchRoute(
            execution_metadata=_VERIREEL_PREVIEW_INVENTORY_ROUTE,
            context_resolver=lambda request: _DescriptorDriverDispatchContext(
                product=request.product,
                context="",
                authorization_context=request.inventory.context,
            ),
            handler=_handle_verireel_preview_inventory,
        ),
        _VERIREEL_PREVIEW_DESTROY_ROUTE.route_path: _DescriptorDriverDispatchRoute(
            execution_metadata=_VERIREEL_PREVIEW_DESTROY_ROUTE,
            context_resolver=lambda request: _DescriptorDriverDispatchContext(
                product=request.product,
                context="",
                authorization_context=request.destroy.context,
            ),
            handler=_handle_verireel_preview_destroy,
        ),
        _VERIREEL_PREVIEW_REFRESH_ROUTE.route_path: _DescriptorDriverDispatchRoute(
            execution_metadata=_VERIREEL_PREVIEW_REFRESH_ROUTE,
            context_resolver=lambda request: _DescriptorDriverDispatchContext(
                product=request.product,
                context="",
                authorization_context=request.refresh.context,
            ),
            handler=_handle_verireel_preview_refresh,
        ),
        _VERIREEL_TESTING_VERIFICATION_ROUTE.route_path: _DescriptorDriverDispatchRoute(
            execution_metadata=_VERIREEL_TESTING_VERIFICATION_ROUTE,
            context_resolver=lambda request: _DescriptorDriverDispatchContext(
                product=request.product,
                context=request.verification.context,
                instance=request.verification.instance,
            ),
            handler=_handle_verireel_testing_verification,
        ),
        _VERIREEL_TESTING_DEPLOY_ROUTE.route_path: _DescriptorDriverDispatchRoute(
            execution_metadata=_VERIREEL_TESTING_DEPLOY_ROUTE,
            context_resolver=lambda request: _DescriptorDriverDispatchContext(
                product=request.product,
                context=request.deploy.context,
                instance=request.deploy.instance,
            ),
            handler=_handle_verireel_testing_deploy,
        ),
        _VERIREEL_PROD_DEPLOY_ROUTE.route_path: _DescriptorDriverDispatchRoute(
            execution_metadata=_VERIREEL_PROD_DEPLOY_ROUTE,
            context_resolver=lambda request: _DescriptorDriverDispatchContext(
                product=request.product,
                context=request.deploy.context,
                instance=request.deploy.instance,
            ),
            handler=_handle_verireel_prod_deploy,
        ),
        _VERIREEL_PROD_BACKUP_GATE_ROUTE.route_path: _DescriptorDriverDispatchRoute(
            execution_metadata=_VERIREEL_PROD_BACKUP_GATE_ROUTE,
            context_resolver=lambda request: _DescriptorDriverDispatchContext(
                product=request.product,
                context=request.backup_gate.context,
                instance=request.backup_gate.instance,
            ),
            handler=_handle_verireel_prod_backup_gate,
        ),
        _VERIREEL_PROD_PROMOTION_ROUTE.route_path: _DescriptorDriverDispatchRoute(
            execution_metadata=_VERIREEL_PROD_PROMOTION_ROUTE,
            context_resolver=lambda request: _DescriptorDriverDispatchContext(
                product=request.product,
                context=request.promotion.context,
                instance=request.promotion.from_instance,
            ),
            handler=_handle_verireel_prod_promotion,
            pre_idempotency_validator=_validate_verireel_prod_promotion_target_lane,
        ),
        _VERIREEL_PROD_ROLLBACK_ROUTE.route_path: _DescriptorDriverDispatchRoute(
            execution_metadata=_VERIREEL_PROD_ROLLBACK_ROUTE,
            context_resolver=lambda request: _DescriptorDriverDispatchContext(
                product=request.product,
                context=request.rollback.context,
                instance=request.rollback.instance,
            ),
            handler=_handle_verireel_prod_rollback,
        ),
        _VERIREEL_STABLE_ENVIRONMENT_ROUTE.route_path: _DescriptorDriverDispatchRoute(
            execution_metadata=_VERIREEL_STABLE_ENVIRONMENT_ROUTE,
            context_resolver=lambda request: _DescriptorDriverDispatchContext(
                product=request.product,
                context=request.environment.context,
                instance=request.environment.instance,
            ),
            handler=_handle_verireel_stable_environment,
        ),
        _VERIREEL_RUNTIME_VERIFICATION_ROUTE.route_path: _DescriptorDriverDispatchRoute(
            execution_metadata=_VERIREEL_RUNTIME_VERIFICATION_ROUTE,
            context_resolver=lambda request: _DescriptorDriverDispatchContext(
                product=request.product,
                context=request.verification.context,
                instance=request.verification.instance,
            ),
            handler=_handle_verireel_runtime_verification,
        ),
        _VERIREEL_APP_MAINTENANCE_ROUTE.route_path: _DescriptorDriverDispatchRoute(
            execution_metadata=_VERIREEL_APP_MAINTENANCE_ROUTE,
            context_resolver=lambda request: _DescriptorDriverDispatchContext(
                product=request.product,
                context="",
                authorization_context=request.maintenance.context,
            ),
            handler=_handle_verireel_app_maintenance,
        ),
    }


def _required_descriptor_driver_dispatch_route_paths() -> frozenset[str]:
    return frozenset(
        (
            _GENERIC_WEB_DEPLOY_ROUTE.route_path,
            _GENERIC_WEB_SOURCE_REF_DEPLOY_ROUTE.route_path,
            _GENERIC_WEB_PROD_PROMOTION_ROUTE.route_path,
            _GENERIC_WEB_PROD_PROMOTION_WORKFLOW_ROUTE.route_path,
            _GENERIC_WEB_ROLLBACK_PLAN_ROUTE.route_path,
            _GENERIC_WEB_ROLLBACK_ROUTE.route_path,
            _GENERIC_WEB_STABLE_VERIFICATION_ROUTE.route_path,
            _GENERIC_WEB_PREVIEW_DESIRED_STATE_ROUTE.route_path,
            _GENERIC_WEB_PREVIEW_INVENTORY_ROUTE.route_path,
            _GENERIC_WEB_PREVIEW_REFRESH_ROUTE.route_path,
            _GENERIC_WEB_PREVIEW_READINESS_ROUTE.route_path,
            _GENERIC_WEB_PREVIEW_DESTROY_ROUTE.route_path,
            _GENERIC_WEB_PREVIEW_VERIFICATION_ROUTE.route_path,
            _ODOO_ARTIFACT_PUBLISH_ROUTE.route_path,
            _ODOO_ARTIFACT_PUBLISH_INPUTS_ROUTE.route_path,
            _ODOO_PROD_PROMOTION_INPUTS_ROUTE.route_path,
            _ODOO_PROD_BACKUP_GATE_ROUTE.route_path,
            _ODOO_PROD_PROMOTION_RUN_ROUTE.route_path,
            _ODOO_PROD_PROMOTION_ROUTE.route_path,
            _ODOO_PROD_ROLLBACK_ROUTE.route_path,
            _ODOO_TARGET_REPLACEMENT_PLAN_ROUTE.route_path,
            _ODOO_TARGET_REPLACEMENT_APPLY_ROUTE.route_path,
            _ODOO_POST_DEPLOY_ROUTE.route_path,
            _ODOO_CONFIG_PARAMETER_OVERRIDE_ROUTE.route_path,
            _ODOO_WEBSITE_BOOTSTRAP_OVERRIDE_ROUTE.route_path,
            _ODOO_STABLE_BOOTSTRAP_ROUTE.route_path,
            _ODOO_PREVIEW_APPLY_INPUTS_ROUTE.route_path,
            _ODOO_PREVIEW_APPLY_ROUTE.route_path,
            _VERIREEL_PREVIEW_VERIFICATION_ROUTE.route_path,
            _VERIREEL_PREVIEW_INVENTORY_ROUTE.route_path,
            _VERIREEL_PREVIEW_DESTROY_ROUTE.route_path,
            _VERIREEL_PREVIEW_REFRESH_ROUTE.route_path,
            _VERIREEL_TESTING_VERIFICATION_ROUTE.route_path,
            _VERIREEL_TESTING_DEPLOY_ROUTE.route_path,
            _VERIREEL_PROD_DEPLOY_ROUTE.route_path,
            _VERIREEL_PROD_BACKUP_GATE_ROUTE.route_path,
            _VERIREEL_PROD_PROMOTION_ROUTE.route_path,
            _VERIREEL_PROD_ROLLBACK_ROUTE.route_path,
            _VERIREEL_STABLE_ENVIRONMENT_ROUTE.route_path,
            _VERIREEL_RUNTIME_VERIFICATION_ROUTE.route_path,
            _VERIREEL_APP_MAINTENANCE_ROUTE.route_path,
        )
    )


def _descriptor_driver_dispatch_exempt_route_paths() -> frozenset[str]:
    return _NATIVE_FASTAPI_DRIVER_ROUTE_PATHS


def _dispatch_descriptor_driver_route(
    *,
    dispatch_route: _DescriptorDriverDispatchRoute[Any],
    payload: dict[str, object],
    record_store: object,
    authz_policy: LaunchplaneAuthzPolicy,
    identity: LaunchplaneIdentity,
    request_scope: str,
    request_idempotency_key: str,
    request_fingerprint: str,
    start_response: _StartResponse,
    trace_id: str,
    control_plane_root_path: Path,
    state_dir: Path,
    database_url: str | None,
) -> tuple[dict[str, object], BaseModel | dict[str, object] | None] | list[bytes]:
    route_metadata = dispatch_route.execution_metadata
    descriptor_route_metadata = _driver_route_metadata_from_descriptors().get(
        route_metadata.route_path
    )
    if descriptor_route_metadata is None:
        raise ValueError(
            f"Descriptor-backed dispatch route {route_metadata.route_path} "
            "must be declared by a driver descriptor."
        )
    if descriptor_route_metadata.method != "POST":
        raise ValueError(
            f"Descriptor-backed dispatch route {route_metadata.route_path} must use POST."
        )
    request = route_metadata.envelope_model.model_validate(payload)
    dispatch_context = dispatch_route.context_resolver(request)
    resolved_driver_context = (
        _ResolvedProductDriverContext(profile=None)
        if dispatch_route.skip_driver_context_resolution
        else _resolve_descriptor_product_driver_context(
            record_store=record_store,
            route_path=route_metadata.route_path,
            product=dispatch_context.product,
            context=dispatch_context.context,
            instance=dispatch_context.instance,
            require_profile=dispatch_context.require_profile,
        )
    )
    if dispatch_route.pre_idempotency_validator is not None:
        dispatch_route.pre_idempotency_validator(
            request,
            resolved_driver_context,
            record_store,
            control_plane_root_path,
        )
    if dispatch_route.pre_authorization_validator is not None:
        pre_authorization_response = dispatch_route.pre_authorization_validator(
            request,
            resolved_driver_context,
            identity,
            start_response,
            trace_id,
        )
        if pre_authorization_response is not None:
            return pre_authorization_response
    authorization_product = dispatch_context.product
    if (
        dispatch_context.use_resolved_profile_product_for_authorization
        and resolved_driver_context.profile is not None
    ):
        authorization_product = resolved_driver_context.profile.product
    authorization_context = dispatch_context.authorization_context or dispatch_context.context
    if not authorization_context and resolved_driver_context.lane is not None:
        authorization_context = resolved_driver_context.lane.context
    if (
        not authorization_context
        and dispatch_context.use_preview_context_for_authorization
        and resolved_driver_context.profile is not None
        and resolved_driver_context.profile.preview.context
    ):
        authorization_context = resolved_driver_context.profile.preview.context
    authorization_response = _driver_route_authorization_response(
        authz_policy=authz_policy,
        identity=identity,
        route_path=route_metadata.route_path,
        action=dispatch_route.authorization_action_resolver(request)
        if dispatch_route.authorization_action_resolver is not None
        else None,
        product=authorization_product,
        context=authorization_context,
        denial_message=route_metadata.denial_message,
        start_response=start_response,
        trace_id=trace_id,
    )
    if authorization_response is not None:
        return authorization_response
    if (
        route_metadata.route_path not in _NON_IDEMPOTENT_DRIVER_RESULT_ROUTES
        and not dispatch_route.skip_pre_idempotency_check
    ):
        idempotent_response = _check_idempotent_request(
            record_store=record_store,
            scope=request_scope,
            route_path=route_metadata.route_path,
            idempotency_key=request_idempotency_key,
            request_fingerprint=request_fingerprint,
            start_response=start_response,
            trace_id=trace_id,
        )
        if idempotent_response is not None:
            return idempotent_response
    if dispatch_route.custom_dispatch_handler is not None:
        return dispatch_route.custom_dispatch_handler(
            request,
            resolved_driver_context,
            record_store,
            control_plane_root_path,
            state_dir,
            database_url,
            identity,
            request_scope,
            request_idempotency_key,
            request_fingerprint,
            start_response,
            trace_id,
        )
    if dispatch_route.handler is None:
        raise ValueError(
            f"Descriptor-backed dispatch route {route_metadata.route_path} must register a handler."
        )
    dispatch_result = dispatch_route.handler(
        request,
        resolved_driver_context,
        record_store,
        control_plane_root_path,
    )
    return dispatch_result.result, dispatch_result.driver_result


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
        503: "Service Unavailable",
        500: "Internal Server Error",
    }.get(status_code, "OK")


def _trace_id() -> str:
    return f"launchplane_req_{uuid.uuid4().hex}"


def _utc_now_timestamp() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_utc_timestamp(value: str) -> datetime:
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


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
        for route_alias in descriptor.route_aliases:
            if not route_alias.route_path.startswith("/v1/drivers/"):
                continue
            if not route_alias.authz_action:
                raise ValueError(
                    f"Driver route alias {descriptor.driver_id}.{route_alias.action_id} "
                    "must declare authz_action."
                )
            if route_alias.route_path in route_metadata:
                raise ValueError(f"Duplicate driver action route path: {route_alias.route_path}")
            route_metadata[route_alias.route_path] = _DriverRouteMetadata(
                driver_id=descriptor.driver_id,
                action_id=route_alias.action_id,
                method=route_alias.method,
                authz_action=route_alias.authz_action,
                operator_visible=route_alias.operator_visible,
            )
    return route_metadata


def _descriptor_driver_authz_action(route_path: str) -> str:
    try:
        return _driver_route_metadata_from_descriptors()[route_path].authz_action
    except KeyError as exc:
        raise ValueError(f"Unknown descriptor-backed driver route: {route_path}") from exc


def _validate_descriptor_driver_dispatch_routes(
    dispatch_routes: dict[str, _DescriptorDriverDispatchRoute[Any]],
) -> None:
    descriptor_routes = _driver_route_metadata_from_descriptors()
    missing_required_routes = sorted(
        _required_descriptor_driver_dispatch_route_paths() - dispatch_routes.keys()
    )
    if missing_required_routes:
        raise ValueError(
            "Descriptor-backed dispatch routes must be registered by the service: "
            f"{', '.join(missing_required_routes)}"
        )
    post_descriptor_routes = frozenset(
        route_path
        for route_path, route_metadata in descriptor_routes.items()
        if route_metadata.method == "POST"
    )
    missing_post_descriptor_routes = sorted(
        post_descriptor_routes
        - _descriptor_driver_dispatch_exempt_route_paths()
        - dispatch_routes.keys()
    )
    if missing_post_descriptor_routes:
        raise ValueError(
            "POST driver descriptor routes must be registered for descriptor-backed "
            f"dispatch: {', '.join(missing_post_descriptor_routes)}"
        )
    for route_path, dispatch_route in dispatch_routes.items():
        if route_path != dispatch_route.execution_metadata.route_path:
            raise ValueError(
                f"Descriptor-backed dispatch route {route_path} must match execution metadata."
            )
        has_standard_handler = dispatch_route.handler is not None
        has_custom_handler = dispatch_route.custom_dispatch_handler is not None
        if has_standard_handler == has_custom_handler:
            raise ValueError(
                f"Descriptor-backed dispatch route {route_path} must register exactly one handler."
            )
        descriptor_route = descriptor_routes.get(route_path)
        if descriptor_route is None:
            raise ValueError(
                f"Descriptor-backed dispatch route {route_path} must be declared by a driver descriptor."
            )
        if descriptor_route.method != "POST":
            raise ValueError(
                f"Descriptor-backed dispatch route {route_path} must be declared as POST."
            )


def _driver_write_routes_from_descriptors() -> frozenset[str]:
    return frozenset(
        route_path
        for route_path, route_metadata in _driver_route_metadata_from_descriptors().items()
        if route_metadata.method == "POST" and route_path not in _NATIVE_FASTAPI_DRIVER_ROUTE_PATHS
    )


def _build_write_routes() -> frozenset[str]:
    launchplane_write_routes = {
        _MERGE_TRAIN_BATCH_CANDIDATE_RUN_ONCE_ROUTE,
        _MERGE_TRAIN_BATCH_LANDING_RUN_ONCE_ROUTE,
        _MERGE_TRAIN_CONTROLLER_RUN_ONCE_ROUTE,
        _MERGE_TRAIN_PR_FEEDBACK_ROUTE,
        _MERGE_TRAIN_STACK_COLLAPSE_RUN_ONCE_ROUTE,
        _MERGE_TRAIN_RUN_ONCE_ROUTE,
        "/v1/previews/desired-state",
        "/v1/previews/pr-feedback",
        "/v1/previews/lifecycle-cleanup",
        "/v1/previews/lifecycle-sweep",
        "/v1/drivers/launchplane/self-deploy",
    }
    return frozenset(launchplane_write_routes | set(_driver_write_routes_from_descriptors()))


def _secret_capable_store(record_store: object) -> control_plane_secrets.SecretReadStore | None:
    if hasattr(record_store, "read_secret_record") and hasattr(record_store, "list_secret_records"):
        return cast(control_plane_secrets.SecretReadStore, record_store)
    return None


class _OdooInstanceOverrideStore(Protocol):
    def read_odoo_instance_override_record(
        self, *, context_name: str, instance_name: str
    ) -> OdooInstanceOverrideRecord: ...

    def write_odoo_instance_override_record(self, record: OdooInstanceOverrideRecord) -> object: ...


def _write_odoo_config_parameter_override(
    *,
    record_store: _OdooInstanceOverrideStore,
    request: OdooConfigParameterOverrideRequest,
) -> OdooInstanceOverrideRecord:
    try:
        existing_record = record_store.read_odoo_instance_override_record(
            context_name=request.context, instance_name=request.instance
        )
    except FileNotFoundError:
        existing_record = None
    config_parameters = {
        override.key: override
        for override in (existing_record.config_parameters if existing_record is not None else ())
    }
    addon_settings = existing_record.addon_settings if existing_record is not None else ()
    config_parameters[request.key] = OdooConfigParameterOverride(
        key=request.key,
        value=OdooOverrideValue(source="literal", value=request.value),
    )
    apply_on = tuple(
        dict.fromkeys(
            (
                *(existing_record.apply_on if existing_record is not None else ()),
                "deploy",
                "promotion",
            )
        )
    )
    record = OdooInstanceOverrideRecord(
        context=request.context,
        instance=request.instance,
        apply_on=apply_on,
        config_parameters=tuple(config_parameters[key] for key in sorted(config_parameters)),
        addon_settings=addon_settings,
        website_bootstrap=existing_record.website_bootstrap
        if existing_record is not None
        else None,
        updated_at=_utc_now_timestamp(),
        source_label=request.source_label,
    )
    record_store.write_odoo_instance_override_record(record)
    return record


def _write_odoo_website_bootstrap_override(
    *,
    record_store: _OdooInstanceOverrideStore,
    request: OdooWebsiteBootstrapOverrideRequest,
) -> OdooInstanceOverrideRecord:
    try:
        existing_record = record_store.read_odoo_instance_override_record(
            context_name=request.context, instance_name=request.instance
        )
    except FileNotFoundError:
        existing_record = None
    apply_on = tuple(
        dict.fromkeys(
            (
                *(existing_record.apply_on if existing_record is not None else ()),
                "deploy",
                "promotion",
            )
        )
    )
    record = OdooInstanceOverrideRecord(
        context=request.context,
        instance=request.instance,
        apply_on=apply_on,
        config_parameters=existing_record.config_parameters if existing_record is not None else (),
        addon_settings=existing_record.addon_settings if existing_record is not None else (),
        website_bootstrap=request.website_bootstrap,
        updated_at=_utc_now_timestamp(),
        source_label=request.source_label,
    )
    record_store.write_odoo_instance_override_record(record)
    return record


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

    def write_every_code_preview_gate_record(
        self, record: EveryCodePreviewGateRecord
    ) -> object: ...

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


class _PreviewPrFeedbackNotificationStore(Protocol):
    def write_preview_pr_feedback_notification_policy_record(
        self, record: PreviewPrFeedbackNotificationPolicyRecord
    ) -> object: ...

    def list_preview_pr_feedback_notification_policy_records(
        self,
        *,
        product: str = "",
        context_name: str = "",
        repository: str = "",
        status: str = "",
        limit: int | None = None,
    ) -> tuple[PreviewPrFeedbackNotificationPolicyRecord, ...]: ...

    def write_preview_pr_feedback_notification_attempt_record(
        self, record: PreviewPrFeedbackNotificationAttemptRecord
    ) -> object: ...

    def list_preview_pr_feedback_notification_attempt_records(
        self,
        *,
        feedback_id: str = "",
        event: str = "",
        destination_kind: str = "",
        limit: int | None = None,
    ) -> tuple[PreviewPrFeedbackNotificationAttemptRecord, ...]: ...


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


def _preview_pr_feedback_notification_store(
    record_store: object,
) -> _PreviewPrFeedbackNotificationStore | None:
    required_methods = (
        "write_preview_pr_feedback_notification_policy_record",
        "list_preview_pr_feedback_notification_policy_records",
        "write_preview_pr_feedback_notification_attempt_record",
        "list_preview_pr_feedback_notification_attempt_records",
    )
    if all(hasattr(record_store, method_name) for method_name in required_methods):
        return cast(_PreviewPrFeedbackNotificationStore, record_store)
    return None


class _MergeTrainBatchCandidateRecordStore(Protocol):
    def write_merge_train_batch_candidate_record(
        self, record: MergeTrainBatchCandidateRecord
    ) -> object: ...

    def list_merge_train_batch_candidate_records(
        self,
        *,
        repository: str = "",
        base_branch: str = "",
        status: str = "",
        limit: int | None = None,
    ) -> tuple[MergeTrainBatchCandidateRecord, ...]: ...


def _merge_train_batch_candidate_record_store(
    record_store: object,
) -> _MergeTrainBatchCandidateRecordStore:
    if hasattr(record_store, "write_merge_train_batch_candidate_record") and hasattr(
        record_store, "list_merge_train_batch_candidate_records"
    ):
        return cast(_MergeTrainBatchCandidateRecordStore, record_store)
    raise TypeError("record store does not support merge train batch candidate records")


class _MergeTrainBatchLandingPlanRecordStore(Protocol):
    def write_merge_train_batch_landing_plan_record(
        self, record: MergeTrainBatchLandingPlanRecord
    ) -> object: ...

    def list_merge_train_batch_landing_plan_records(
        self,
        *,
        repository: str = "",
        base_branch: str = "",
        status: str = "",
        limit: int | None = None,
    ) -> tuple[MergeTrainBatchLandingPlanRecord, ...]: ...


def _merge_train_batch_landing_plan_record_store(
    record_store: object,
) -> _MergeTrainBatchLandingPlanRecordStore:
    if hasattr(record_store, "write_merge_train_batch_landing_plan_record") and hasattr(
        record_store, "list_merge_train_batch_landing_plan_records"
    ):
        return cast(_MergeTrainBatchLandingPlanRecordStore, record_store)
    raise TypeError("record store does not support merge train batch landing plan records")


class _MergeTrainStackCollapsePlanRecordStore(Protocol):
    def write_merge_train_stack_collapse_plan_record(
        self, record: MergeTrainStackCollapsePlanRecord
    ) -> object: ...

    def list_merge_train_stack_collapse_plan_records(
        self,
        *,
        repository: str = "",
        base_branch: str = "",
        status: str = "",
        limit: int | None = None,
    ) -> tuple[MergeTrainStackCollapsePlanRecord, ...]: ...


def _merge_train_stack_collapse_plan_record_store(
    record_store: object,
) -> _MergeTrainStackCollapsePlanRecordStore:
    if hasattr(record_store, "write_merge_train_stack_collapse_plan_record") and hasattr(
        record_store, "list_merge_train_stack_collapse_plan_records"
    ):
        return cast(_MergeTrainStackCollapsePlanRecordStore, record_store)
    raise TypeError("record store does not support merge train stack collapse plans")


class _MergeTrainPrFeedbackRecordStore(Protocol):
    def write_merge_train_pr_feedback_record(
        self, record: MergeTrainPrFeedbackRecord
    ) -> object: ...

    def list_merge_train_pr_feedback_records(
        self,
        *,
        repository: str = "",
        base_branch: str = "",
        pr_number: int | None = None,
        limit: int | None = None,
    ) -> tuple[MergeTrainPrFeedbackRecord, ...]: ...


def _merge_train_pr_feedback_record_store(
    record_store: object,
) -> _MergeTrainPrFeedbackRecordStore:
    if hasattr(record_store, "write_merge_train_pr_feedback_record") and hasattr(
        record_store, "list_merge_train_pr_feedback_records"
    ):
        return cast(_MergeTrainPrFeedbackRecordStore, record_store)
    raise TypeError("record store does not support merge train PR feedback records")


def _merge_train_snapshot_has_stack_topology(
    *, snapshot: MergeTrainDryRunSnapshot, dry_run_result: MergeTrainDryRunResult
) -> bool:
    selected_pr = dry_run_result.selected_pr
    if selected_pr is None:
        return False
    for pull_request in snapshot.pull_requests:
        if pull_request.number != selected_pr.number:
            continue
        return all(
            (
                pull_request.head_ref,
                pull_request.head_repository,
                pull_request.base_ref,
                pull_request.base_repository,
            )
        )
    return False


def _build_merge_train_pr_feedback_record(
    *,
    request: MergeTrainPrFeedbackEnvelope,
    policy_key: str,
    policy_sha256: str,
    token: str,
    recorded_at: str,
    response_trace_id: str,
) -> MergeTrainPrFeedbackRecord:
    marker = merge_train_pr_feedback_marker(
        repository=request.repository,
        base_branch=request.base_branch,
        pull_request_number=request.pull_request_number,
    )
    comment_markdown = _render_merge_train_pr_feedback_markdown(
        marker=marker,
        request=request,
    )
    delivery_status: Literal["delivered", "skipped", "failed"] = "skipped"
    delivery_action = ""
    comment_id = 0
    comment_url = ""
    error_message = ""
    owner, repo = request.repository.split("/", 1)
    if not token:
        error_message = "Configured merge train GitHub token is not available."
    else:
        try:
            existing_comment = find_github_issue_comment_by_marker(
                owner=owner,
                repo=repo,
                issue_number=request.pull_request_number,
                token=token,
                marker=marker,
            )
            if existing_comment is not None:
                existing_comment_id = existing_comment.get("id")
                if not isinstance(existing_comment_id, int):
                    raise click.ClickException(
                        "Existing merge train feedback comment is missing a numeric id."
                    )
                updated_comment = update_github_issue_comment(
                    owner=owner,
                    repo=repo,
                    comment_id=existing_comment_id,
                    token=token,
                    body=comment_markdown,
                )
                delivery_action = "updated_comment"
                comment_id = existing_comment_id
                comment_url = _github_comment_url(updated_comment)
            else:
                created_comment = create_github_issue_comment(
                    owner=owner,
                    repo=repo,
                    issue_number=request.pull_request_number,
                    token=token,
                    body=comment_markdown,
                )
                created_comment_id = created_comment.get("id")
                delivery_action = "created_comment"
                comment_id = created_comment_id if isinstance(created_comment_id, int) else 0
                comment_url = _github_comment_url(created_comment)
            delivery_status = "delivered"
        except click.ClickException as exc:
            delivery_status = "failed"
            error_message = str(exc)
    return MergeTrainPrFeedbackRecord(
        feedback_id=build_merge_train_pr_feedback_id(
            repository=request.repository,
            base_branch=request.base_branch,
            pull_request_number=request.pull_request_number,
            event=request.event,
            marker=marker,
            recorded_at=recorded_at,
            response_trace_id=response_trace_id,
        ),
        repository=request.repository,
        base_branch=request.base_branch,
        pull_request_number=request.pull_request_number,
        pull_request_url=(
            f"https://github.com/{request.repository}/pull/{request.pull_request_number}"
        ),
        event=request.event,
        marker=marker,
        comment_markdown=comment_markdown,
        source=request.source or "service:merge-train-pr-feedback",
        recorded_at=recorded_at,
        policy_key=policy_key,
        policy_sha256=policy_sha256,
        controller_action=request.controller_action,
        controller_record_id=request.controller_record_id,
        delivery_status=delivery_status,
        delivery_action=delivery_action,
        comment_id=comment_id,
        comment_url=comment_url,
        error_message=error_message,
    )


def _render_merge_train_pr_feedback_markdown(
    *, marker: str, request: MergeTrainPrFeedbackEnvelope
) -> str:
    event_titles = {
        "queued": "Launchplane queued this pull request in the merge train.",
        "building": "Launchplane is building a merge-train candidate.",
        "waiting": "Launchplane is waiting before the next merge-train step.",
        "blocked": "Launchplane blocked the merge-train step for this pull request.",
        "stale_policy": "Launchplane parked this merge-train record because policy changed.",
        "completed": "Launchplane completed the merge-train step for this pull request.",
    }
    lines = [
        marker,
        event_titles[request.event],
        "",
        f"- Repository: `{request.repository}`",
        f"- Base branch: `{request.base_branch}`",
        f"- Pull request: #{request.pull_request_number}",
    ]
    if request.controller_action:
        lines.append(f"- Controller action: `{request.controller_action}`")
    if request.controller_record_id:
        lines.append(f"- Controller record: `{request.controller_record_id}`")
    if request.message:
        lines.extend(["", request.message])
    lines.extend(
        [
            "",
            "Launchplane manages this comment and will update it as the train moves.",
        ]
    )
    return "\n".join(lines)


def _read_merge_train_batch_candidate_record(
    *,
    record_store: _MergeTrainBatchCandidateRecordStore,
    repository: str,
    base_branch: str,
    record_id: str,
) -> MergeTrainBatchCandidateRecord:
    records = record_store.list_merge_train_batch_candidate_records(
        repository=repository, base_branch=base_branch
    )
    for record in records:
        if record.record_id == record_id:
            return record
    raise ValueError("merge train batch candidate record not found")


def _read_merge_train_batch_landing_plan_record(
    *,
    record_store: _MergeTrainBatchLandingPlanRecordStore,
    repository: str,
    base_branch: str,
    record_id: str,
) -> MergeTrainBatchLandingPlanRecord:
    records = record_store.list_merge_train_batch_landing_plan_records(
        repository=repository, base_branch=base_branch
    )
    for record in records:
        if record.record_id == record_id:
            return record
    raise ValueError("merge train batch landing plan record not found")


def _read_merge_train_stack_collapse_plan_record(
    *,
    record_store: _MergeTrainStackCollapsePlanRecordStore,
    repository: str,
    base_branch: str,
    record_id: str,
) -> MergeTrainStackCollapsePlanRecord:
    records = record_store.list_merge_train_stack_collapse_plan_records(
        repository=repository, base_branch=base_branch
    )
    for record in records:
        if record.record_id == record_id:
            return record
    raise ValueError("merge train stack collapse plan record not found")


def _latest_merge_train_batch_candidate_record(
    *,
    record_store: _MergeTrainBatchCandidateRecordStore,
    repository: str,
    base_branch: str,
) -> MergeTrainBatchCandidateRecord | None:
    records = record_store.list_merge_train_batch_candidate_records(
        repository=repository,
        base_branch=base_branch,
        status="active",
        limit=25,
    )
    latest_record = _latest_merge_train_batch_candidate_progress_record(records)
    if latest_record is None:
        return None
    terminal_statuses = {"passed", "stale", "blocked"}
    if latest_record.candidate.status in terminal_statuses:
        return None
    return latest_record


def _latest_passed_merge_train_batch_candidate_record(
    *,
    record_store: _MergeTrainBatchCandidateRecordStore,
    landing_plan_record_store: _MergeTrainBatchLandingPlanRecordStore,
    repository: str,
    base_branch: str,
) -> MergeTrainBatchCandidateRecord | None:
    records = record_store.list_merge_train_batch_candidate_records(
        repository=repository,
        base_branch=base_branch,
        status="active",
        limit=25,
    )
    latest_record = _latest_merge_train_batch_candidate_progress_record(records)
    if latest_record is None:
        return None
    if latest_record.candidate.status != "passed":
        return None
    completed_landing_record = _latest_completed_merge_train_batch_landing_plan_record(
        record_store=landing_plan_record_store,
        repository=repository,
        base_branch=base_branch,
        batch_id=latest_record.candidate.batch_id,
        candidate_sha=latest_record.candidate.candidate_sha,
    )
    if completed_landing_record is not None:
        return None
    return latest_record


def _latest_merge_train_batch_landing_plan_record(
    *,
    record_store: _MergeTrainBatchLandingPlanRecordStore,
    repository: str,
    base_branch: str,
) -> MergeTrainBatchLandingPlanRecord | None:
    records = record_store.list_merge_train_batch_landing_plan_records(
        repository=repository,
        base_branch=base_branch,
        status="active",
        limit=25,
    )
    latest_record = _latest_merge_train_batch_landing_progress_record(records)
    if latest_record is None:
        return None
    if not any(entry.status == "planned" for entry in latest_record.landing_plan.entries):
        return None
    return latest_record


def _latest_completed_merge_train_batch_landing_plan_record(
    *,
    record_store: _MergeTrainBatchLandingPlanRecordStore,
    repository: str,
    base_branch: str,
    batch_id: str,
    candidate_sha: str,
) -> MergeTrainBatchLandingPlanRecord | None:
    records = record_store.list_merge_train_batch_landing_plan_records(
        repository=repository,
        base_branch=base_branch,
        status="active",
        limit=25,
    )
    matching_records = tuple(
        record
        for record in records
        if record.landing_plan.batch_id == batch_id
        and record.landing_plan.candidate_sha == candidate_sha
    )
    latest_record = _latest_merge_train_batch_landing_progress_record(matching_records)
    if latest_record is None:
        return None
    if not latest_record.landing_plan.entries:
        return None
    if any(entry.status not in {"merged", "stale"} for entry in latest_record.landing_plan.entries):
        return None
    return latest_record


def _stale_merge_train_landing_plan(
    landing_plan: MergeTrainBatchLandingPlan,
) -> MergeTrainBatchLandingPlan:
    return landing_plan.model_copy(
        update={
            "entries": tuple(
                entry.model_copy(update={"status": "stale"})
                if entry.status in {"planned", "merging"}
                else entry
                for entry in landing_plan.entries
            )
        }
    )


def _latest_merge_train_stack_collapse_plan_record(
    *,
    record_store: _MergeTrainStackCollapsePlanRecordStore,
    repository: str,
    base_branch: str,
    plan_status: str,
) -> MergeTrainStackCollapsePlanRecord | None:
    records = record_store.list_merge_train_stack_collapse_plan_records(
        repository=repository,
        base_branch=base_branch,
        status="active",
        limit=25,
    )
    latest_record = _latest_merge_train_stack_collapse_progress_record(records)
    if latest_record is None:
        return None
    if latest_record.plan.status != plan_status:
        return None
    return latest_record


def _latest_merge_train_stack_collapse_plan_record_for_landing(
    *,
    record_store: _MergeTrainStackCollapsePlanRecordStore,
    repository: str,
    base_branch: str,
    landing_plan: MergeTrainBatchLandingPlan,
    policy_sha256: str,
) -> MergeTrainStackCollapsePlanRecord | None:
    records = record_store.list_merge_train_stack_collapse_plan_records(
        repository=repository,
        base_branch=base_branch,
        status="active",
        limit=25,
    )
    compatible_records = tuple(
        record
        for record in records
        if _merge_train_stack_collapse_record_matches_landing_plan(
            collapse_record=record,
            landing_plan=landing_plan,
            policy_sha256=policy_sha256,
        )
    )
    return _latest_merge_train_stack_collapse_progress_record(compatible_records)


def _merge_train_stack_collapse_record_matches_landing_plan(
    *,
    collapse_record: MergeTrainStackCollapsePlanRecord,
    landing_plan: MergeTrainBatchLandingPlan,
    policy_sha256: str,
) -> bool:
    try:
        _validate_stack_collapse_record_for_landing(
            collapse_record=collapse_record,
            landing_plan=landing_plan,
            policy_sha256=policy_sha256,
        )
    except ValueError:
        return False
    return True


def _validate_merge_train_candidate_record_for_controller(
    *,
    candidate_record: MergeTrainBatchCandidateRecord,
    policy_key: str,
    policy_sha256: str,
) -> None:
    if candidate_record.candidate.policy_key != policy_key:
        raise ValueError("merge train candidate policy key no longer matches")
    if candidate_record.candidate.policy_sha256 != policy_sha256:
        raise ValueError("merge train candidate policy digest no longer matches")


def _merge_train_candidate_matches_dry_run_queue(
    *, candidate: MergeTrainBatchCandidate, dry_run_result: MergeTrainDryRunResult, base_sha: str
) -> bool:
    if candidate.repository != dry_run_result.repository:
        return False
    if candidate.base_branch != dry_run_result.base_branch:
        return False
    if candidate.base_sha != base_sha:
        return False
    candidate_entries = tuple(
        (entry.pull_request_number, entry.head_sha) for entry in candidate.entries
    )
    queue_by_number = {entry.number: entry for entry in dry_run_result.queue}
    current_entries: list[tuple[int, str]] = []
    for pull_request_number in dry_run_result.queue_order:
        queue_entry = queue_by_number[pull_request_number]
        if not queue_entry.eligible:
            return False
        current_entries.append((queue_entry.number, queue_entry.head_sha))
    return candidate_entries == tuple(current_entries)


def _supersede_active_merge_train_batch_candidate_records(
    *,
    record_store: _MergeTrainBatchCandidateRecordStore,
    repository: str,
    base_branch: str,
    batch_id: str,
    replacement_record_id: str,
) -> None:
    records = record_store.list_merge_train_batch_candidate_records(
        repository=repository,
        base_branch=base_branch,
        status="active",
    )
    for record in records:
        if record.record_id == replacement_record_id:
            continue
        if record.candidate.batch_id != batch_id:
            continue
        record_store.write_merge_train_batch_candidate_record(
            record.model_copy(update={"status": "superseded"})
        )


def _try_reflow_failed_merge_train_candidate(
    *,
    candidate_store: _MergeTrainBatchCandidateRecordStore,
    active_candidate_record: MergeTrainBatchCandidateRecord,
    policy: MergeTrainPolicy,
    policy_sha256: str,
    transport: MergeTrainGitHubTransport,
    repository: str,
    base_branch: str,
    recorded_at: str,
    request_trace_id: str,
    mutate: bool,
) -> dict[str, object] | None:
    try:
        snapshot = GitHubMergeTrainSnapshotReader(transport=transport).read_merge_train_snapshot(
            repository=repository,
            base_branch=base_branch,
        )
    except Exception:
        return None
    dry_run_result = build_merge_train_dry_run_result(policy=policy, snapshot=snapshot)
    if dry_run_result.intended_next_action != "merge":
        return None
    if _merge_train_candidate_matches_dry_run_queue(
        candidate=active_candidate_record.candidate,
        dry_run_result=dry_run_result,
        base_sha=snapshot.base_sha,
    ):
        return None
    candidate = build_merge_train_batch_candidate(
        dry_run_result=dry_run_result,
        base_sha=snapshot.base_sha,
        policy_sha256=policy_sha256,
        created_at=recorded_at,
    )
    result: dict[str, object] = {
        "repository": candidate.repository,
        "base_branch": candidate.base_branch,
        "mode": "dry-run" if not mutate else "plan_candidate",
        "controller_action": "plan_candidate",
        "superseded_merge_train_batch_candidate_record_id": active_candidate_record.record_id,
        "dry_run_result": dry_run_result.model_dump(mode="json"),
        "candidate": candidate.model_dump(mode="json"),
    }
    if mutate:
        candidate_record = build_merge_train_batch_candidate_record(
            candidate=candidate,
            source=f"service:controller:candidate-reflow:{request_trace_id}",
            updated_at=recorded_at,
        )
        candidate_store.write_merge_train_batch_candidate_record(candidate_record)
        try:
            _supersede_active_merge_train_batch_candidate_records(
                record_store=candidate_store,
                repository=repository,
                base_branch=base_branch,
                batch_id=active_candidate_record.candidate.batch_id,
                replacement_record_id=candidate_record.record_id,
            )
        except Exception:
            candidate_store.write_merge_train_batch_candidate_record(
                candidate_record.model_copy(update={"status": "superseded"})
            )
            raise
        result["merge_train_batch_candidate_record_id"] = candidate_record.record_id
    return result


def _validate_merge_train_landing_record_for_controller(
    *,
    landing_record: MergeTrainBatchLandingPlanRecord,
    policy_key: str,
    policy_sha256: str,
) -> None:
    if landing_record.landing_plan.policy_key != policy_key:
        raise ValueError("merge train landing plan policy key no longer matches")
    if landing_record.landing_plan.policy_sha256 != policy_sha256:
        raise ValueError("merge train landing plan policy digest no longer matches")


def _validate_merge_train_stack_collapse_record_for_controller(
    *,
    collapse_record: MergeTrainStackCollapsePlanRecord,
    policy_key: str,
    policy_sha256: str,
) -> None:
    if collapse_record.plan.policy_key != policy_key:
        raise ValueError("merge train stack collapse policy key no longer matches")
    if collapse_record.plan.policy_sha256 != policy_sha256:
        raise ValueError("merge train stack collapse policy digest no longer matches")


def _latest_merge_train_batch_candidate_progress_record(
    records: tuple[MergeTrainBatchCandidateRecord, ...],
) -> MergeTrainBatchCandidateRecord | None:
    return controller_latest_merge_train_batch_candidate_progress_record(records)


def _latest_merge_train_batch_landing_progress_record(
    records: tuple[MergeTrainBatchLandingPlanRecord, ...],
) -> MergeTrainBatchLandingPlanRecord | None:
    return controller_latest_merge_train_batch_landing_progress_record(records)


def _latest_merge_train_stack_collapse_progress_record(
    records: tuple[MergeTrainStackCollapsePlanRecord, ...],
) -> MergeTrainStackCollapsePlanRecord | None:
    return controller_latest_merge_train_stack_collapse_progress_record(records)


def _merge_train_batch_landing_entry_rank(status: str) -> int:
    return controller_merge_train_batch_landing_entry_rank(status)


def _validate_stack_collapse_record_for_landing(
    *,
    collapse_record: MergeTrainStackCollapsePlanRecord,
    landing_plan: MergeTrainBatchLandingPlan,
    policy_sha256: str,
) -> None:
    stack_collapse_plan = collapse_record.plan
    if stack_collapse_plan.repository != landing_plan.repository:
        raise ValueError("merge train stack collapse repository does not match landing plan")
    if stack_collapse_plan.base_branch != landing_plan.base_branch:
        raise ValueError("merge train stack collapse base branch does not match landing plan")
    if stack_collapse_plan.policy_key != landing_plan.policy_key:
        raise ValueError("merge train stack collapse policy key does not match landing plan")
    if stack_collapse_plan.policy_sha256 != policy_sha256:
        raise ValueError("merge train stack collapse policy digest no longer matches")
    if stack_collapse_plan.policy_sha256 != landing_plan.policy_sha256:
        raise ValueError("merge train stack collapse policy digest does not match landing plan")
    if stack_collapse_plan.status != "waiting_for_root_checks":
        raise ValueError("merge train stack collapse plan is not ready for landing")
    root_entry = next(
        (
            entry
            for entry in landing_plan.entries
            if entry.pull_request_number == stack_collapse_plan.root_pull_request_number
        ),
        None,
    )
    if root_entry is None:
        raise ValueError("merge train stack collapse root PR is missing from landing plan")
    if root_entry.expected_head_sha != _stack_collapse_expected_root_head_sha(stack_collapse_plan):
        raise ValueError("merge train stack collapse root PR head no longer matches")


def _cleanup_merge_train_batch_candidate_ref(
    *,
    github_client: GitHubMergeTrainClient,
    landing_plan: MergeTrainBatchLandingPlan,
    request_trace_id: str,
) -> dict[str, object]:
    try:
        deleted = github_client.cleanup_batch_candidate_ref(landing_plan=landing_plan)
    except MergeTrainGitHubError as error:
        message = str(error).strip() or "GitHub candidate ref cleanup failed."
        _LOGGER.warning(
            "Merge train candidate ref cleanup failed after landing persistence",
            extra={
                "trace_id": request_trace_id,
                "repository": landing_plan.repository,
                "base_branch": landing_plan.base_branch,
                "candidate_ref": landing_plan.candidate_ref,
                "github_status_code": error.status_code,
            },
        )
        result: dict[str, object] = {
            "candidate_ref_cleanup_status": "failed",
            "candidate_ref_cleanup_message": message,
        }
        if error.status_code is not None:
            result["candidate_ref_cleanup_github_status_code"] = error.status_code
        return result
    return {
        "candidate_ref_cleanup_status": "deleted" if deleted else "already_missing",
    }


def _stack_collapse_expected_root_head_sha(plan: MergeTrainStackCollapsePlan) -> str:
    for mutation in plan.mutations:
        if mutation.parent_pull_request_number == plan.root_pull_request_number:
            return mutation.merge_commit_sha or plan.root_initial_head_sha
    return plan.root_initial_head_sha


def _supports_every_code_work_requests(record_store: object) -> bool:
    return hasattr(record_store, "list_every_code_work_request_records")


def _github_webhook_mapping(payload: dict[str, object], key: str) -> dict[str, object] | None:
    value = payload.get(key)
    if isinstance(value, dict):
        return cast(dict[str, object], value)
    return None


def _github_webhook_string(mapping: dict[str, object] | None, key: str) -> str:
    if mapping is None:
        return ""
    value = mapping.get(key)
    return value.strip() if isinstance(value, str) else ""


def _github_webhook_raw_string(mapping: dict[str, object] | None, key: str) -> str:
    if mapping is None:
        return ""
    value = mapping.get(key)
    return value if isinstance(value, str) else ""


def _github_webhook_positive_int(mapping: dict[str, object] | None, key: str) -> int | None:
    if mapping is None:
        return None
    value = mapping.get(key)
    if type(value) is int and value >= 1:
        return value
    return None


def _github_repository_full_name_is_valid(repository: str) -> bool:
    if repository.strip() != repository:
        return False
    owner, separator, name = repository.partition("/")
    if not (owner and separator and name) or "/" in name:
        return False
    return all(_github_repository_component_is_valid(part) for part in (owner, name))


def _github_repository_component_is_valid(value: str) -> bool:
    return bool(
        value
        and value.strip() == value
        and all(character.isalnum() or character in {".", "_", "-"} for character in value)
    )


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
    managers: set[str] = set()
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
        default_manager = workflow.get("default_manager")
        if isinstance(default_manager, str) and default_manager.strip():
            managers.add(_github_login_normalized(default_manager))
        repo_managers = workflow.get("repo_managers")
        if isinstance(repo_managers, dict):
            repo_manager = repo_managers.get(repository) or repo_managers.get(normalized_repository)
            if isinstance(repo_manager, str) and repo_manager.strip():
                managers.add(_github_login_normalized(repo_manager))
    return frozenset(manager for manager in managers if manager)


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
    trace_id: str,
    delivery_id: str,
) -> _EveryCodeWebhookResponse:
    return (
        202,
        {
            "status": "accepted",
            "trace_id": trace_id,
            "skipped": True,
            "reason": "untrusted_actor",
            "github_delivery_id": delivery_id,
        },
    )


def _every_code_github_webhook_invalid_payload_response(
    trace_id: str,
) -> _EveryCodeWebhookResponse:
    return (
        400,
        {
            "status": "rejected",
            "trace_id": trace_id,
            "error": {
                "code": "invalid_request",
                "message": "GitHub webhook payload is invalid.",
            },
        },
    )


def handle_every_code_github_webhook_request(
    body_bytes: bytes,
    event_name: str,
    delivery_id: str,
    signature_header: str,
    record_store: object,
    control_plane_root_path: Path,
    trace_id: str,
) -> _EveryCodeWebhookResponse:
    secret = os.environ.get(_EVERY_CODE_GITHUB_WEBHOOK_SECRET_ENV_KEY, "").strip()
    if not secret:
        return (
            503,
            {
                "status": "rejected",
                "trace_id": trace_id,
                "error": {
                    "code": "webhook_secret_not_configured",
                    "message": "Every Code GitHub webhook secret is not configured.",
                },
            },
        )

    try:
        verify_github_webhook_signature(
            payload_bytes=body_bytes,
            signature_header=signature_header,
            secret=secret,
        )
    except click.ClickException:
        return (
            401,
            {
                "status": "rejected",
                "trace_id": trace_id,
                "error": {
                    "code": "webhook_signature_invalid",
                    "message": "GitHub webhook signature verification failed.",
                },
            },
        )

    if not delivery_id.strip():
        return (
            400,
            {
                "status": "rejected",
                "trace_id": trace_id,
                "error": {
                    "code": "github_delivery_required",
                    "message": "GitHub webhook delivery id is required.",
                },
            },
        )

    normalized_delivery_id = delivery_id.strip()
    normalized_event_name = event_name.strip()
    payload = _decode_json_request_body_or_none(body_bytes)
    if payload is None:
        return _every_code_github_webhook_invalid_payload_response(trace_id)
    return _handle_decoded_every_code_github_webhook_request(
        trace_id=trace_id,
        normalized_delivery_id=normalized_delivery_id,
        normalized_event_name=normalized_event_name,
        payload=payload,
        record_store=record_store,
        control_plane_root_path=control_plane_root_path,
    )


def _handle_decoded_every_code_github_webhook_request(
    *,
    trace_id: str,
    normalized_delivery_id: str,
    normalized_event_name: str,
    payload: dict[str, object],
    record_store: object,
    control_plane_root_path: Path,
) -> _EveryCodeWebhookResponse:
    if normalized_event_name == "issue_comment":
        preview_validation_response = _handle_every_code_preview_validation_webhook(
            trace_id=trace_id,
            delivery_id=normalized_delivery_id,
            payload=payload,
            record_store=record_store,
            control_plane_root_path=control_plane_root_path,
        )
        if preview_validation_response is not None:
            return preview_validation_response
    if normalized_event_name in {
        "issue_comment",
        "pull_request_review",
        "pull_request_review_comment",
    }:
        return _handle_every_code_pr_feedback_webhook(
            trace_id=trace_id,
            delivery_id=normalized_delivery_id,
            event_name=normalized_event_name,
            payload=payload,
            record_store=record_store,
        )
    if normalized_event_name == "pull_request":
        return _handle_every_code_pull_request_webhook(
            trace_id=trace_id,
            delivery_id=normalized_delivery_id,
            payload=payload,
            record_store=record_store,
        )
    if normalized_event_name != "issues":
        return (
            202,
            {
                "status": "accepted",
                "trace_id": trace_id,
                "skipped": True,
                "reason": "unsupported_event",
            },
        )
    if payload.get("action") == "closed":
        return _handle_every_code_issue_closed_webhook(
            trace_id=trace_id,
            delivery_id=normalized_delivery_id,
            payload=payload,
            record_store=record_store,
        )
    if payload.get("action") != "labeled":
        return (
            202,
            {
                "status": "accepted",
                "trace_id": trace_id,
                "skipped": True,
                "reason": "unsupported_action",
            },
        )

    label = _github_webhook_mapping(payload, "label")
    label_name = _github_webhook_string(label, "name")
    if label_name.strip().lower() != _EVERY_CODE_TRIGGER_LABEL:
        return (
            202,
            {
                "status": "accepted",
                "trace_id": trace_id,
                "skipped": True,
                "reason": "label_not_matched",
            },
        )

    repository_payload = _github_webhook_mapping(payload, "repository")
    issue_payload = _github_webhook_mapping(payload, "issue")
    sender_payload = _github_webhook_mapping(payload, "sender")
    repository = _github_webhook_raw_string(repository_payload, "full_name")
    issue_url = _github_webhook_string(issue_payload, "html_url")
    issue_number_value = _github_webhook_positive_int(issue_payload, "number")
    if (
        issue_number_value is None
        or not _github_repository_full_name_is_valid(repository)
        or not issue_url.strip()
    ):
        return _every_code_github_webhook_invalid_payload_response(trace_id)

    request = EveryCodeWorkRequestCreateEnvelope(
        repository=repository,
        issue_number=issue_number_value,
        issue_url=issue_url,
        issue_title=_github_webhook_string(issue_payload, "title"),
        trigger_label=_EVERY_CODE_TRIGGER_LABEL,
        trigger_actor=_github_webhook_string(sender_payload, "login"),
        github_delivery_id=normalized_delivery_id,
        source="github_issue_label",
        queued_at=_utc_now_timestamp(),
    )
    record = build_every_code_work_request_record(request, queued_at=request.queued_at)
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
    accepted_payload["github_delivery_id"] = normalized_delivery_id
    return 202, accepted_payload


def _handle_every_code_issue_closed_webhook(
    *,
    trace_id: str,
    delivery_id: str,
    payload: dict[str, object],
    record_store: object,
) -> _EveryCodeWebhookResponse:
    repository_payload = _github_webhook_mapping(payload, "repository")
    issue_payload = _github_webhook_mapping(payload, "issue")
    repository = _github_webhook_raw_string(repository_payload, "full_name")
    issue_number_value = _github_webhook_positive_int(issue_payload, "number")
    if issue_number_value is None or not _github_repository_full_name_is_valid(repository):
        return _every_code_github_webhook_invalid_payload_response(trace_id)
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
        return 202, response_payload

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
    return 202, accepted_payload


def _handle_every_code_pull_request_webhook(
    *,
    trace_id: str,
    delivery_id: str,
    payload: dict[str, object],
    record_store: object,
) -> _EveryCodeWebhookResponse:
    if payload.get("action") != "closed":
        return (
            202,
            {
                "status": "accepted",
                "trace_id": trace_id,
                "skipped": True,
                "reason": "unsupported_action",
            },
        )

    repository_payload = _github_webhook_mapping(payload, "repository")
    pull_request_payload = _github_webhook_mapping(payload, "pull_request")
    repository = _github_webhook_raw_string(repository_payload, "full_name")
    if not _github_repository_full_name_is_valid(repository):
        return _every_code_github_webhook_invalid_payload_response(trace_id)
    pr_url = _github_webhook_raw_string(pull_request_payload, "html_url")
    if not pr_url.strip() or pr_url.strip() != pr_url:
        return _every_code_github_webhook_invalid_payload_response(trace_id)
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
        return (
            202,
            {
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
        return (
            202,
            {
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
    return 202, accepted_payload


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
    trace_id: str,
    delivery_id: str,
    payload: dict[str, object],
    record_store: object,
    control_plane_root_path: Path,
) -> _EveryCodeWebhookResponse | None:
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
            trace_id=trace_id,
            delivery_id=delivery_id,
        )
    every_code_store = _every_code_work_request_store(record_store)
    context_name = launchplane_anchor_repo_context(
        record_store=cast(ProductProfileListStore, record_store), repo=repo
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
    except click.ClickException:
        return (
            202,
            {
                "status": "accepted",
                "trace_id": trace_id,
                "skipped": True,
                "reason": "preview_validation_failed",
                "github_delivery_id": delivery_id,
            },
        )
    if not bool(result.get("handled")):
        return None
    accepted_payload = _accepted_payload(
        trace_id=trace_id,
        result={
            "preview_validation": {key: value for key, value in result.items() if key != "handled"}
        },
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
    return 202, accepted_payload


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
    trace_id: str,
    delivery_id: str,
    event_name: str,
    payload: dict[str, object],
    record_store: object,
) -> _EveryCodeWebhookResponse:
    if not _every_code_pr_feedback_action_supported(
        event_name=event_name,
        action=str(payload.get("action", "")),
    ):
        return (
            202,
            {
                "status": "accepted",
                "trace_id": trace_id,
                "skipped": True,
                "reason": "unsupported_action",
                "github_delivery_id": delivery_id,
            },
        )

    sender_payload = _github_webhook_mapping(payload, "sender")
    body_payload = _every_code_feedback_body_payload(event_name=event_name, payload=payload)
    if body_payload is None:
        return _every_code_github_webhook_invalid_payload_response(trace_id)
    if _every_code_feedback_actor_is_automation(
        sender_payload=sender_payload,
        body_payload=body_payload,
    ):
        return (
            202,
            {
                "status": "accepted",
                "trace_id": trace_id,
                "skipped": True,
                "reason": "automation_actor",
                "github_delivery_id": delivery_id,
            },
        )

    repository_payload = _github_webhook_mapping(payload, "repository")
    repository = _github_webhook_raw_string(repository_payload, "full_name")
    if not _github_repository_full_name_is_valid(repository):
        return _every_code_github_webhook_invalid_payload_response(trace_id)
    actor = _github_webhook_string(sender_payload, "login")
    if not _every_code_feedback_actor_is_trusted(repository=repository, actor=actor):
        return _every_code_untrusted_feedback_response(
            trace_id=trace_id,
            delivery_id=delivery_id,
        )
    pr_reference = _every_code_feedback_pr_reference(
        event_name=event_name,
        payload=payload,
        repository=repository,
    )
    if pr_reference is None:
        return _every_code_github_webhook_invalid_payload_response(trace_id)
    pr_number, pr_url = pr_reference
    body = _github_webhook_string(body_payload, "body")
    if not body.strip():
        return (
            202,
            {
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
        return (
            202,
            {
                "status": "accepted",
                "trace_id": trace_id,
                "skipped": True,
                "reason": "linked_every_code_request_not_found",
                "github_delivery_id": delivery_id,
            },
        )

    github_node_id = _github_webhook_string(body_payload, "node_id")
    github_id_value = body_payload.get("id")
    github_id = str(github_id_value) if github_id_value is not None else ""
    github_feedback_identity = github_node_id.strip() or github_id.strip()
    if not github_feedback_identity or not any(
        character.isalnum() for character in github_feedback_identity
    ):
        return _every_code_github_webhook_invalid_payload_response(trace_id)
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
            return (
                202,
                {
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
    return 202, accepted_payload


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
) -> dict[str, object] | None:
    key = "review" if event_name == "pull_request_review" else "comment"
    return _github_webhook_mapping(payload, key)


def _every_code_feedback_pr_reference(
    *,
    event_name: str,
    payload: dict[str, object],
    repository: str,
) -> tuple[int, str] | None:
    if event_name == "issue_comment":
        issue_payload = _github_webhook_mapping(payload, "issue")
        if issue_payload is None:
            return None
        pull_request_marker = issue_payload.get("pull_request")
        if not isinstance(pull_request_marker, dict):
            return None
        pr_number_value = _github_webhook_positive_int(issue_payload, "number")
    else:
        pull_request_payload = _github_webhook_mapping(payload, "pull_request")
        if pull_request_payload is None:
            return None
        pr_number_value = _github_webhook_positive_int(pull_request_payload, "number")
    if pr_number_value is None:
        return None
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
    if isinstance(identity, LocalOperatorIdentity):
        return f"local-operator:{identity.subject}"
    if isinstance(identity, LocalAdminIdentity):
        return f"local-admin:{identity.subject}"
    if isinstance(identity, TerminalAgentIdentity):
        return f"terminal-agent:{identity.subject}"
    return (
        f"github-actions:{identity.repository}:{identity.workflow_ref or identity.job_workflow_ref}"
    )


def _idempotency_scope(identity: LaunchplaneIdentity) -> str:
    if isinstance(identity, GitHubHumanIdentity):
        return "|".join(("github-human", identity.login, str(identity.github_id)))
    if isinstance(identity, LocalOperatorIdentity):
        return "|".join(("local-operator", identity.subject, identity.token_label))
    if isinstance(identity, LocalAdminIdentity):
        return "|".join(("local-admin", identity.subject, identity.token_label))
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
    if route_path not in _PREVIEW_DESTROY_IDEMPOTENCY_ROUTE_PATHS:
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
    extra_record_keys: frozenset[str] = frozenset(),
    replayed: bool = False,
    original_trace_id: str = "",
) -> dict[str, object]:
    serialized_driver_result: dict[str, object] | None = None
    if isinstance(driver_result, BaseModel):
        serialized_driver_result = driver_result.model_dump(mode="json")
    elif isinstance(driver_result, dict):
        serialized_driver_result = dict(driver_result)
    record_keys = {
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
        "provider_target_count",
        "provider_target_id_count",
        "runtime_environment_record_count",
        "secret_binding_count",
        "generation_id",
        "promotion_record_id",
        "target_id",
        "target_type",
        "image_reference",
        "artifact_id",
        "transition",
        "preview_state",
        "preview_generation_id",
        "verification_status",
        "verified_at",
        "generic_web_preview_verification",
        "request_id",
        "feedback_id",
        "state",
        "merge_train_batch_candidate_record_id",
        "merge_train_batch_landing_plan_record_id",
        "merge_train_stack_collapse_plan_record_id",
        "merge_train_run_id",
        "odoo_stable_bootstrap_operation_id",
        "odoo_stable_target_replacement_operation_id",
        "runner_host_hygiene_audit_record_key",
        "runner_lane_registration_audit_record_key",
        "generic_web_rollback_plan_id",
        "ingress_route_audit_record_id",
        "edge_endpoint_key",
        "ingress_canary_route_key",
    }
    accepted_record_keys = record_keys | extra_record_keys
    records: dict[str, object] = {}
    for key, value in result.items():
        if key not in accepted_record_keys:
            continue
        if key.endswith("_preview_verification") and isinstance(value, dict):
            records[key] = value
            continue
        records[key] = str(value)
    payload: dict[str, object] = {
        "status": "accepted",
        "trace_id": trace_id,
        "records": records,
        **({"result": serialized_driver_result} if serialized_driver_result else {}),
    }
    if replayed:
        payload["replayed"] = True
        payload["original_trace_id"] = original_trace_id
    return payload


def _accepted_payload_extra_record_keys(*, route_path: str) -> frozenset[str]:
    if route_path == "/v1/drivers/launchplane/self-deploy":
        return frozenset({"oauth_env_keys_removed"})
    if route_path == _ODOO_TARGET_REPLACEMENT_APPLY_ROUTE.route_path:
        return frozenset({"deployment_record_id", "release_tuple_id"})
    if route_path == _GENERIC_WEB_ROLLBACK_ROUTE.route_path:
        return frozenset({"rollback_status", "deploy_status", "post_deploy_status"})
    return frozenset()


def _driver_result_payload_for_idempotency_replay(
    *, route_path: str, driver_result: dict[str, object]
) -> dict[str, object]:
    replay_payload = dict(driver_result)
    if route_path in {
        _GENERIC_WEB_DEPLOY_ROUTE.route_path,
        _GENERIC_WEB_PROD_PROMOTION_ROUTE.route_path,
        _GENERIC_WEB_PROD_PROMOTION_WORKFLOW_ROUTE.route_path,
        _VERIREEL_TESTING_DEPLOY_ROUTE.route_path,
        _VERIREEL_PROD_DEPLOY_ROUTE.route_path,
        _VERIREEL_PROD_PROMOTION_ROUTE.route_path,
    }:
        replay_payload.pop("target_type", None)
    return replay_payload


def _operation_payload(
    operation: OdooStableBootstrapOperationRecord,
) -> dict[str, object]:
    payload = operation.model_dump(mode="json")
    payload["poll_url"] = _odoo_stable_bootstrap_operation_poll_url(operation.operation_id)
    return payload


def _odoo_stable_bootstrap_operation_poll_url(operation_id: str) -> str:
    return f"/v1/drivers/odoo/stable-bootstrap/operations/{operation_id.strip()}"


def _target_replacement_operation_payload(
    operation: OdooStableTargetReplacementOperationRecord,
) -> dict[str, object]:
    payload = operation.model_dump(mode="json")
    payload["poll_url"] = _odoo_stable_target_replacement_operation_poll_url(operation.operation_id)
    return payload


def _odoo_stable_target_replacement_operation_poll_url(operation_id: str) -> str:
    return f"/v1/drivers/odoo/target-replacement/operations/{operation_id.strip()}"


def _odoo_stable_bootstrap_operation_store(
    record_store: object,
) -> _OdooStableBootstrapOperationStore:
    required_methods = (
        "write_odoo_stable_bootstrap_operation_record",
        "create_odoo_stable_bootstrap_operation_record_if_no_active_lane",
        "read_odoo_stable_bootstrap_operation_record",
        "list_odoo_stable_bootstrap_operation_records",
    )
    if all(hasattr(record_store, method_name) for method_name in required_methods):
        return cast(_OdooStableBootstrapOperationStore, record_store)
    raise click.ClickException(
        "Odoo stable bootstrap operations require Launchplane operation-record storage."
    )


def _odoo_stable_target_replacement_operation_store(
    record_store: object,
) -> _OdooStableTargetReplacementOperationStore:
    required_methods = (
        "write_odoo_stable_target_replacement_operation_record",
        "create_odoo_stable_target_replacement_operation_record_if_no_active_lane",
        "read_odoo_stable_target_replacement_operation_record",
        "list_odoo_stable_target_replacement_operation_records",
    )
    if all(hasattr(record_store, method_name) for method_name in required_methods):
        return cast(_OdooStableTargetReplacementOperationStore, record_store)
    raise click.ClickException(
        "Odoo stable target replacement operations require Launchplane operation-record storage."
    )


def _query_string_value(query: dict[str, list[str]], key: str) -> str:
    return str((query.get(key) or [""])[0] or "").strip()


def _query_int_value(
    query: dict[str, list[str]],
    key: str,
    *,
    default: int | None = None,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int | None:
    raw_value = _query_string_value(query, key)
    if not raw_value:
        value = default
    else:
        value = int(raw_value)
    if value is None:
        return None
    if minimum is not None and value < minimum:
        raise ValueError(f"Query parameter {key} must be at least {minimum}")
    if maximum is not None and value > maximum:
        raise ValueError(f"Query parameter {key} must be at most {maximum}")
    return value


def _find_odoo_stable_bootstrap_operation_by_idempotency_key(
    *,
    operation_store: _OdooStableBootstrapOperationStore,
    product: str,
    context: str,
    instance: str,
    idempotency_key: str,
) -> OdooStableBootstrapOperationRecord | None:
    records = operation_store.list_odoo_stable_bootstrap_operation_records(
        product=product,
        context_name=context,
        instance_name=instance,
        idempotency_key=idempotency_key,
        limit=1,
    )
    return records[0] if records else None


def _find_odoo_stable_target_replacement_operation_by_idempotency_key(
    *,
    operation_store: _OdooStableTargetReplacementOperationStore,
    idempotency_key: str,
    idempotency_scope: str,
) -> OdooStableTargetReplacementOperationRecord | None:
    records = operation_store.list_odoo_stable_target_replacement_operation_records(
        idempotency_key=idempotency_key,
        idempotency_scope=idempotency_scope,
        limit=1,
    )
    return records[0] if records else None


def _build_odoo_stable_bootstrap_operation_record(
    *,
    bootstrap_request: OdooStableBootstrapRequest,
    idempotency_key: str,
    request_fingerprint: str,
    created_at: str,
) -> OdooStableBootstrapOperationRecord:
    return OdooStableBootstrapOperationRecord(
        operation_id=build_odoo_stable_bootstrap_operation_id(
            product=bootstrap_request.product,
            context=bootstrap_request.context,
            instance=bootstrap_request.instance,
            created_at=created_at,
            idempotency_key=idempotency_key,
        ),
        product=bootstrap_request.product,
        context=bootstrap_request.context,
        instance=bootstrap_request.instance,
        idempotency_key=idempotency_key,
        request_fingerprint=request_fingerprint,
        request=bootstrap_request,
        status="pending",
        phase="created",
        created_at=created_at,
        updated_at=created_at,
    )


def _build_odoo_stable_target_replacement_operation_record(
    *,
    replacement_request: OdooStableTargetReplacementApplyRequest,
    context: str,
    idempotency_key: str,
    idempotency_scope: str,
    request_fingerprint: str,
    created_at: str,
) -> OdooStableTargetReplacementOperationRecord:
    return OdooStableTargetReplacementOperationRecord(
        operation_id=build_odoo_stable_target_replacement_operation_id(
            product=replacement_request.product,
            context=context,
            instance=replacement_request.instance,
            created_at=created_at,
            idempotency_key=idempotency_key,
            idempotency_scope=idempotency_scope,
        ),
        product=replacement_request.product,
        context=context,
        instance=replacement_request.instance,
        idempotency_key=idempotency_key,
        idempotency_scope=idempotency_scope,
        request_fingerprint=request_fingerprint,
        request=replacement_request,
        status="pending",
        phase="created",
        created_at=created_at,
        updated_at=created_at,
    )


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
        driver_result=(
            _driver_result_payload_for_idempotency_replay(
                route_path=stored_record.route_path,
                driver_result=stored_driver_result,
            )
            if isinstance(stored_driver_result, dict)
            else None
        ),
        extra_record_keys=_accepted_payload_extra_record_keys(route_path=stored_record.route_path),
        replayed=True,
        original_trace_id=stored_record.response_trace_id,
    )
    return _json_response(
        start_response=start_response,
        status_code=stored_record.response_status_code,
        payload=result_payload,
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


def _deliver_preview_pr_feedback_notifications(
    *,
    record_store: object,
    feedback: PreviewPrFeedbackRecord,
    attempted_at: str,
    discord_sender: Callable[[str, dict[str, object]], object] = post_discord_webhook,
) -> tuple[PreviewPrFeedbackNotificationAttemptRecord, ...]:
    if feedback.delivery_status not in {"skipped", "failed"}:
        return ()
    notification_store = _preview_pr_feedback_notification_store(record_store)
    secret_store = _secret_capable_store(record_store)
    if notification_store is None or secret_store is None:
        return ()
    policies = tuple(
        policy
        for policy in notification_store.list_preview_pr_feedback_notification_policy_records(
            product=feedback.product,
            context_name=feedback.context,
            repository=feedback.repository,
            status="enabled",
            limit=None,
        )
        if policy.matches(feedback)
    )
    if not policies:
        return ()
    event = preview_pr_feedback_notification_event(feedback)
    secret_resolver = _launchplane_managed_secret_resolver(
        record_store=secret_store,
        context_name=_LAUNCHPLANE_SERVICE_CONTEXT,
        instance_name="preview-feedback",
    )
    attempts: list[PreviewPrFeedbackNotificationAttemptRecord] = []
    for policy in policies:
        for destination in policy.destinations:
            existing_attempt = _existing_preview_pr_feedback_notification_attempt(
                notification_store=notification_store,
                feedback=feedback,
                event=event,
                policy=policy,
                destination=destination,
            )
            if existing_attempt is not None and existing_attempt.delivery_status in {
                "pending",
                "delivered",
                "skipped",
            }:
                attempts.append(existing_attempt)
                continue
            if destination.status != "enabled":
                attempt = _write_preview_pr_feedback_notification_attempt(
                    notification_store=notification_store,
                    feedback=feedback,
                    event=event,
                    policy=policy,
                    destination=destination,
                    attempted_at=attempted_at,
                    delivery_status="skipped",
                    action="destination_disabled",
                )
                attempts.append(attempt)
                continue
            if destination.kind == "discord":
                attempt = _deliver_preview_pr_feedback_discord_notification(
                    notification_store=notification_store,
                    secret_resolver=secret_resolver,
                    discord_sender=discord_sender,
                    feedback=feedback,
                    event=event,
                    policy=policy,
                    destination=destination,
                    attempted_at=attempted_at,
                )
                attempts.append(attempt)
    return tuple(attempts)


def _deliver_preview_pr_feedback_discord_notification(
    *,
    notification_store: _PreviewPrFeedbackNotificationStore,
    secret_resolver: Callable[[str], str],
    discord_sender: Callable[[str, dict[str, object]], object],
    feedback: PreviewPrFeedbackRecord,
    event: PreviewPrFeedbackNotificationEvent,
    policy: PreviewPrFeedbackNotificationPolicyRecord,
    destination: PreviewPrFeedbackNotificationDestination,
    attempted_at: str,
) -> PreviewPrFeedbackNotificationAttemptRecord:
    webhook_url = secret_resolver(destination.discord_webhook_secret).strip()
    if not webhook_url:
        return _write_preview_pr_feedback_notification_attempt(
            notification_store=notification_store,
            feedback=feedback,
            event=event,
            policy=policy,
            destination=destination,
            attempted_at=attempted_at,
            delivery_status="failed",
            action="missing_discord_webhook",
            error_message="Discord webhook secret could not be resolved.",
        )
    public_url_error = public_discord_url_error(webhook_url)
    if public_url_error:
        return _write_preview_pr_feedback_notification_attempt(
            notification_store=notification_store,
            feedback=feedback,
            event=event,
            policy=policy,
            destination=destination,
            attempted_at=attempted_at,
            delivery_status="failed",
            action="invalid_discord_webhook",
            error_message=f"Discord webhook URL is not public: {public_url_error}",
        )
    pending_attempt = _write_preview_pr_feedback_notification_attempt(
        notification_store=notification_store,
        feedback=feedback,
        event=event,
        policy=policy,
        destination=destination,
        attempted_at=attempted_at,
        delivery_status="pending",
        action="dispatching_discord",
    )
    try:
        discord_sender(webhook_url, _preview_pr_feedback_discord_payload(feedback, event=event))
    except Exception as error:  # noqa: BLE001 - delivery attempts preserve provider failures.
        return _write_preview_pr_feedback_notification_attempt(
            notification_store=notification_store,
            feedback=feedback,
            event=event,
            policy=policy,
            destination=destination,
            attempted_at=attempted_at,
            delivery_status="failed",
            action="discord_webhook_failed",
            error_message=str(error) or error.__class__.__name__,
        )
    try:
        return _write_preview_pr_feedback_notification_attempt(
            notification_store=notification_store,
            feedback=feedback,
            event=event,
            policy=policy,
            destination=destination,
            attempted_at=attempted_at,
            delivery_status="delivered",
            action="posted_discord",
        )
    except Exception:  # noqa: BLE001 - the pending attempt preserves dispatch evidence.
        return pending_attempt


def _existing_preview_pr_feedback_notification_attempt(
    *,
    notification_store: _PreviewPrFeedbackNotificationStore,
    feedback: PreviewPrFeedbackRecord,
    event: PreviewPrFeedbackNotificationEvent,
    policy: PreviewPrFeedbackNotificationPolicyRecord,
    destination: PreviewPrFeedbackNotificationDestination,
) -> PreviewPrFeedbackNotificationAttemptRecord | None:
    attempt_id = build_preview_pr_feedback_notification_attempt_id(
        feedback_id=feedback.feedback_id,
        event=event,
        policy_id=policy.policy_id,
        destination_id=destination.destination_id,
    )
    return next(
        (
            attempt
            for attempt in notification_store.list_preview_pr_feedback_notification_attempt_records(
                feedback_id=feedback.feedback_id,
                event=event,
                limit=None,
            )
            if attempt.attempt_id == attempt_id
        ),
        None,
    )


def _write_preview_pr_feedback_notification_attempt(
    *,
    notification_store: _PreviewPrFeedbackNotificationStore,
    feedback: PreviewPrFeedbackRecord,
    event: PreviewPrFeedbackNotificationEvent,
    policy: PreviewPrFeedbackNotificationPolicyRecord,
    destination: PreviewPrFeedbackNotificationDestination,
    attempted_at: str,
    delivery_status: PreviewPrFeedbackNotificationDeliveryStatus,
    action: str,
    error_message: str = "",
) -> PreviewPrFeedbackNotificationAttemptRecord:
    attempt = PreviewPrFeedbackNotificationAttemptRecord(
        attempt_id=build_preview_pr_feedback_notification_attempt_id(
            feedback_id=feedback.feedback_id,
            event=event,
            policy_id=policy.policy_id,
            destination_id=destination.destination_id,
        ),
        feedback_id=feedback.feedback_id,
        event=event,
        policy_id=policy.policy_id,
        destination_id=destination.destination_id,
        destination_kind=destination.kind,
        delivery_status=delivery_status,
        attempted_at=attempted_at,
        action=action,
        error_message=_bounded_text(error_message, max_length=500),
    )
    notification_store.write_preview_pr_feedback_notification_attempt_record(attempt)
    return attempt


def _preview_pr_feedback_discord_payload(
    feedback: PreviewPrFeedbackRecord,
    *,
    event: PreviewPrFeedbackNotificationEvent,
) -> dict[str, object]:
    fields = [
        {"name": "Product", "value": feedback.product, "inline": True},
        {"name": "Context", "value": feedback.context, "inline": True},
        {"name": "Repository", "value": feedback.repository, "inline": True},
        {"name": "PR", "value": str(feedback.anchor_pr_number), "inline": True},
        {"name": "Delivery", "value": feedback.delivery_status, "inline": True},
        {"name": "Status", "value": feedback.status, "inline": True},
        {"name": "Feedback", "value": feedback.feedback_id, "inline": False},
    ]
    if feedback.anchor_pr_url:
        fields.append({"name": "Pull request", "value": feedback.anchor_pr_url, "inline": False})
    if feedback.run_url:
        fields.append({"name": "Workflow", "value": feedback.run_url, "inline": False})
    description = feedback.error_message or "Preview PR feedback could not be delivered."
    return {
        "embeds": [
            {
                "title": "Launchplane preview PR feedback delivery failed",
                "description": description,
                "color": 0xC62828,
                "fields": fields,
                "footer": {"text": event},
            }
        ]
    }


def _launchplane_managed_secret_resolver(
    *,
    record_store: control_plane_secrets.SecretReadStore,
    context_name: str,
    instance_name: str,
) -> Callable[[str], str]:
    def resolve(secret_id: str) -> str:
        normalized_secret_id = secret_id.strip()
        if not normalized_secret_id:
            return ""
        try:
            record = record_store.read_secret_record(normalized_secret_id)
        except Exception:  # noqa: BLE001 - notification attempts capture missing secrets.
            return ""
        if record.status != control_plane_secrets.SECRET_STATUS_CONFIGURED:
            return ""
        if not control_plane_secrets._scope_matches_record(
            record,
            context_name=context_name,
            instance_name=instance_name,
        ):
            return ""
        try:
            version = record_store.read_secret_version(record.current_version_id)
            return control_plane_secrets._decrypt_secret_value(version.ciphertext)
        except Exception:  # noqa: BLE001 - notification attempts capture unreadable secrets.
            return ""

    return resolve


def _bounded_text(value: str, *, max_length: int) -> str:
    normalized_value = " ".join(value.strip().split())
    if len(normalized_value) <= max_length:
        return normalized_value
    return f"{normalized_value[: max_length - 3]}..."


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
    if driver_result is None:
        return True
    if _driver_result_contains_status(driver_result, "blocked"):
        return False
    if _driver_result_has_completed_deploy_with_post_deploy_failure(driver_result):
        return True
    if _driver_result_contains_status(driver_result, "fail"):
        return False
    if path in _PENDING_RESULT_IDEMPOTENCY_SKIP_ROUTES:
        return not _driver_result_contains_status(driver_result, "pending")
    return True


def _driver_result_has_completed_deploy_with_post_deploy_failure(
    driver_result: BaseModel | dict[str, object] | object,
) -> bool:
    if isinstance(driver_result, BaseModel):
        result_payload = driver_result.model_dump(mode="json")
    elif isinstance(driver_result, dict):
        result_payload = driver_result
    elif hasattr(driver_result, "__dict__"):
        result_payload = vars(driver_result)
    else:
        return False
    return (
        result_payload.get("deploy_status") == "pass"
        and result_payload.get("post_deploy_status") == "fail"
    )


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


def _decode_json_request_body_or_none(body_bytes: bytes) -> dict[str, object] | None:
    try:
        payload = json.loads(body_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    return cast(dict[str, object], payload)


def _bearer_token(environ: dict[str, object]) -> str:
    return read_bearer_token(str(environ.get("HTTP_AUTHORIZATION", "")))


def _every_code_worker_token_from_env() -> str:
    return os.environ.get("LAUNCHPLANE_EVERY_CODE_WORKER_TOKEN", "").strip()


def _terminal_agent_read_token_from_env() -> str:
    return os.environ.get("LAUNCHPLANE_TERMINAL_AGENT_READ_TOKEN", "").strip()


def _terminal_agent_subject_from_env() -> str:
    return os.environ.get("LAUNCHPLANE_TERMINAL_AGENT_SUBJECT", "").strip()


def _terminal_agent_token_label_from_env() -> str:
    return os.environ.get("LAUNCHPLANE_TERMINAL_AGENT_TOKEN_LABEL", "").strip()


def _local_operator_token_from_env() -> str:
    return os.environ.get("LAUNCHPLANE_LOCAL_OPERATOR_TOKEN", "").strip()


def _local_operator_subject_from_env() -> str:
    return os.environ.get("LAUNCHPLANE_LOCAL_OPERATOR_SUBJECT", "").strip()


def _local_operator_token_label_from_env() -> str:
    return os.environ.get("LAUNCHPLANE_LOCAL_OPERATOR_TOKEN_LABEL", "").strip()


def _local_admin_token_from_env() -> str:
    return os.environ.get("LAUNCHPLANE_LOCAL_ADMIN_TOKEN", "").strip()


def _local_admin_subject_from_env() -> str:
    return os.environ.get("LAUNCHPLANE_LOCAL_ADMIN_SUBJECT", "").strip()


def _local_admin_token_label_from_env() -> str:
    return os.environ.get("LAUNCHPLANE_LOCAL_ADMIN_TOKEN_LABEL", "").strip()


def _bearer_identity_config_from_env() -> BearerIdentityConfig:
    return BearerIdentityConfig(
        every_code_worker_token=_every_code_worker_token_from_env(),
        local_admin_token=_local_admin_token_from_env(),
        local_admin_subject=_local_admin_subject_from_env(),
        local_admin_token_label=_local_admin_token_label_from_env(),
        local_operator_token=_local_operator_token_from_env(),
        local_operator_subject=_local_operator_subject_from_env(),
        local_operator_token_label=_local_operator_token_label_from_env(),
        terminal_agent_token=_terminal_agent_read_token_from_env(),
        terminal_agent_subject=_terminal_agent_subject_from_env(),
        terminal_agent_token_label=_terminal_agent_token_label_from_env(),
    )


def _owner_agent_identity_from_bearer(environ: dict[str, object]) -> LaunchplaneIdentity | None:
    try:
        provided_token = _bearer_token(environ)
    except PermissionError:
        return None
    return bearer_identity_from_token(
        token=provided_token,
        config=_bearer_identity_config_from_env(),
    )


def _session(
    *,
    environ: dict[str, object],
    session_manager: HumanSessionManager | None,
    on_renewed_session: Callable[[LaunchplaneHumanSession], None] | None = None,
) -> LaunchplaneHumanSession | None:
    if session_manager is None:
        return None
    session = session_manager.read_cookie(str(environ.get("HTTP_COOKIE", "")))
    if session is None:
        return None
    renewed_session = session_manager.renew_if_needed(session)
    if (
        renewed_session is not None
        and renewed_session.expires_at != session.expires_at
        and on_renewed_session is not None
    ):
        on_renewed_session(renewed_session)
    return renewed_session


def _session_identity(
    *,
    environ: dict[str, object],
    session_manager: HumanSessionManager | None,
    on_renewed_session: Callable[[LaunchplaneHumanSession], None] | None = None,
) -> GitHubHumanIdentity | None:
    session = _session(
        environ=environ,
        session_manager=session_manager,
        on_renewed_session=on_renewed_session,
    )
    return session.identity if session is not None else None


def _read_identity(
    *,
    environ: dict[str, object],
    verifier: TokenVerifier,
    session_manager: HumanSessionManager | None,
    on_renewed_session: Callable[[LaunchplaneHumanSession], None] | None = None,
) -> LaunchplaneIdentity:
    human_identity = _session_identity(
        environ=environ,
        session_manager=session_manager,
        on_renewed_session=on_renewed_session,
    )
    if human_identity is not None:
        return human_identity
    owner_agent_identity = _owner_agent_identity_from_bearer(environ)
    if owner_agent_identity is not None:
        return owner_agent_identity
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
    elif isinstance(identity, LocalOperatorIdentity):
        identity_payload = {
            "type": "local_operator",
            "subject": identity.subject,
            "token_label": identity.token_label,
        }
    elif isinstance(identity, LocalAdminIdentity):
        identity_payload = {
            "type": "local_admin",
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


def _product_driver_compatible(
    *, profile: LaunchplaneProductProfileRecord, expected_driver_id: str
) -> bool:
    expected = expected_driver_id.strip()
    profile_driver_id = profile.driver_id.strip()
    if profile_driver_id == expected:
        return True
    try:
        descriptor = read_driver_descriptor(profile_driver_id)
    except FileNotFoundError:
        return False
    return descriptor.base_driver_id == expected


def _product_driver_route_compatible(
    *, profile: LaunchplaneProductProfileRecord, expected_driver_id: str, route_path: str
) -> bool:
    if profile.driver_id.strip() == expected_driver_id.strip():
        return True
    return route_path in _GENERIC_WEB_BASE_DRIVER_ROUTE_PATHS and _product_driver_compatible(
        profile=profile,
        expected_driver_id=expected_driver_id,
    )


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
    route_path: str = "",
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
    try:
        profile = cast(LaunchplaneProductProfileRecord, read_profile(normalized_product))
    except FileNotFoundError as error:
        raise DriverRouteDependencyNotFoundError from error
    if not _product_driver_route_compatible(
        profile=profile,
        expected_driver_id=normalized_driver_id,
        route_path=route_path,
    ):
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
        route_path=route_path,
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
    resolved_policy = resolve_launchplane_authz_policy(
        record_store=record_store,
        bootstrap_policy=bootstrap_policy,
        policy_source=_bootstrap_policy_source_from_env(),
        now_timestamp=_now_timestamp(),
    )
    return (
        resolved_policy.policy,
        resolved_policy.policy_sha256,
        resolved_policy.source,
    )


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


def _preview_lifecycle_cleanup_driver_id(profile: LaunchplaneProductProfileRecord) -> str:
    if profile.driver_id == "verireel":
        return "verireel"
    try:
        generic_web_compatible = _product_driver_compatible(
            profile=profile,
            expected_driver_id="generic-web",
        )
    except FileNotFoundError:
        generic_web_compatible = False
    if generic_web_compatible:
        return "generic-web"
    return ""


def _preview_lifecycle_sweep_profiles(
    *, record_store: object, product: str = ""
) -> tuple[LaunchplaneProductProfileRecord, ...]:
    if not hasattr(record_store, "list_product_profile_records"):
        return ()
    records = getattr(record_store, "list_product_profile_records")()
    requested_product = product.strip()
    profiles: list[LaunchplaneProductProfileRecord] = []
    for record in records:
        profile = LaunchplaneProductProfileRecord.model_validate(record)
        if requested_product and profile.product != requested_product:
            continue
        if not profile.preview.enabled:
            continue
        if not profile.preview.context.strip():
            continue
        profiles.append(profile)
    return tuple(sorted(profiles, key=lambda profile: profile.product))


def _build_preview_lifecycle_sweep(
    *,
    control_plane_root: Path,
    record_store: object,
    request: PreviewLifecycleSweepEnvelope,
) -> dict[str, object]:
    profiles = _preview_lifecycle_sweep_profiles(
        record_store=record_store,
        product=request.product,
    )
    entries: list[dict[str, object]] = []
    for profile in profiles:
        cleanup_driver_id = _preview_lifecycle_cleanup_driver_id(profile)
        entry: dict[str, object] = {
            "product": profile.product,
            "context": profile.preview.context,
            "driver_id": profile.driver_id,
            "cleanup_driver_id": cleanup_driver_id,
        }
        if not cleanup_driver_id:
            entry.update(
                {
                    "status": "skipped",
                    "error_message": (
                        "Preview lifecycle cleanup is not implemented for this product driver."
                    ),
                }
            )
            entries.append(entry)
            continue
        if cleanup_driver_id == "verireel":
            verireel_inventory_result = execute_verireel_preview_inventory(
                control_plane_root=control_plane_root,
                request=VeriReelPreviewInventoryRequest(context=profile.preview.context),
            )
            inventory_context = verireel_inventory_result.context
            inventory_source = request.source
            inventory_slugs = tuple(item.previewSlug for item in verireel_inventory_result.previews)
        else:
            generic_web_inventory_result = execute_generic_web_preview_inventory(
                control_plane_root=control_plane_root,
                record_store=cast(GenericWebPreviewProfileStore, record_store),
                request=GenericWebPreviewInventoryRequest(
                    product=profile.product,
                    source=request.source,
                ),
                profile=profile,
            )
            inventory_context = generic_web_inventory_result.context
            inventory_source = generic_web_inventory_result.source
            inventory_slugs = tuple(
                item.previewSlug for item in generic_web_inventory_result.previews
            )
        inventory_scan_id = _write_preview_inventory_scan_if_supported(
            record_store=record_store,
            context=inventory_context,
            source=inventory_source,
            preview_slugs=inventory_slugs,
        )
        desired_state = discover_generic_web_preview_desired_state(
            control_plane_root=control_plane_root,
            record_store=cast(GenericWebPreviewProfileStore, record_store),
            request=GenericWebPreviewDesiredStateRequest(
                product=profile.product,
                source=request.source,
                label=profile.preview.enable_label,
                max_pages=request.max_pages,
            ),
            discovered_at=_utc_now_timestamp(),
            profile=profile,
        )
        desired_state_id = _write_preview_desired_state_if_supported(
            record_store=record_store,
            record=desired_state,
        )
        lifecycle_plan = build_preview_lifecycle_plan(
            product=profile.product,
            context=profile.preview.context,
            planned_at=_utc_now_timestamp(),
            source=request.source,
            desired_previews=desired_state.desired_previews,
            desired_state_id=desired_state_id,
            latest_inventory_scan=_latest_preview_inventory_scan(
                record_store=record_store,
                context_name=profile.preview.context,
            ),
        )
        lifecycle_plan_id = _write_preview_lifecycle_plan_if_supported(
            record_store=record_store,
            record=lifecycle_plan,
        )
        cleanup_record = build_preview_lifecycle_cleanup_record(
            plan=lifecycle_plan,
            requested_at=_utc_now_timestamp(),
            source=request.source,
            apply=request.apply,
            destroy_reason=request.destroy_reason,
            control_plane_root=control_plane_root,
            record_store=record_store,
            timeout_seconds=request.timeout_seconds,
            driver_id=cleanup_driver_id,
            preview_slug_template=profile.preview.slug_template,
        )
        cleanup_id = _write_preview_lifecycle_cleanup_if_supported(
            record_store=record_store,
            record=cleanup_record,
        )
        entry.update(
            {
                "status": cleanup_record.status,
                "preview_inventory_scan_id": inventory_scan_id,
                "preview_desired_state_id": desired_state_id,
                "preview_lifecycle_plan_id": lifecycle_plan_id,
                "preview_lifecycle_cleanup_id": cleanup_id,
                "desired_count": desired_state.desired_count,
                "actual_count": len(lifecycle_plan.actual_slugs),
                "orphaned_slugs": lifecycle_plan.orphaned_slugs,
                "missing_slugs": lifecycle_plan.missing_slugs,
                "destroyed_slugs": cleanup_record.destroyed_slugs,
                "failed_slugs": cleanup_record.failed_slugs,
                "blocked_slugs": cleanup_record.blocked_slugs,
                "error_message": cleanup_record.error_message,
            }
        )
        entries.append(entry)
    status = "pass"
    for entry in entries:
        if entry.get("status") in {"fail", "blocked"}:
            status = "fail"
            break
        if entry.get("status") == "skipped" and status != "fail":
            status = "partial"
    return {
        "status": status,
        "apply": request.apply,
        "profile_count": len(entries),
        "profiles": entries,
    }


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


def _verireel_preview_manifest_fingerprint(request: VeriReelPreviewRefreshRequest) -> str:
    normalized_sha = request.anchor_head_sha.strip().lower()
    short_sha = normalized_sha[:7]
    return (
        f"{_repo_token(request.anchor_repo)}-preview-manifest-"
        f"{request.preview_slug.strip()}-{short_sha}"
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
    refresh_passed = driver_result.refresh_status == "pass"
    failure_summary = driver_result.error_message.strip() or "Preview provisioning failed."
    preview_url = driver_result.preview_url.strip() or request.preview_url.strip()
    if not preview_url and not refresh_passed:
        preview_url = _verireel_preview_url_for_failed_records(request=request)
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
    return apply_launchplane_generation_evidence(
        control_plane_root_path=control_plane_root_path,
        record_store=cast(LaunchplaneMutationStore, record_store),
        preview_request=preview_request,
        generation_request=generation_request,
    )


def _verireel_preview_url_for_failed_records(*, request: VeriReelPreviewRefreshRequest) -> str:
    return f"https://{request.preview_slug}.preview-config-missing.launchplane.invalid"


def _apply_verireel_preview_destroy_records(
    *,
    record_store: object,
    request: VeriReelPreviewDestroyRequest,
    driver_result: VeriReelPreviewDestroyResult,
) -> dict[str, object]:
    if driver_result.destroy_status != "pass":
        return {"transition": "destroy_failed"}
    try:
        return apply_launchplane_destroy_preview(
            record_store=cast(LaunchplaneMutationStore, record_store),
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
    generic_request = GenericWebPreviewVerificationRequest(
        context=request.context,
        anchor_repo=request.anchor_repo,
        anchor_pr_number=request.anchor_pr_number,
        verification_status=cast(ReleaseStatus, request.verification_status),
        verified_at=request.verified_at,
        failure_summary=request.failure_summary,
    )
    return _apply_generic_web_preview_verification_records(
        control_plane_root_path=control_plane_root_path,
        record_store=record_store,
        request=generic_request,
        result_key="verireel_preview_verification",
        default_failure_summary="Preview E2E verification failed.",
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
    evidence_store = cast(EvidenceIngestionStore, record_store)
    try:
        deployment_record = evidence_store.read_deployment_record(request.deployment_record_id)
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
        record_store=evidence_store,
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
    local_record_store_for_tests: _TestLaunchplaneServiceRecordStore | None = None,
    github_oauth_config: GitHubOAuthConfig | None = None,
    github_oauth_client: GitHubOAuthClient | None = None,
    human_session_store: HumanSessionStore | None = None,
    preview_pr_feedback_discord_sender: Callable[
        [str, dict[str, object]], object
    ] = post_discord_webhook,
    authz_policy_runtime: LaunchplaneAuthzPolicyRuntime | None = None,
    record_store_for_service: _TestLaunchplaneServiceRecordStore | None = None,
) -> _WsgiApp:
    resolved_root = control_plane_root_path or control_plane_root()
    ui_static_root = resolved_root / "control_plane" / "ui_static"
    record_store = cast(
        PostgresRecordStore,
        record_store_for_service
        or local_record_store_for_tests
        or build_shared_record_store(database_url=database_url),
    )
    authz_policy, resolved_authz_policy_sha256, resolved_authz_policy_source = (
        _resolve_authz_policy(record_store=record_store, bootstrap_policy=authz_policy)
    )
    resolved_authz_policy_runtime = authz_policy_runtime or LaunchplaneAuthzPolicyRuntime(
        authz_policy,
        policy_sha256=resolved_authz_policy_sha256,
        source=resolved_authz_policy_source,
    )
    resolved_authz_policy_runtime.update(
        authz_policy,
        policy_sha256=resolved_authz_policy_sha256,
        source=resolved_authz_policy_source,
    )
    resolved_github_oauth_config = github_oauth_config or load_github_oauth_config_from_env()
    session_store = (
        human_session_store
        or _human_session_capable_store(record_store)
        or InMemoryHumanSessionStore()
    )
    oauth_login_states = OAuthLoginStateStore()
    session_manager = (
        HumanSessionManager(
            config=resolved_github_oauth_config,
            session_store=session_store,
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
    descriptor_driver_dispatch_routes = _descriptor_driver_dispatch_routes()
    _validate_descriptor_driver_dispatch_routes(descriptor_driver_dispatch_routes)
    write_routes = _build_write_routes()

    def app(
        environ: dict[str, object],
        start_response: _StartResponse,
    ) -> list[bytes]:
        nonlocal authz_policy, resolved_authz_policy_sha256, resolved_authz_policy_source

        renewed_session: LaunchplaneHumanSession | None = None

        def record_renewed_session(session: LaunchplaneHumanSession) -> None:
            nonlocal renewed_session
            renewed_session = session

        original_start_response = start_response

        def start_response_with_session_cookie(status: str, headers: list[tuple[str, str]]) -> None:
            response_headers = list(headers)
            if session_manager is not None and renewed_session is not None:
                response_headers.append(
                    (
                        "Set-Cookie",
                        session_manager.session_cookie_header(renewed_session),
                    )
                )
            original_start_response(status, response_headers)

        start_response = start_response_with_session_cookie
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
            auth_session = _session(
                environ=environ,
                session_manager=session_manager,
                on_renewed_session=record_renewed_session,
            )
            if auth_session is None:
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
                    "identity": _human_identity_payload(auth_session.identity),
                },
            )
        if method == "GET" and (path == "/" or path == "/ui" or path.startswith("/ui/")):
            return serve_ui_route(
                start_response=start_response,
                trace_id=request_trace_id,
                path=path,
                ui_static_root=ui_static_root,
                json_response=_json_response,
                http_status_text=_http_status_text,
            )
        if path not in write_routes:
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
        if method == "GET":
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
        try:
            if method == "GET":
                identity = _read_identity(
                    environ=environ,
                    verifier=verifier,
                    session_manager=session_manager,
                    on_renewed_session=record_renewed_session,
                )
            else:
                if path in _HUMAN_IDENTITY_MUTATION_ROUTES:
                    identity = _read_identity(
                        environ=environ,
                        verifier=verifier,
                        session_manager=session_manager,
                        on_renewed_session=record_renewed_session,
                    )
                else:
                    owner_agent_identity = _owner_agent_identity_from_bearer(environ)
                    if isinstance(
                        owner_agent_identity,
                        LocalAdminIdentity | LocalOperatorIdentity,
                    ):
                        identity = owner_agent_identity
                    else:
                        token = _bearer_token(environ)
                        identity = verifier.verify(token)
                        if not isinstance(identity, GitHubActionsIdentity):
                            raise PermissionError("Mutation routes require GitHub Actions OIDC.")
            if isinstance(identity, TerminalAgentIdentity) and method != "GET":
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
            payload = _read_json_request(environ)
            request_idempotency_key = _idempotency_key(environ)
            request_scope = _idempotency_scope(identity)
            request_fingerprint = _idempotency_request_fingerprint(route_path=path, payload=payload)
            effective_idempotency_route_path = path
            driver_result: BaseModel | dict[str, object] | None = None
            result: dict[str, object] = {}
            if path == _MERGE_TRAIN_RUN_ONCE_ROUTE:
                merge_train_request = MergeTrainRunOnceEnvelope.model_validate(payload)
                policy_record = resolve_merge_train_policy_record(record_store)
                policy = policy_record.policy
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
                dry_run_result = build_merge_train_dry_run_result(policy=policy, snapshot=snapshot)
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
                    policy_sha256=policy_record.policy_sha256,
                    snapshot=snapshot,
                    dry_run_result=dry_run_result,
                    worker_step_result=worker_step_result,
                )
                record_store.write_merge_train_run_record(run_record)
                result["merge_train_run_id"] = run_record.run_id
            elif path == _MERGE_TRAIN_PR_FEEDBACK_ROUTE:
                feedback_request = MergeTrainPrFeedbackEnvelope.model_validate(payload)
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
                policy_record = resolve_merge_train_policy_record(record_store)
                policy = policy_record.policy
                repository_policy = policy.find_repository_policy(
                    repository=feedback_request.repository,
                    base_branch=feedback_request.base_branch,
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
                                "message": "Workflow cannot write merge train PR feedback.",
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
                feedback_store = _merge_train_pr_feedback_record_store(record_store)
                feedback_record = _build_merge_train_pr_feedback_record(
                    request=feedback_request,
                    policy_key=repository_policy.policy_key,
                    policy_sha256=policy_record.policy_sha256,
                    token=token,
                    recorded_at=_utc_now_timestamp(),
                    response_trace_id=request_trace_id,
                )
                feedback_store.write_merge_train_pr_feedback_record(feedback_record)
                if feedback_record.delivery_status == "failed":
                    return _json_response(
                        start_response=start_response,
                        status_code=502,
                        payload={
                            "status": "rejected",
                            "trace_id": request_trace_id,
                            "error": {
                                "code": "github_comment_delivery_failed",
                                "message": feedback_record.error_message
                                or "Merge train PR feedback comment delivery failed.",
                            },
                            "records": {
                                "merge_train_pr_feedback_id": feedback_record.feedback_id,
                            },
                            "result": {
                                "feedback": feedback_record.model_dump(mode="json"),
                            },
                        },
                    )
                result = {"feedback": feedback_record.model_dump(mode="json")}
                driver_result = {"feedback": feedback_record.model_dump(mode="json")}
            elif path == _MERGE_TRAIN_BATCH_CANDIDATE_RUN_ONCE_ROUTE:
                batch_request = MergeTrainBatchCandidateRunOnceEnvelope.model_validate(payload)
                policy_record = resolve_merge_train_policy_record(record_store)
                policy = policy_record.policy
                repository_policy = policy.find_repository_policy(
                    repository=batch_request.repository,
                    base_branch=batch_request.base_branch,
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
                batch_store = _merge_train_batch_candidate_record_store(record_store)
                stack_collapse_store = _merge_train_stack_collapse_plan_record_store(record_store)
                transport = UrllibMergeTrainGitHubTransport(
                    token=token,
                    api_base_url=batch_request.github_api_base_url,
                )
                github_client = GitHubMergeTrainClient(transport=transport)
                recorded_at = _utc_now_timestamp()
                candidate: MergeTrainBatchCandidate | None = None
                if batch_request.mode == "plan":
                    snapshot = GitHubMergeTrainSnapshotReader(
                        transport=transport
                    ).read_merge_train_snapshot(
                        repository=batch_request.repository,
                        base_branch=batch_request.base_branch,
                    )
                    dry_run_result = build_merge_train_dry_run_result(
                        policy=policy, snapshot=snapshot
                    )
                    selected_pr = dry_run_result.selected_pr
                    if selected_pr is not None and _merge_train_snapshot_has_stack_topology(
                        snapshot=snapshot, dry_run_result=dry_run_result
                    ):
                        stack_discovery = discover_merge_train_stack(
                            snapshot=snapshot,
                            root_pull_request_number=selected_pr.number,
                        )
                    else:
                        stack_discovery = None
                    if (
                        stack_discovery is not None
                        and stack_discovery.status == "ready_for_collapse"
                    ):
                        stack_collapse_plan = build_merge_train_stack_collapse_plan(
                            discovery_result=stack_discovery,
                            policy_key=dry_run_result.policy_key,
                            policy_sha256=policy_record.policy_sha256,
                            created_at=recorded_at,
                        )
                        stack_collapse_record = build_merge_train_stack_collapse_plan_record(
                            plan=stack_collapse_plan,
                            source=f"service:{batch_request.mode}:{request_trace_id}",
                            updated_at=recorded_at,
                        )
                        stack_collapse_store.write_merge_train_stack_collapse_plan_record(
                            stack_collapse_record
                        )
                        result = {
                            "merge_train_stack_collapse_plan_record_id": stack_collapse_record.record_id,
                            "repository": stack_collapse_plan.repository,
                            "base_branch": stack_collapse_plan.base_branch,
                            "mode": batch_request.mode,
                            "stack_collapse_plan": stack_collapse_plan.model_dump(mode="json"),
                        }
                        driver_result = {
                            "mode": batch_request.mode,
                            "dry_run_result": dry_run_result.model_dump(mode="json"),
                            "stack_discovery": stack_discovery.model_dump(mode="json"),
                            "stack_collapse_plan": stack_collapse_plan.model_dump(mode="json"),
                        }
                    elif stack_discovery is not None and stack_discovery.status == "unsupported":
                        result = {
                            "repository": batch_request.repository,
                            "base_branch": batch_request.base_branch,
                            "mode": batch_request.mode,
                        }
                        driver_result = {
                            "mode": batch_request.mode,
                            "dry_run_result": dry_run_result.model_dump(mode="json"),
                            "stack_discovery": stack_discovery.model_dump(mode="json"),
                            "next_action": "stack_unsupported",
                        }
                    else:
                        candidate = build_merge_train_batch_candidate(
                            dry_run_result=dry_run_result,
                            base_sha=snapshot.base_sha,
                            policy_sha256=policy_record.policy_sha256,
                            created_at=recorded_at,
                        )
                        driver_result = {
                            "mode": batch_request.mode,
                            "dry_run_result": dry_run_result.model_dump(mode="json"),
                            "candidate": candidate.model_dump(mode="json"),
                        }
                else:
                    existing_record = _read_merge_train_batch_candidate_record(
                        record_store=batch_store,
                        repository=batch_request.repository,
                        base_branch=batch_request.base_branch,
                        record_id=batch_request.candidate_record_id,
                    )
                    candidate = existing_record.candidate
                    if batch_request.mode == "build":
                        candidate = github_client.build_batch_candidate(candidate=candidate)
                    else:
                        candidate = github_client.observe_batch_candidate_checks(
                            candidate=candidate
                        )
                    driver_result = {
                        "mode": batch_request.mode,
                        "candidate": candidate.model_dump(mode="json"),
                    }
                if candidate is not None:
                    candidate_record = build_merge_train_batch_candidate_record(
                        candidate=candidate,
                        source=f"service:{batch_request.mode}:{request_trace_id}",
                        updated_at=recorded_at,
                    )
                    batch_store.write_merge_train_batch_candidate_record(candidate_record)
                    result = {
                        "merge_train_batch_candidate_record_id": candidate_record.record_id,
                        "repository": candidate.repository,
                        "base_branch": candidate.base_branch,
                        "mode": batch_request.mode,
                        "candidate": candidate.model_dump(mode="json"),
                    }
            elif path == _MERGE_TRAIN_BATCH_LANDING_RUN_ONCE_ROUTE:
                landing_request = MergeTrainBatchLandingRunOnceEnvelope.model_validate(payload)
                policy_record = resolve_merge_train_policy_record(record_store)
                policy = policy_record.policy
                repository_policy = policy.find_repository_policy(
                    repository=landing_request.repository,
                    base_branch=landing_request.base_branch,
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
                candidate_store = _merge_train_batch_candidate_record_store(record_store)
                landing_store = _merge_train_batch_landing_plan_record_store(record_store)
                collapse_store = _merge_train_stack_collapse_plan_record_store(record_store)
                recorded_at = _utc_now_timestamp()
                collapse_record: MergeTrainStackCollapsePlanRecord | None = None
                reconciled_collapse_plan: MergeTrainStackCollapsePlan | None = None
                candidate_ref_cleanup_result: dict[str, object] = {}
                if landing_request.mode == "plan":
                    candidate_record = _read_merge_train_batch_candidate_record(
                        record_store=candidate_store,
                        repository=landing_request.repository,
                        base_branch=landing_request.base_branch,
                        record_id=landing_request.candidate_record_id,
                    )
                    landing_plan = build_merge_train_batch_landing_plan(
                        candidate=candidate_record.candidate,
                        merge_method=repository_policy.merge_method,
                        created_at=recorded_at,
                    )
                else:
                    landing_record = _read_merge_train_batch_landing_plan_record(
                        record_store=landing_store,
                        repository=landing_request.repository,
                        base_branch=landing_request.base_branch,
                        record_id=landing_request.landing_plan_record_id,
                    )
                    collapse_existing_record: MergeTrainStackCollapsePlanRecord | None = None
                    if landing_request.stack_collapse_plan_record_id:
                        collapse_existing_record = _read_merge_train_stack_collapse_plan_record(
                            record_store=collapse_store,
                            repository=landing_request.repository,
                            base_branch=landing_request.base_branch,
                            record_id=landing_request.stack_collapse_plan_record_id,
                        )
                        _validate_stack_collapse_record_for_landing(
                            collapse_record=collapse_existing_record,
                            landing_plan=landing_record.landing_plan,
                            policy_sha256=policy_record.policy_sha256,
                        )
                        if not repository_policy.stack_child_disposition_label:
                            raise ValueError(
                                "merge train stack child disposition requires stack_child_disposition_label policy"
                            )
                    transport = UrllibMergeTrainGitHubTransport(
                        token=token,
                        api_base_url=landing_request.github_api_base_url,
                    )
                    github_client = GitHubMergeTrainClient(transport=transport)
                    landing_plan = github_client.land_batch_candidate(
                        landing_plan=landing_record.landing_plan
                    )
                    landing_record = build_merge_train_batch_landing_plan_record(
                        landing_plan=landing_plan,
                        source=f"service:{landing_request.mode}:{request_trace_id}",
                        updated_at=recorded_at,
                    )
                    landing_store.write_merge_train_batch_landing_plan_record(landing_record)
                    candidate_ref_cleanup_result = _cleanup_merge_train_batch_candidate_ref(
                        github_client=github_client,
                        landing_plan=landing_plan,
                        request_trace_id=request_trace_id,
                    )
                    if collapse_existing_record is not None:
                        root_entry = next(
                            (
                                entry
                                for entry in landing_plan.entries
                                if entry.pull_request_number
                                == collapse_existing_record.plan.root_pull_request_number
                            ),
                            None,
                        )
                        if root_entry is None or root_entry.status != "merged":
                            raise ValueError(
                                "merge train stack child disposition requires merged root PR"
                            )
                        reconciled_collapse_plan = (
                            reconcile_merge_train_stack_children_after_root_landing(
                                plan=collapse_existing_record.plan,
                                disposition_client=github_client,
                                root_merge_commit_sha=root_entry.merge_commit_sha,
                                label=repository_policy.stack_child_disposition_label,
                                updated_at=recorded_at,
                            )
                        )
                        collapse_record = build_merge_train_stack_collapse_plan_record(
                            plan=reconciled_collapse_plan,
                            source=f"service:child-disposition:{request_trace_id}",
                            updated_at=recorded_at,
                        )
                        collapse_store.write_merge_train_stack_collapse_plan_record(collapse_record)
                if landing_request.mode == "plan":
                    landing_record = build_merge_train_batch_landing_plan_record(
                        landing_plan=landing_plan,
                        source=f"service:{landing_request.mode}:{request_trace_id}",
                        updated_at=recorded_at,
                    )
                    landing_store.write_merge_train_batch_landing_plan_record(landing_record)
                result = {
                    "merge_train_batch_landing_plan_record_id": landing_record.record_id,
                    "repository": landing_plan.repository,
                    "base_branch": landing_plan.base_branch,
                    "mode": landing_request.mode,
                    "landing_plan": landing_plan.model_dump(mode="json"),
                }
                if landing_request.mode == "land" and landing_request.stack_collapse_plan_record_id:
                    if collapse_record is None or reconciled_collapse_plan is None:
                        raise ValueError("merge train stack child disposition record missing")
                    result["merge_train_stack_collapse_plan_record_id"] = collapse_record.record_id
                    result["stack_collapse_plan"] = reconciled_collapse_plan.model_dump(mode="json")
                driver_result = {
                    "mode": landing_request.mode,
                    "landing_plan": result["landing_plan"],
                }
                if landing_request.mode == "land":
                    result.update(candidate_ref_cleanup_result)
                    driver_result.update(candidate_ref_cleanup_result)
                if "stack_collapse_plan" in result:
                    driver_result["stack_collapse_plan"] = result["stack_collapse_plan"]
            elif path == _MERGE_TRAIN_STACK_COLLAPSE_RUN_ONCE_ROUTE:
                collapse_request = MergeTrainStackCollapseRunOnceEnvelope.model_validate(payload)
                policy_record = resolve_merge_train_policy_record(record_store)
                policy = policy_record.policy
                repository_policy = policy.find_repository_policy(
                    repository=collapse_request.repository,
                    base_branch=collapse_request.base_branch,
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
                collapse_store = _merge_train_stack_collapse_plan_record_store(record_store)
                collapse_existing_record = _read_merge_train_stack_collapse_plan_record(
                    record_store=collapse_store,
                    repository=collapse_request.repository,
                    base_branch=collapse_request.base_branch,
                    record_id=collapse_request.stack_collapse_plan_record_id,
                )
                recorded_at = _utc_now_timestamp()
                transport = UrllibMergeTrainGitHubTransport(
                    token=token,
                    api_base_url=collapse_request.github_api_base_url,
                )
                if collapse_request.mode == "execute":
                    executed_plan = execute_merge_train_stack_collapse_plan(
                        plan=collapse_existing_record.plan,
                        branch_client=GitHubMergeTrainClient(transport=transport),
                        updated_at=recorded_at,
                    )
                    collapse_record = build_merge_train_stack_collapse_plan_record(
                        plan=executed_plan,
                        source=f"service:execute:{request_trace_id}",
                        updated_at=recorded_at,
                    )
                    collapse_store.write_merge_train_stack_collapse_plan_record(collapse_record)
                    result = {
                        "merge_train_stack_collapse_plan_record_id": collapse_record.record_id,
                        "repository": executed_plan.repository,
                        "base_branch": executed_plan.base_branch,
                        "mode": collapse_request.mode,
                        "stack_collapse_plan": executed_plan.model_dump(mode="json"),
                    }
                    driver_result = {
                        "mode": collapse_request.mode,
                        "stack_collapse_plan": result["stack_collapse_plan"],
                    }
                else:
                    stack_collapse_plan = collapse_existing_record.plan
                    if stack_collapse_plan.status != "waiting_for_root_checks":
                        raise ValueError(
                            "merge train stack collapse plan is not ready for train admission"
                        )
                    if stack_collapse_plan.policy_sha256 != policy_record.policy_sha256:
                        raise ValueError(
                            "merge train stack collapse policy digest no longer matches"
                        )
                    snapshot = GitHubMergeTrainSnapshotReader(
                        transport=transport
                    ).read_merge_train_snapshot(
                        repository=collapse_request.repository,
                        base_branch=collapse_request.base_branch,
                    )
                    root_pull_request = next(
                        (
                            pull_request
                            for pull_request in snapshot.pull_requests
                            if pull_request.number == stack_collapse_plan.root_pull_request_number
                        ),
                        None,
                    )
                    if root_pull_request is None:
                        raise ValueError("merge train stack collapse root PR is missing")
                    if root_pull_request.head_sha != _stack_collapse_expected_root_head_sha(
                        stack_collapse_plan
                    ):
                        raise ValueError(
                            "merge train stack collapse root PR head no longer matches"
                        )
                    snapshot = snapshot.model_copy(update={"pull_requests": (root_pull_request,)})
                    dry_run_result = build_merge_train_dry_run_result(
                        policy=policy, snapshot=snapshot
                    )
                    candidate = build_merge_train_batch_candidate(
                        dry_run_result=dry_run_result,
                        base_sha=snapshot.base_sha,
                        policy_sha256=policy_record.policy_sha256,
                        created_at=recorded_at,
                    )
                    candidate_record = build_merge_train_batch_candidate_record(
                        candidate=candidate,
                        source=f"service:stack-collapse-admit:{request_trace_id}",
                        updated_at=recorded_at,
                    )
                    _merge_train_batch_candidate_record_store(
                        record_store
                    ).write_merge_train_batch_candidate_record(candidate_record)
                    result = {
                        "merge_train_batch_candidate_record_id": candidate_record.record_id,
                        "repository": candidate.repository,
                        "base_branch": candidate.base_branch,
                        "mode": collapse_request.mode,
                        "candidate": candidate.model_dump(mode="json"),
                    }
                    driver_result = {
                        "mode": collapse_request.mode,
                        "dry_run_result": dry_run_result.model_dump(mode="json"),
                        "candidate": result["candidate"],
                    }
            elif path == _MERGE_TRAIN_CONTROLLER_RUN_ONCE_ROUTE:
                controller_request = MergeTrainControllerRunOnceEnvelope.model_validate(payload)
                policy_record = resolve_merge_train_policy_record(record_store)
                policy = policy_record.policy
                repository_policy = policy.find_repository_policy(
                    repository=controller_request.repository,
                    base_branch=controller_request.base_branch,
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
                recorded_at = _utc_now_timestamp()
                candidate_store = _merge_train_batch_candidate_record_store(record_store)
                landing_store = _merge_train_batch_landing_plan_record_store(record_store)
                collapse_store = _merge_train_stack_collapse_plan_record_store(record_store)
                transport = UrllibMergeTrainGitHubTransport(
                    token=token,
                    api_base_url=controller_request.github_api_base_url,
                )
                github_client = GitHubMergeTrainClient(transport=transport)

                active_landing_record = _latest_merge_train_batch_landing_plan_record(
                    record_store=landing_store,
                    repository=controller_request.repository,
                    base_branch=controller_request.base_branch,
                )
                if active_landing_record is not None:
                    try:
                        _validate_merge_train_landing_record_for_controller(
                            landing_record=active_landing_record,
                            policy_key=repository_policy.policy_key,
                            policy_sha256=policy_record.policy_sha256,
                        )
                    except ValueError as error:
                        raise MergeTrainControllerRequestError(str(error)) from error
                    collapse_record = _latest_merge_train_stack_collapse_plan_record_for_landing(
                        record_store=collapse_store,
                        repository=controller_request.repository,
                        base_branch=controller_request.base_branch,
                        landing_plan=active_landing_record.landing_plan,
                        policy_sha256=policy_record.policy_sha256,
                    )
                    if collapse_record is not None:
                        try:
                            _validate_stack_collapse_record_for_landing(
                                collapse_record=collapse_record,
                                landing_plan=active_landing_record.landing_plan,
                                policy_sha256=policy_record.policy_sha256,
                            )
                        except ValueError as error:
                            raise MergeTrainControllerRequestError(str(error)) from error
                        if not repository_policy.stack_child_disposition_label:
                            raise ValueError(
                                "merge train stack child disposition requires stack_child_disposition_label policy"
                            )
                    if not controller_request.mutate:
                        result = {
                            "repository": controller_request.repository,
                            "base_branch": controller_request.base_branch,
                            "mode": "dry-run",
                            "controller_action": "land_batch",
                            "merge_train_batch_landing_plan_record_id": active_landing_record.record_id,
                        }
                        if collapse_record is not None:
                            result["merge_train_stack_collapse_plan_record_id"] = (
                                collapse_record.record_id
                            )
                        driver_result = result
                    else:
                        try:
                            landed_plan = github_client.land_batch_candidate(
                                landing_plan=active_landing_record.landing_plan
                            )
                        except MergeTrainGitHubStaleHeadError as error:
                            stale_plan = _stale_merge_train_landing_plan(
                                active_landing_record.landing_plan
                            )
                            stale_record = build_merge_train_batch_landing_plan_record(
                                landing_plan=stale_plan,
                                source=f"service:controller:stale-landing:{request_trace_id}",
                                updated_at=recorded_at,
                            )
                            landing_store.write_merge_train_batch_landing_plan_record(stale_record)
                            message = (
                                str(error).strip()
                                or "Merge train landing evidence no longer matches GitHub."
                            )
                            result = {
                                "merge_train_batch_landing_plan_record_id": stale_record.record_id,
                                "repository": stale_plan.repository,
                                "base_branch": stale_plan.base_branch,
                                "mode": "stale_landing",
                                "controller_action": "land_batch",
                                "landing_plan": stale_plan.model_dump(mode="json"),
                                "error": {
                                    "code": "merge_train_github_stale_state",
                                    "message": message,
                                },
                                "details": {
                                    "github_status_code": error.status_code,
                                },
                            }
                            driver_result = result
                        else:
                            landed_record = build_merge_train_batch_landing_plan_record(
                                landing_plan=landed_plan,
                                source=f"service:controller:land:{request_trace_id}",
                                updated_at=recorded_at,
                            )
                            landing_store.write_merge_train_batch_landing_plan_record(landed_record)
                            candidate_ref_cleanup_result = _cleanup_merge_train_batch_candidate_ref(
                                github_client=github_client,
                                landing_plan=landed_plan,
                                request_trace_id=request_trace_id,
                            )
                            result = {
                                "merge_train_batch_landing_plan_record_id": landed_record.record_id,
                                "repository": landed_plan.repository,
                                "base_branch": landed_plan.base_branch,
                                "mode": "land",
                                "controller_action": "land_batch",
                                "landing_plan": landed_plan.model_dump(mode="json"),
                                **candidate_ref_cleanup_result,
                            }
                            if collapse_record is not None:
                                root_entry = next(
                                    (
                                        entry
                                        for entry in landed_plan.entries
                                        if entry.pull_request_number
                                        == collapse_record.plan.root_pull_request_number
                                    ),
                                    None,
                                )
                                if root_entry is None or root_entry.status != "merged":
                                    raise ValueError(
                                        "merge train stack child disposition requires merged root PR"
                                    )
                                reconciled_collapse_plan = (
                                    reconcile_merge_train_stack_children_after_root_landing(
                                        plan=collapse_record.plan,
                                        disposition_client=github_client,
                                        root_merge_commit_sha=root_entry.merge_commit_sha,
                                        label=repository_policy.stack_child_disposition_label,
                                        updated_at=recorded_at,
                                    )
                                )
                                reconciled_record = build_merge_train_stack_collapse_plan_record(
                                    plan=reconciled_collapse_plan,
                                    source=f"service:controller:child-disposition:{request_trace_id}",
                                    updated_at=recorded_at,
                                )
                                collapse_store.write_merge_train_stack_collapse_plan_record(
                                    reconciled_record
                                )
                                result["merge_train_stack_collapse_plan_record_id"] = (
                                    reconciled_record.record_id
                                )
                                result["stack_collapse_plan"] = reconciled_collapse_plan.model_dump(
                                    mode="json"
                                )
                            driver_result = result
                else:
                    active_candidate_record = _latest_merge_train_batch_candidate_record(
                        record_store=candidate_store,
                        repository=controller_request.repository,
                        base_branch=controller_request.base_branch,
                    )
                    passed_candidate_record = None
                    if active_candidate_record is None:
                        passed_candidate_record = _latest_passed_merge_train_batch_candidate_record(
                            record_store=candidate_store,
                            landing_plan_record_store=landing_store,
                            repository=controller_request.repository,
                            base_branch=controller_request.base_branch,
                        )
                    if active_candidate_record is not None:
                        try:
                            _validate_merge_train_candidate_record_for_controller(
                                candidate_record=active_candidate_record,
                                policy_key=repository_policy.policy_key,
                                policy_sha256=policy_record.policy_sha256,
                            )
                        except ValueError as error:
                            raise MergeTrainControllerRequestError(str(error)) from error
                        if active_candidate_record.candidate.status == "failed":
                            reflow_result = _try_reflow_failed_merge_train_candidate(
                                candidate_store=candidate_store,
                                active_candidate_record=active_candidate_record,
                                policy=policy,
                                policy_sha256=policy_record.policy_sha256,
                                transport=transport,
                                repository=controller_request.repository,
                                base_branch=controller_request.base_branch,
                                recorded_at=recorded_at,
                                request_trace_id=request_trace_id,
                                mutate=controller_request.mutate,
                            )
                            if reflow_result is None:
                                result = {
                                    "repository": controller_request.repository,
                                    "base_branch": controller_request.base_branch,
                                    "mode": "dry-run",
                                    "controller_action": "candidate_failed",
                                    "merge_train_batch_candidate_record_id": active_candidate_record.record_id,
                                    "candidate": active_candidate_record.candidate.model_dump(
                                        mode="json"
                                    ),
                                }
                            else:
                                result = reflow_result
                            driver_result = result
                        else:
                            if active_candidate_record.candidate.status in {
                                "planned",
                                "building",
                            }:
                                controller_action = "build_candidate"
                                if controller_request.mutate:
                                    candidate = github_client.build_batch_candidate(
                                        candidate=active_candidate_record.candidate
                                    )
                                else:
                                    candidate = active_candidate_record.candidate
                            else:
                                controller_action = "observe_candidate"
                                if controller_request.mutate:
                                    candidate = github_client.observe_batch_candidate_checks(
                                        candidate=active_candidate_record.candidate
                                    )
                                else:
                                    candidate = active_candidate_record.candidate
                            result = {
                                "repository": controller_request.repository,
                                "base_branch": controller_request.base_branch,
                                "mode": "dry-run"
                                if not controller_request.mutate
                                else controller_action,
                                "controller_action": controller_action,
                                "merge_train_batch_candidate_record_id": active_candidate_record.record_id,
                            }
                            if controller_request.mutate:
                                updated_candidate_record = build_merge_train_batch_candidate_record(
                                    candidate=candidate,
                                    source=f"service:controller:{controller_action}:{request_trace_id}",
                                    updated_at=recorded_at,
                                )
                                candidate_store.write_merge_train_batch_candidate_record(
                                    updated_candidate_record
                                )
                                result["merge_train_batch_candidate_record_id"] = (
                                    updated_candidate_record.record_id
                                )
                            result["candidate"] = candidate.model_dump(mode="json")
                            driver_result = result
                    elif passed_candidate_record is not None:
                        try:
                            _validate_merge_train_candidate_record_for_controller(
                                candidate_record=passed_candidate_record,
                                policy_key=repository_policy.policy_key,
                                policy_sha256=policy_record.policy_sha256,
                            )
                        except ValueError as error:
                            raise MergeTrainControllerRequestError(str(error)) from error
                        completed_landing_record = (
                            _latest_completed_merge_train_batch_landing_plan_record(
                                record_store=landing_store,
                                repository=controller_request.repository,
                                base_branch=controller_request.base_branch,
                                batch_id=passed_candidate_record.candidate.batch_id,
                                candidate_sha=passed_candidate_record.candidate.candidate_sha,
                            )
                        )
                        if completed_landing_record is not None:
                            try:
                                _validate_merge_train_landing_record_for_controller(
                                    landing_record=completed_landing_record,
                                    policy_key=repository_policy.policy_key,
                                    policy_sha256=policy_record.policy_sha256,
                                )
                            except ValueError as error:
                                raise MergeTrainControllerRequestError(str(error)) from error
                            result = {
                                "repository": controller_request.repository,
                                "base_branch": controller_request.base_branch,
                                "mode": "dry-run",
                                "controller_action": "batch_landed",
                                "merge_train_batch_candidate_record_id": passed_candidate_record.record_id,
                                "merge_train_batch_landing_plan_record_id": completed_landing_record.record_id,
                                "landing_plan": completed_landing_record.landing_plan.model_dump(
                                    mode="json"
                                ),
                            }
                            driver_result = result
                        elif not controller_request.mutate:
                            result = {
                                "repository": controller_request.repository,
                                "base_branch": controller_request.base_branch,
                                "mode": "dry-run",
                                "controller_action": "plan_landing",
                                "merge_train_batch_candidate_record_id": passed_candidate_record.record_id,
                            }
                            driver_result = result
                        else:
                            landing_plan = build_merge_train_batch_landing_plan(
                                candidate=passed_candidate_record.candidate,
                                merge_method=repository_policy.merge_method,
                                created_at=recorded_at,
                            )
                            landing_record = build_merge_train_batch_landing_plan_record(
                                landing_plan=landing_plan,
                                source=f"service:controller:landing-plan:{request_trace_id}",
                                updated_at=recorded_at,
                            )
                            landing_store.write_merge_train_batch_landing_plan_record(
                                landing_record
                            )
                            result = {
                                "merge_train_batch_landing_plan_record_id": landing_record.record_id,
                                "repository": landing_plan.repository,
                                "base_branch": landing_plan.base_branch,
                                "mode": "plan_landing",
                                "controller_action": "plan_landing",
                                "landing_plan": landing_plan.model_dump(mode="json"),
                            }
                            driver_result = result
                    else:
                        waiting_collapse_record = _latest_merge_train_stack_collapse_plan_record(
                            record_store=collapse_store,
                            repository=controller_request.repository,
                            base_branch=controller_request.base_branch,
                            plan_status="waiting_for_root_checks",
                        )
                        if waiting_collapse_record is not None:
                            snapshot = GitHubMergeTrainSnapshotReader(
                                transport=transport
                            ).read_merge_train_snapshot(
                                repository=controller_request.repository,
                                base_branch=controller_request.base_branch,
                            )
                            root_pull_request = next(
                                (
                                    pull_request
                                    for pull_request in snapshot.pull_requests
                                    if pull_request.number
                                    == waiting_collapse_record.plan.root_pull_request_number
                                ),
                                None,
                            )
                            if (
                                root_pull_request is None
                                or root_pull_request.head_sha
                                != _stack_collapse_expected_root_head_sha(
                                    waiting_collapse_record.plan
                                )
                            ):
                                waiting_collapse_record = None
                            else:
                                try:
                                    _validate_merge_train_stack_collapse_record_for_controller(
                                        collapse_record=waiting_collapse_record,
                                        policy_key=repository_policy.policy_key,
                                        policy_sha256=policy_record.policy_sha256,
                                    )
                                except ValueError as error:
                                    raise MergeTrainControllerRequestError(str(error)) from error
                                root_snapshot = snapshot.model_copy(
                                    update={"pull_requests": (root_pull_request,)}
                                )
                                dry_run_result = build_merge_train_dry_run_result(
                                    policy=policy, snapshot=root_snapshot
                                )
                                if dry_run_result.intended_next_action != "merge":
                                    result = {
                                        "repository": controller_request.repository,
                                        "base_branch": controller_request.base_branch,
                                        "mode": "dry-run",
                                        "controller_action": "wait_for_root_checks",
                                        "merge_train_stack_collapse_plan_record_id": waiting_collapse_record.record_id,
                                        "dry_run_result": dry_run_result.model_dump(mode="json"),
                                    }
                                    driver_result = result
                                elif not controller_request.mutate:
                                    result = {
                                        "repository": controller_request.repository,
                                        "base_branch": controller_request.base_branch,
                                        "mode": "dry-run",
                                        "controller_action": "admit_collapsed_root",
                                        "merge_train_stack_collapse_plan_record_id": waiting_collapse_record.record_id,
                                        "dry_run_result": dry_run_result.model_dump(mode="json"),
                                    }
                                    driver_result = result
                                else:
                                    candidate = build_merge_train_batch_candidate(
                                        dry_run_result=dry_run_result,
                                        base_sha=root_snapshot.base_sha,
                                        policy_sha256=policy_record.policy_sha256,
                                        created_at=recorded_at,
                                    )
                                    candidate_record = build_merge_train_batch_candidate_record(
                                        candidate=candidate,
                                        source=f"service:controller:stack-collapse-admit:{request_trace_id}",
                                        updated_at=recorded_at,
                                    )
                                    candidate_store.write_merge_train_batch_candidate_record(
                                        candidate_record
                                    )
                                    result = {
                                        "merge_train_batch_candidate_record_id": candidate_record.record_id,
                                        "merge_train_stack_collapse_plan_record_id": waiting_collapse_record.record_id,
                                        "repository": candidate.repository,
                                        "base_branch": candidate.base_branch,
                                        "mode": "admit_collapsed_root",
                                        "controller_action": "admit_collapsed_root",
                                        "dry_run_result": dry_run_result.model_dump(mode="json"),
                                        "candidate": candidate.model_dump(mode="json"),
                                    }
                                    driver_result = result
                        if waiting_collapse_record is None:
                            planned_collapse_record = (
                                _latest_merge_train_stack_collapse_plan_record(
                                    record_store=collapse_store,
                                    repository=controller_request.repository,
                                    base_branch=controller_request.base_branch,
                                    plan_status="planned",
                                )
                            )
                            if planned_collapse_record is not None:
                                snapshot = GitHubMergeTrainSnapshotReader(
                                    transport=transport
                                ).read_merge_train_snapshot(
                                    repository=controller_request.repository,
                                    base_branch=controller_request.base_branch,
                                )
                                root_pull_request = next(
                                    (
                                        pull_request
                                        for pull_request in snapshot.pull_requests
                                        if pull_request.number
                                        == planned_collapse_record.plan.root_pull_request_number
                                    ),
                                    None,
                                )
                                if (
                                    root_pull_request is None
                                    or root_pull_request.head_sha
                                    != planned_collapse_record.plan.root_initial_head_sha
                                ):
                                    planned_collapse_record = None
                                else:
                                    try:
                                        _validate_merge_train_stack_collapse_record_for_controller(
                                            collapse_record=planned_collapse_record,
                                            policy_key=repository_policy.policy_key,
                                            policy_sha256=policy_record.policy_sha256,
                                        )
                                    except ValueError as error:
                                        raise MergeTrainControllerRequestError(
                                            str(error)
                                        ) from error
                                    result = {
                                        "repository": controller_request.repository,
                                        "base_branch": controller_request.base_branch,
                                        "mode": "dry-run"
                                        if not controller_request.mutate
                                        else "execute_stack_collapse",
                                        "controller_action": "execute_stack_collapse",
                                        "merge_train_stack_collapse_plan_record_id": planned_collapse_record.record_id,
                                    }
                                    if controller_request.mutate:
                                        executed_plan = execute_merge_train_stack_collapse_plan(
                                            plan=planned_collapse_record.plan,
                                            branch_client=github_client,
                                            updated_at=recorded_at,
                                        )
                                        executed_record = build_merge_train_stack_collapse_plan_record(
                                            plan=executed_plan,
                                            source=f"service:controller:stack-collapse-execute:{request_trace_id}",
                                            updated_at=recorded_at,
                                        )
                                        collapse_store.write_merge_train_stack_collapse_plan_record(
                                            executed_record
                                        )
                                        result["merge_train_stack_collapse_plan_record_id"] = (
                                            executed_record.record_id
                                        )
                                        result["stack_collapse_plan"] = executed_plan.model_dump(
                                            mode="json"
                                        )
                                    else:
                                        result["stack_collapse_plan"] = (
                                            planned_collapse_record.plan.model_dump(mode="json")
                                        )
                                    driver_result = result
                            if planned_collapse_record is None:
                                snapshot = GitHubMergeTrainSnapshotReader(
                                    transport=transport
                                ).read_merge_train_snapshot(
                                    repository=controller_request.repository,
                                    base_branch=controller_request.base_branch,
                                )
                                dry_run_result = build_merge_train_dry_run_result(
                                    policy=policy, snapshot=snapshot
                                )
                                selected_pr = dry_run_result.selected_pr
                                if (
                                    selected_pr is not None
                                    and _merge_train_snapshot_has_stack_topology(
                                        snapshot=snapshot, dry_run_result=dry_run_result
                                    )
                                ):
                                    stack_discovery = discover_merge_train_stack(
                                        snapshot=snapshot,
                                        root_pull_request_number=selected_pr.number,
                                    )
                                else:
                                    stack_discovery = None
                                if (
                                    stack_discovery is not None
                                    and stack_discovery.status == "ready_for_collapse"
                                ):
                                    controller_action = "plan_stack_collapse"
                                    stack_collapse_plan = build_merge_train_stack_collapse_plan(
                                        discovery_result=stack_discovery,
                                        policy_key=dry_run_result.policy_key,
                                        policy_sha256=policy_record.policy_sha256,
                                        created_at=recorded_at,
                                    )
                                    result = {
                                        "repository": stack_collapse_plan.repository,
                                        "base_branch": stack_collapse_plan.base_branch,
                                        "mode": "dry-run"
                                        if not controller_request.mutate
                                        else controller_action,
                                        "controller_action": controller_action,
                                        "dry_run_result": dry_run_result.model_dump(mode="json"),
                                        "stack_discovery": stack_discovery.model_dump(mode="json"),
                                        "stack_collapse_plan": stack_collapse_plan.model_dump(
                                            mode="json"
                                        ),
                                    }
                                    if controller_request.mutate:
                                        stack_collapse_record = build_merge_train_stack_collapse_plan_record(
                                            plan=stack_collapse_plan,
                                            source=f"service:controller:stack-collapse-plan:{request_trace_id}",
                                            updated_at=recorded_at,
                                        )
                                        collapse_store.write_merge_train_stack_collapse_plan_record(
                                            stack_collapse_record
                                        )
                                        result["merge_train_stack_collapse_plan_record_id"] = (
                                            stack_collapse_record.record_id
                                        )
                                    driver_result = result
                                elif (
                                    stack_discovery is not None
                                    and stack_discovery.status == "unsupported"
                                ):
                                    result = {
                                        "repository": controller_request.repository,
                                        "base_branch": controller_request.base_branch,
                                        "mode": "dry-run",
                                        "controller_action": "stack_unsupported",
                                        "dry_run_result": dry_run_result.model_dump(mode="json"),
                                        "stack_discovery": stack_discovery.model_dump(mode="json"),
                                    }
                                    driver_result = result
                                elif dry_run_result.intended_next_action == "idle":
                                    result = {
                                        "repository": controller_request.repository,
                                        "base_branch": controller_request.base_branch,
                                        "mode": "dry-run",
                                        "controller_action": "idle",
                                        "dry_run_result": dry_run_result.model_dump(mode="json"),
                                    }
                                    driver_result = result
                                elif dry_run_result.intended_next_action != "merge":
                                    result = {
                                        "repository": controller_request.repository,
                                        "base_branch": controller_request.base_branch,
                                        "mode": "dry-run",
                                        "controller_action": dry_run_result.intended_next_action,
                                        "dry_run_result": dry_run_result.model_dump(mode="json"),
                                    }
                                    driver_result = result
                                else:
                                    controller_action = "plan_candidate"
                                    candidate = build_merge_train_batch_candidate(
                                        dry_run_result=dry_run_result,
                                        base_sha=snapshot.base_sha,
                                        policy_sha256=policy_record.policy_sha256,
                                        created_at=recorded_at,
                                    )
                                    result = {
                                        "repository": candidate.repository,
                                        "base_branch": candidate.base_branch,
                                        "mode": "dry-run"
                                        if not controller_request.mutate
                                        else controller_action,
                                        "controller_action": controller_action,
                                        "dry_run_result": dry_run_result.model_dump(mode="json"),
                                        "candidate": candidate.model_dump(mode="json"),
                                    }
                                    if controller_request.mutate:
                                        candidate_record = build_merge_train_batch_candidate_record(
                                            candidate=candidate,
                                            source=f"service:controller:candidate-plan:{request_trace_id}",
                                            updated_at=recorded_at,
                                        )
                                        candidate_store.write_merge_train_batch_candidate_record(
                                            candidate_record
                                        )
                                        result["merge_train_batch_candidate_record_id"] = (
                                            candidate_record.record_id
                                        )
                                    driver_result = result
            elif path in descriptor_driver_dispatch_routes:
                try:
                    dispatch_response = _dispatch_descriptor_driver_route(
                        dispatch_route=descriptor_driver_dispatch_routes[path],
                        payload=payload,
                        record_store=record_store,
                        control_plane_root_path=resolved_root,
                        state_dir=state_dir,
                        database_url=database_url,
                        authz_policy=authz_policy,
                        identity=identity,
                        request_scope=request_scope,
                        request_idempotency_key=request_idempotency_key,
                        request_fingerprint=request_fingerprint,
                        start_response=start_response,
                        trace_id=request_trace_id,
                    )
                except DriverRouteDependencyNotFoundError:
                    return _json_response(
                        start_response=start_response,
                        status_code=503,
                        payload={
                            "status": "rejected",
                            "trace_id": request_trace_id,
                            "error": {
                                "code": "driver_route_dependency_not_found",
                                "message": (
                                    "Driver route is registered, but required"
                                    " product or runtime records were not found."
                                ),
                            },
                            "details": {
                                "route_path": path,
                            },
                        },
                    )
                if isinstance(dispatch_response, list):
                    return dispatch_response
                result, driver_result = dispatch_response
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
                result = execute_launchplane_self_deploy(
                    control_plane_root_path=resolved_root,
                    request=self_deploy_request.deploy,
                ).model_dump(mode="json")
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
                if preview_pr_feedback_request.dry_run:
                    preview_pr_feedback_dry_run_result: dict[str, object] = {
                        "dry_run": True,
                        "preview_pr_feedback": "authorized",
                        "product": preview_pr_feedback_request.product,
                        "context": preview_pr_feedback_request.context,
                        "status": preview_pr_feedback_request.status,
                        "anchor_pr_number": preview_pr_feedback_request.anchor_pr_number,
                    }
                    return _json_response(
                        start_response=start_response,
                        status_code=202,
                        payload=_accepted_payload(
                            trace_id=request_trace_id,
                            result=preview_pr_feedback_dry_run_result,
                            driver_result=preview_pr_feedback_dry_run_result,
                        ),
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
                notification_attempts = _deliver_preview_pr_feedback_notifications(
                    record_store=record_store,
                    feedback=driver_result,
                    attempted_at=driver_result.requested_at,
                    discord_sender=preview_pr_feedback_discord_sender,
                )
                result = {"preview_pr_feedback_id": preview_pr_feedback_id}
                if notification_attempts:
                    result["preview_pr_feedback_notification_attempt_count"] = len(
                        notification_attempts
                    )
                    feedback_payload = driver_result.model_dump(mode="json")
                    feedback_payload["notifications"] = [
                        attempt.model_dump(mode="json") for attempt in notification_attempts
                    ]
                    driver_result = {
                        **feedback_payload,
                    }
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
                    cleanup_driver_id = _preview_lifecycle_cleanup_driver_id(cleanup_profile)
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
            elif path == "/v1/previews/lifecycle-sweep":
                preview_lifecycle_sweep_request = PreviewLifecycleSweepEnvelope.model_validate(
                    payload
                )
                requested_sweep_profiles = _preview_lifecycle_sweep_profiles(
                    record_store=record_store,
                    product=preview_lifecycle_sweep_request.product,
                )
                if preview_lifecycle_sweep_request.product.strip() and not requested_sweep_profiles:
                    return _json_response(
                        start_response=start_response,
                        status_code=404,
                        payload={
                            "status": "rejected",
                            "trace_id": request_trace_id,
                            "error": {
                                "code": "not_found",
                                "message": "Preview lifecycle sweep found no enabled preview profile for the requested product.",
                            },
                        },
                    )
                denied_profile = next(
                    (
                        profile
                        for profile in requested_sweep_profiles
                        if not authz_policy.allows(
                            identity=identity,
                            action="preview_lifecycle.plan",
                            product=profile.product,
                            context=profile.preview.context,
                        )
                        or not authz_policy.allows(
                            identity=identity,
                            action="preview_lifecycle.cleanup",
                            product=profile.product,
                            context=profile.preview.context,
                        )
                    ),
                    None,
                )
                if denied_profile is not None:
                    return _json_response(
                        start_response=start_response,
                        status_code=403,
                        payload={
                            "status": "rejected",
                            "trace_id": request_trace_id,
                            "error": {
                                "code": "authorization_denied",
                                "message": (
                                    "Workflow cannot sweep preview lifecycle for one or more"
                                    " enabled product profiles."
                                ),
                            },
                            "authz": _authz_diagnostic_payload(
                                identity=identity,
                                authz_policy_sha256_value=resolved_authz_policy_sha256,
                                authz_policy_source=resolved_authz_policy_source,
                                action="preview_lifecycle.cleanup",
                                product=denied_profile.product,
                                context=denied_profile.preview.context,
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
                driver_result = _build_preview_lifecycle_sweep(
                    control_plane_root=resolved_root,
                    record_store=record_store,
                    request=preview_lifecycle_sweep_request,
                )
                result = {}
            elif path.startswith("/v1/drivers/"):
                return _json_response(
                    start_response=start_response,
                    status_code=500,
                    payload={
                        "status": "rejected",
                        "trace_id": request_trace_id,
                        "error": {
                            "code": "driver_route_not_registered",
                            "message": (
                                "Driver route is declared but has no registered service handler."
                            ),
                        },
                    },
                )
            else:
                return _not_found_response(
                    start_response=start_response,
                    trace_id=request_trace_id,
                    path=path,
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
        except ValidationError as error:
            if path == _MERGE_TRAIN_CONTROLLER_RUN_ONCE_ROUTE:
                message = str(error).strip() or "Request payload failed validation."
                return _json_response(
                    start_response=start_response,
                    status_code=400,
                    payload={
                        "status": "rejected",
                        "trace_id": request_trace_id,
                        "error": {
                            "code": "merge_train_controller_invalid_state",
                            "message": message,
                        },
                    },
                )
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
        except VeriReelPreviewRefreshTransportError as error:
            error_message = str(error).strip() or "VeriReel preview refresh backend request failed."
            return _json_response(
                start_response=start_response,
                status_code=502,
                payload={
                    "status": "rejected",
                    "trace_id": request_trace_id,
                    "error": {
                        "code": "preview_refresh_backend_unavailable",
                        "message": error_message,
                    },
                },
            )
        except OdooPreviewApplyConfigError as error:
            return _json_response(
                start_response=start_response,
                status_code=400,
                payload={
                    "status": "rejected",
                    "trace_id": request_trace_id,
                    "error": {
                        "code": "odoo_preview_runtime_config_incomplete",
                        "message": "Odoo preview apply runtime environment is incomplete.",
                    },
                    "details": {
                        "context": error.context,
                        "instance": error.instance,
                        "missing_keys": list(error.missing_keys),
                    },
                },
            )
        except MergeTrainGitHubStaleHeadError as error:
            message = str(error).strip() or "Merge train landing evidence no longer matches GitHub."
            return _json_response(
                start_response=start_response,
                status_code=409,
                payload={
                    "status": "rejected",
                    "trace_id": request_trace_id,
                    "error": {
                        "code": "merge_train_github_stale_state",
                        "message": message,
                    },
                    "details": {
                        "github_status_code": error.status_code,
                    },
                },
            )
        except MergeTrainGitHubError as error:
            message = str(error).strip() or "GitHub merge train request failed."
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
                    "details": {
                        "github_status_code": error.status_code,
                        "message": message,
                    },
                },
            )
        except MergeTrainPolicyStoreMissingError:
            return _json_response(
                start_response=start_response,
                status_code=503,
                payload={
                    "status": "rejected",
                    "trace_id": request_trace_id,
                    "error": {
                        "code": "merge_train_policy_not_configured",
                        "message": "No active DB-backed merge train policy record is configured.",
                    },
                },
            )
        except MergeTrainControllerRequestError as error:
            message = str(error).strip() or "Merge train controller request could not be completed."
            return _json_response(
                start_response=start_response,
                status_code=400,
                payload={
                    "status": "rejected",
                    "trace_id": request_trace_id,
                    "error": {
                        "code": "merge_train_controller_invalid_state",
                        "message": message,
                    },
                },
            )
        except (ValueError, click.ClickException) as error:
            if path == _MERGE_TRAIN_CONTROLLER_RUN_ONCE_ROUTE:
                message = (
                    str(error).strip() or "Merge train controller request could not be completed."
                )
                return _json_response(
                    start_response=start_response,
                    status_code=400,
                    payload={
                        "status": "rejected",
                        "trace_id": request_trace_id,
                        "error": {
                            "code": "merge_train_controller_invalid_state",
                            "message": message,
                        },
                    },
                )
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
            extra_record_keys=_accepted_payload_extra_record_keys(route_path=path),
        )
        should_store_idempotency = _should_store_idempotency_record(
            path=effective_idempotency_route_path,
            driver_result=driver_result,
        )
        if method == "POST" and request_idempotency_key and should_store_idempotency:
            _write_idempotency_record(
                record_store=record_store,
                scope=request_scope,
                route_path=effective_idempotency_route_path,
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
    import uvicorn

    from control_plane.service_auth import GitHubOidcVerifier

    bootstrap_authz_policy = load_authz_policy(policy_file)
    verifier = GitHubOidcVerifier(audience=audience)
    service_record_store = build_shared_record_store(database_url=database_url)
    resolved_fastapi_policy = resolve_launchplane_authz_policy(
        record_store=service_record_store,
        bootstrap_policy=bootstrap_authz_policy,
        policy_source=_bootstrap_policy_source_from_env(),
        now_timestamp=_now_timestamp(),
    )
    authz_policy_runtime = LaunchplaneAuthzPolicyRuntime(
        resolved_fastapi_policy.policy,
        policy_sha256=resolved_fastapi_policy.policy_sha256,
        source=resolved_fastapi_policy.source,
    )
    work_graph_project_config = load_github_project_planning_facts_config_from_env(dict(os.environ))
    work_graph_planning_facts_provider = (
        (lambda: build_github_project_planning_facts(work_graph_project_config))
        if work_graph_project_config is not None
        else None
    )
    work_graph_issue_inbox_config = load_github_issue_inbox_config_from_env(
        dict(os.environ),
        project_config=work_graph_project_config,
    )
    work_graph_issue_inbox_provider = (
        (
            lambda: build_github_issue_inbox_read_model(
                generated_at=_utc_now_timestamp(),
                config=work_graph_issue_inbox_config,
            )
        )
        if work_graph_issue_inbox_config is not None
        else None
    )
    work_graph_issue_inbox_reconcile_provider = (
        (
            lambda request: reconcile_github_issue_inbox(
                generated_at=_utc_now_timestamp(),
                config=work_graph_issue_inbox_config,
                request=request,
            )
        )
        if work_graph_issue_inbox_config is not None
        else None
    )
    application = create_launchplane_service_app(
        state_dir=state_dir,
        verifier=verifier,
        authz_policy=bootstrap_authz_policy,
        database_url=database_url,
        authz_policy_runtime=authz_policy_runtime,
        record_store_for_service=service_record_store,
    )
    github_oauth_config = load_github_oauth_config_from_env()
    human_session_manager = (
        HumanSessionManager(
            config=github_oauth_config,
            session_store=service_record_store,
        )
        if github_oauth_config is not None
        else None
    )
    fastapi_application = create_launchplane_fastapi_app(
        verifier=verifier,
        authz_policy=resolved_fastapi_policy.policy,
        authz_policy_runtime=authz_policy_runtime,
        record_store_factory=lambda: service_record_store,
        bearer_identity_config=_bearer_identity_config_from_env(),
        human_session_manager=human_session_manager,
        control_plane_root_path=control_plane_root(),
        work_graph_planning_facts_provider=work_graph_planning_facts_provider,
        work_graph_issue_inbox_provider=work_graph_issue_inbox_provider,
        work_graph_issue_inbox_reconcile_provider=work_graph_issue_inbox_reconcile_provider,
        every_code_github_webhook_handler=handle_every_code_github_webhook_request,
    )
    fastapi_application.mount(
        "/",
        cast(ASGIApp, WSGIMiddleware(cast(Any, application))),
    )
    click.echo(f"Launchplane service listening on http://{host}:{port}")
    try:
        uvicorn.run(
            fastapi_application,
            host=host,
            port=port,
            log_config=None,
        )
    finally:
        service_record_store.close()
