from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
import os
import re
import secrets
import threading
from socketserver import ThreadingMixIn
import uuid
from pathlib import Path
from typing import Any, BinaryIO, Callable, Iterable, Literal, Protocol, cast
from urllib.parse import parse_qs
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
from control_plane import runtime_environments as control_plane_runtime_environments
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
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.contracts.edge_endpoint_record import EdgeEndpointRecord
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
from control_plane.contracts.ingress_route_audit_record import (
    IngressRouteAuditOperation,
    IngressRouteAuditRecord,
    build_ingress_route_audit_record_id,
)
from control_plane.contracts.ingress_canary_route_record import IngressCanaryRouteRecord
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
from control_plane.contracts.merge_train_policy import MergeTrainPolicyRecord
from control_plane.contracts.merge_train_pr_feedback_record import (
    MergeTrainPrFeedbackEvent,
    MergeTrainPrFeedbackRecord,
    build_merge_train_pr_feedback_id,
    merge_train_pr_feedback_marker,
)
from control_plane.merge_train_admission import build_merge_train_controller_status_read_model
from control_plane.merge_train_admission import evaluate_merge_train_admission_from_store
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
    odoo_stable_bootstrap_operation_is_terminal,
)
from control_plane.contracts.odoo_stable_target_replacement import (
    OdooStableTargetReplacementApplyRequest,
    OdooStableTargetReplacementApplyResult,
    OdooStableTargetReplacementRequest,
)
from control_plane.contracts.odoo_stable_target_replacement_operation import (
    OdooStableTargetReplacementOperationRecord,
    build_odoo_stable_target_replacement_operation_id,
    odoo_stable_target_replacement_operation_is_terminal,
)
from control_plane.contracts.preview_mutation_request import (
    PreviewDestroyMutationRequest,
    PreviewGenerationMutationRequest,
    PreviewMutationRequest,
)
from control_plane.contracts.preview_inventory_scan_record import PreviewInventoryScanRecord
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
from control_plane.contracts.protected_artifacts import build_protected_artifact_set
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
from control_plane.contracts.public_ingress_monitoring import (
    PublicIngressIncidentRecord,
    PublicIngressNotificationPolicyRecord,
)
from control_plane.contracts.runtime_key_safety_policy import RuntimeKeySafetyTarget
from control_plane.contracts.runner_host_hygiene import RunnerHostHygieneApplyAuditRecord
from control_plane.contracts.runner_lane_registration import RunnerLaneRegistrationAuditRecord
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
    GitHubActionsIdentity,
    GitHubHumanIdentity,
    LaunchplaneAuthzPolicy,
    LaunchplaneIdentity,
    LocalAdminIdentity,
    LocalOperatorIdentity,
    TerminalAgentIdentity,
    TokenVerifier,
    agent_authz_audit,
    load_authz_policy,
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
from control_plane.storage.factory import build_shared_record_store, storage_backend_name
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.tracked_target_logs import build_tracked_target_logs_payload
from control_plane.ui_static_http import serve_ui_route
from control_plane.product_config_http import (
    ProductConfigRouteResult,
    apply_product_config_route,
    product_config_database_required_response,
    validate_product_config_apply_request,
)
from control_plane.provider_target_operations_http import (
    PROVIDER_TARGET_OPERATIONS_ROUTE,
    ProviderTargetOperationEnvelope,
    ProviderTargetOperationRouteResult,
    execute_provider_target_operation_route,
    provider_target_operation_authorized,
    provider_target_operation_requires_reason,
)
from control_plane.work_graph_github_projects import (
    build_github_project_planning_facts,
    load_github_project_planning_facts_config_from_env,
)
from control_plane.work_graph_issue_inbox import (
    build_github_issue_inbox_read_model,
    load_github_issue_inbox_config_from_env,
    reconcile_github_issue_inbox,
)
from control_plane.work_graph_service import (
    WorkGraphIssueInboxProvider,
    WorkGraphIssueInboxReconcileProvider,
    WorkGraphPlanningFactsProvider,
)
from control_plane.work_graph_http import (
    handle_work_graph_issue_inbox_read,
    handle_repo_product_mapping_read,
    handle_work_graph_snapshot_read,
    rank_work_graph_snapshot,
    reconcile_work_graph_issue_inbox,
    work_graph_issue_inbox_reconcile_denied_response,
    work_graph_rank_denied_response,
)
from control_plane.workflows.npmplus_ingress import (
    NpmplusIngressApplyRequest,
    NpmplusIngressClient,
    NpmplusIngressApplyResult,
)
from control_plane.workflows.ingress_provider import (
    IngressProvider,
    NpmplusIngressProvider,
    default_ingress_provider,
)
from control_plane.workflows.launchplane_self_deploy import (
    LAUNCHPLANE_IMAGE_REFERENCE_ENV_KEY,
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
    apply_promotion_evidence,
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
from control_plane.workflows.dokploy_target_adoption import (
    DokployComposeTargetCreateResult,
    DokployTargetAdoptionResult,
    DokployTargetCreateResult,
    adopt_dokploy_target,
    create_dokploy_application_target,
    create_dokploy_compose_target,
)
from control_plane.workflows.product_onboarding import apply_product_onboarding_manifest
from control_plane.workflows.public_ingress_monitor import (
    PublicIngressNotificationDriverSet,
    build_github_issue_notifier,
    run_public_ingress_monitor_once,
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
    OdooStableBootstrapResult,
)
from control_plane.workflows.odoo_stable_bootstrap import (
    OdooStableBootstrapStore,
    execute_odoo_stable_bootstrap,
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
    execute_odoo_stable_target_replacement_apply,
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
    VeriReelProdBackupGateRequest,
    VeriReelProdBackupGateStore,
    execute_verireel_prod_backup_gate,
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
_EVERY_CODE_GITHUB_WEBHOOK_ROUTE = "/v1/every-code/github-webhook"
_MERGE_TRAIN_ADMISSION_ROUTE = "/v1/work-graph/merge-train/admission"
_MERGE_TRAIN_CONTROLLER_STATUS_ROUTE = "/v1/work-graph/merge-train/controller/status"
_MERGE_TRAIN_POLICY_TARGETS_ROUTE = "/v1/work-graph/merge-train/policy-targets"
_MERGE_TRAIN_BATCH_CANDIDATE_RUN_ONCE_ROUTE = "/v1/work-graph/merge-train/batch-candidate/run-once"
_MERGE_TRAIN_BATCH_LANDING_RUN_ONCE_ROUTE = "/v1/work-graph/merge-train/batch-landing/run-once"
_MERGE_TRAIN_STACK_COLLAPSE_RUN_ONCE_ROUTE = "/v1/work-graph/merge-train/stack-collapse/run-once"
_NPMPLUS_INGRESS_APPLY_ROUTE_PATH = "/v1/drivers/ingress/route-apply"
_EDGE_ENDPOINT_APPLY_ROUTE = "/v1/edge-endpoints/apply"
_INGRESS_CANARY_ROUTE_RECORD_APPLY_ROUTE = "/v1/ingress/canary-routes/records/apply"
_INGRESS_CANARY_ROUTE_APPLY_ROUTE = "/v1/ingress/canary-routes/apply"
_MERGE_TRAIN_CONTROLLER_RUN_ONCE_ROUTE = "/v1/work-graph/merge-train/controller/run-once"
_MERGE_TRAIN_PR_FEEDBACK_ROUTE = "/v1/work-graph/merge-train/pr-feedback"
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
_AGENT_WRITE_INTENT_MAX_AGE = timedelta(hours=24)


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


class _IngressProviderFactory(Protocol):
    def __call__(self) -> IngressProvider: ...


class _NpmplusIngressClientFactory(Protocol):
    def __call__(self) -> NpmplusIngressClient: ...


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


class _IngressRouteAuditRecordStore(Protocol):
    def write_ingress_route_audit_record(self, record: IngressRouteAuditRecord) -> object: ...

    def read_ingress_route_audit_record(self, record_id: str) -> IngressRouteAuditRecord: ...

    def list_ingress_route_audit_records(
        self,
        *,
        product: str = "",
        context_name: str = "",
        limit: int | None = None,
    ) -> tuple[IngressRouteAuditRecord, ...]: ...


class _EdgeEndpointRecordStore(Protocol):
    def write_edge_endpoint_record(self, record: EdgeEndpointRecord) -> object: ...

    def read_edge_endpoint_record(self, endpoint_key: str) -> EdgeEndpointRecord: ...

    def list_edge_endpoint_records(
        self,
        *,
        provider: str = "",
        status: str = "",
        limit: int | None = None,
    ) -> tuple[EdgeEndpointRecord, ...]: ...


class _IngressCanaryRouteRecordStore(Protocol):
    def write_ingress_canary_route_record(self, record: IngressCanaryRouteRecord) -> object: ...

    def read_ingress_canary_route_record(self, canary_key: str) -> IngressCanaryRouteRecord: ...

    def list_ingress_canary_route_records(
        self,
        *,
        product: str = "",
        context_name: str = "",
        status: str = "",
        limit: int | None = None,
    ) -> tuple[IngressCanaryRouteRecord, ...]: ...


_StartResponse = Callable[[str, list[tuple[str, str]]], None]
_WsgiApp = Callable[[dict[str, object], _StartResponse], list[bytes]]


_LOGGER = logging.getLogger(__name__)


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


class RunnerHostHygieneAuditEvidenceEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    product: str
    audit: RunnerHostHygieneApplyAuditRecord

    @model_validator(mode="after")
    def _validate_alignment(self) -> "RunnerHostHygieneAuditEvidenceEnvelope":
        if self.product.strip() != "launchplane":
            raise ValueError("runner host hygiene audit evidence requires product 'launchplane'")
        self.product = "launchplane"
        return self


class RunnerLaneRegistrationAuditEvidenceEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    product: str
    audit: RunnerLaneRegistrationAuditRecord

    @model_validator(mode="after")
    def _validate_alignment(self) -> "RunnerLaneRegistrationAuditEvidenceEnvelope":
        if self.product.strip() != "launchplane":
            raise ValueError(
                "runner lane registration audit evidence requires product 'launchplane'"
            )
        self.product = "launchplane"
        return self


class PublicIngressMonitorRunOnceEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    product: str = "launchplane"
    timeout_seconds: int = Field(default=10, ge=1, le=120)
    notify: bool = True

    @model_validator(mode="after")
    def _validate_request(self) -> "PublicIngressMonitorRunOnceEnvelope":
        if self.product.strip() != "launchplane":
            raise ValueError("public ingress monitor run requires product 'launchplane'")
        self.product = "launchplane"
        return self


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


class NpmplusIngressApplyEnvelope(_ProductRouteEnvelope):
    schema_version: int = Field(default=1, ge=1)
    context: str
    ingress: NpmplusIngressApplyRequest

    @model_validator(mode="after")
    def _validate_envelope(self) -> "NpmplusIngressApplyEnvelope":
        _validate_driver_envelope_product(self.product, label="NPMplus ingress apply")
        if not self.context.strip():
            raise ValueError("NPMplus ingress apply requires context.")
        return self


_NPMPLUS_INGRESS_APPLY_ROUTE = _DriverRouteExecutionMetadata(
    route_path=_NPMPLUS_INGRESS_APPLY_ROUTE_PATH,
    envelope_model=NpmplusIngressApplyEnvelope,
    denial_message=(
        "Workflow cannot plan or apply the ingress route for the requested product/context."
    ),
)


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


class MergeTrainPolicyImportEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    product: str = "launchplane"
    mode: Literal["dry_run", "apply"] = "dry_run"
    reason: str = ""
    record: MergeTrainPolicyRecord

    @model_validator(mode="after")
    def _validate_envelope(self) -> "MergeTrainPolicyImportEnvelope":
        self.product = self.product.strip() or "launchplane"
        if self.product != "launchplane":
            raise ValueError("merge train policy import requires product 'launchplane'")
        self.reason = self.reason.strip()
        if self.mode == "apply" and not self.reason:
            raise ValueError("merge train policy import apply requires reason")
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
        "/v1/agent/write-intents/evaluate",
        _GENERIC_WEB_PROD_PROMOTION_ROUTE.route_path,
        _GENERIC_WEB_PROD_PROMOTION_WORKFLOW_ROUTE.route_path,
        "/v1/product-config/apply",
        _NPMPLUS_INGRESS_APPLY_ROUTE.route_path,
        "/v1/authz-policies/github-actions/grants",
        "/v1/authz-policies/github-actions/removals",
        "/v1/authz-policies/github-humans/grants",
        "/v1/authz-policies/terminal-agents/grants",
        "/v1/authz-policies/local-operators/grants",
        "/v1/authz-policies/local-admins/grants",
        "/v1/merge-train/policies/import",
    }
)
_HUMAN_IDENTITY_READ_MODEL_POST_ROUTES = frozenset({"/v1/work-graph/rank"})
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
    source_url: str = ""
    agent_write_intent_record_id: str = ""

    @model_validator(mode="after")
    def _validate_rerun(self) -> "EveryCodeWorkRequestRerunEnvelope":
        if not self.request_id.strip():
            raise ValueError("Every Code work request rerun requires request_id")
        self.request_id = self.request_id.strip()
        self.trigger_actor = self.trigger_actor.strip()
        self.source_url = self.source_url.strip()
        self.agent_write_intent_record_id = self.agent_write_intent_record_id.strip()
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


class EveryCodePrFeedbackEnvelope(EveryCodePrFeedbackRecord):
    pass


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


class DokployTargetSetupEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    mode: Literal["dry-run", "apply"] = "dry-run"
    operation: Literal[
        "adopt",
        "create-application",
        "create-compose",
        "reconcile-compose-domain",
    ]
    product: str = "launchplane"
    context: str
    instance: str
    target_type: Literal["application", "compose"] = "compose"
    target_id: str = ""
    target_name: str = ""
    project_id: str = ""
    project_name: str = ""
    project_description: str = ""
    environment_id: str = ""
    environment_name: str = ""
    environment_description: str = ""
    server_id: str = ""
    app_name: str = ""
    description: str = ""
    source_git_ref: str = "origin/main"
    source_type: str = "raw"
    compose_path: str = "docker-compose.yml"
    healthcheck_path: str = ""
    domains: tuple[str, ...] = ()
    runtime_port: int | None = Field(default=None, ge=1, le=65535)
    deploy_timeout_seconds: int | None = Field(default=None, ge=1)
    confirmation: str = ""
    reason: str = ""

    @model_validator(mode="after")
    def _validate_setup(self) -> "DokployTargetSetupEnvelope":
        if self.product.strip() != "launchplane":
            raise ValueError("Dokploy target setup requires product 'launchplane'.")
        self.product = "launchplane"
        self.context = self.context.strip()
        self.instance = self.instance.strip()
        self.target_id = self.target_id.strip()
        self.target_name = self.target_name.strip()
        self.project_id = self.project_id.strip()
        self.project_name = self.project_name.strip()
        self.environment_id = self.environment_id.strip()
        self.environment_name = self.environment_name.strip()
        self.server_id = self.server_id.strip()
        self.app_name = self.app_name.strip()
        self.source_git_ref = self.source_git_ref.strip() or "origin/main"
        self.source_type = self.source_type.strip() or "raw"
        self.compose_path = self.compose_path.strip() or "docker-compose.yml"
        self.healthcheck_path = self.healthcheck_path.strip()
        self.confirmation = self.confirmation.strip()
        self.reason = self.reason.strip()
        self.domains = tuple(domain.strip() for domain in self.domains if domain.strip())
        if not self.context:
            raise ValueError("Dokploy target setup requires context.")
        if not self.instance:
            raise ValueError("Dokploy target setup requires instance.")
        if self.operation == "adopt" and not self.target_id:
            raise ValueError("Dokploy target adoption requires target_id.")
        if self.operation == "create-application" and not self.target_name:
            raise ValueError("Dokploy application target creation requires target_name.")
        if self.operation == "create-compose":
            if not self.target_name:
                raise ValueError("Dokploy compose target creation requires target_name.")
            if not self.server_id:
                raise ValueError("Dokploy compose target creation requires server_id.")
        if self.runtime_port is not None and self.operation not in {
            "create-compose",
            "reconcile-compose-domain",
        }:
            raise ValueError(
                "Dokploy target setup runtime_port is only supported for create-compose or reconcile-compose-domain."
            )
        if self.runtime_port is not None and not self.domains:
            raise ValueError("Dokploy target setup runtime_port requires at least one domain.")
        if self.operation == "reconcile-compose-domain":
            if not self.domains:
                raise ValueError("Dokploy compose domain reconciliation requires domains.")
            if self.runtime_port is None:
                raise ValueError("Dokploy compose domain reconciliation requires runtime_port.")
        if self.healthcheck_path and not self.healthcheck_path.startswith("/"):
            raise ValueError("Dokploy target setup healthcheck_path must start with /.")
        return self


class DokployComposeDomainReconcileResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    applied: bool
    target_record: DokployTargetRecord
    target_id_record: DokployTargetIdRecord
    domains: tuple[str, ...]
    runtime_port: int
    route_domain_ids: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


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


def _handle_npmplus_ingress_apply(
    request: NpmplusIngressApplyEnvelope,
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
    ingress_provider_factory: _IngressProviderFactory,
    idempotency_route_path: str = _NPMPLUS_INGRESS_APPLY_ROUTE_PATH,
) -> tuple[dict[str, object], BaseModel | dict[str, object] | None] | list[bytes]:
    del resolved_context, control_plane_root_path, state_dir, database_url, identity
    if request.ingress.mode == "apply" and not request_idempotency_key:
        return _json_response(
            start_response=start_response,
            status_code=400,
            payload={
                "status": "rejected",
                "trace_id": trace_id,
                "error": {
                    "code": "idempotency_key_required",
                    "message": "NPMplus ingress apply requests require an Idempotency-Key header.",
                },
            },
        )

    ingress_provider = ingress_provider_factory()
    try:
        resolved_ingress_request = _resolve_ingress_edge_endpoint(
            record_store=record_store,
            request=request.ingress,
        )
    except click.ClickException as error:
        return _json_response(
            start_response=start_response,
            status_code=400,
            payload={
                "status": "rejected",
                "trace_id": trace_id,
                "error": {
                    "code": "invalid_edge_endpoint",
                    "message": str(error),
                },
            },
        )
    if request.ingress.mode == "apply":
        idempotent_response = _check_idempotent_request(
            record_store=record_store,
            scope=request_scope,
            route_path=idempotency_route_path,
            idempotency_key=request_idempotency_key,
            request_fingerprint=request_fingerprint,
            start_response=start_response,
            trace_id=trace_id,
        )
        if idempotent_response is not None:
            return idempotent_response
        _write_ingress_route_pending_audit_record(
            record_store=record_store,
            trace_id=trace_id,
            product=request.product,
            context=request.context,
            provider=ingress_provider.provider_id,
            request=resolved_ingress_request,
            idempotency_key=request_idempotency_key,
        )
    ingress_result = ingress_provider.apply_route(request=resolved_ingress_request)
    ingress_audit_record = _write_ingress_route_audit_record(
        record_store=record_store,
        trace_id=trace_id,
        product=request.product,
        context=request.context,
        provider=ingress_provider.provider_id,
        request=resolved_ingress_request,
        result=ingress_result,
        idempotency_key=request_idempotency_key,
    )
    return {
        "ingress_provider": ingress_provider.provider_id,
        "ingress_status": ingress_result.status,
        "ingress_dry_run": ingress_result.dry_run,
        "ingress_route_audit_record_id": ingress_audit_record.record_id,
    }, ingress_result


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
    driver_result = build_odoo_artifact_publish_inputs(
        control_plane_root=control_plane_root_path,
        request=request.inputs,
        product_profile=resolved_context.profile,
    )
    return _DescriptorDriverDispatchResult(result=driver_result, driver_result=driver_result)


def _mutate_dokploy_payload_for_target_setup(
    host: str,
    token: str,
    path: str,
    payload: dict[str, control_plane_dokploy.JsonValue],
) -> dict[str, control_plane_dokploy.JsonValue]:
    response = control_plane_dokploy.dokploy_request(
        host=host,
        token=token,
        path=path,
        method="POST",
        payload=payload,
    )
    response_object = control_plane_dokploy.as_json_object(response)
    if response_object is None:
        raise click.ClickException(f"Dokploy API POST {path} returned an invalid response.")
    return response_object


def _fetch_dokploy_target_payload_for_setup(
    host: str,
    token: str,
    target_type: str,
    target_id: str,
) -> dict[str, control_plane_dokploy.JsonValue]:
    return control_plane_dokploy.fetch_dokploy_target_payload(
        host=host,
        token=token,
        target_type=target_type,
        target_id=target_id,
    )


def _dokploy_target_setup_result_payload(
    result: BaseModel,
) -> dict[str, object]:
    payload = result.model_dump(mode="json")
    if isinstance(result, DokployComposeDomainReconcileResult):
        payload.pop("route_domain_ids", None)
    record = payload.get("target_record")
    if isinstance(record, dict):
        env_payload = record.pop("env", None)
        if isinstance(env_payload, dict):
            record["env_keys"] = sorted(str(key) for key in env_payload)
            record["env_value_count"] = len(env_payload)
    return payload


def _execute_dokploy_target_setup(
    *,
    control_plane_root_path: Path,
    record_store: PostgresRecordStore,
    request: DokployTargetSetupEnvelope,
) -> dict[str, object]:
    apply_changes = request.mode == "apply"
    host, token = control_plane_dokploy.read_dokploy_config(
        control_plane_root=control_plane_root_path
    )
    result: (
        DokployTargetAdoptionResult
        | DokployTargetCreateResult
        | DokployComposeTargetCreateResult
        | DokployComposeDomainReconcileResult
    )
    if request.operation == "adopt":
        result = adopt_dokploy_target(
            record_store=record_store,
            host=host,
            token=token,
            context=request.context,
            instance=request.instance,
            target_type=request.target_type,
            target_id=request.target_id,
            project_name=request.project_name,
            target_name=request.target_name,
            source_git_ref=request.source_git_ref,
            healthcheck_path=request.healthcheck_path,
            domains=request.domains,
            deploy_timeout_seconds=request.deploy_timeout_seconds,
            source_label="service:dokploy-targets:setup:adopt",
            apply=apply_changes,
            fetch_target_payload=_fetch_dokploy_target_payload_for_setup,
        )
    elif request.operation == "reconcile-compose-domain":
        result = _execute_dokploy_compose_domain_reconcile(
            record_store=record_store,
            request=request,
            host=host,
            token=token,
            apply_changes=apply_changes,
        )
    elif request.operation == "create-application":
        result = create_dokploy_application_target(
            record_store=record_store,
            host=host,
            token=token,
            context=request.context,
            instance=request.instance,
            target_name=request.target_name,
            project_id=request.project_id,
            project_name=request.project_name,
            project_description=request.project_description,
            environment_id=request.environment_id,
            environment_name=request.environment_name,
            environment_description=request.environment_description,
            server_id=request.server_id,
            app_name=request.app_name,
            application_description=request.description,
            source_git_ref=request.source_git_ref,
            healthcheck_path=request.healthcheck_path,
            domains=request.domains,
            deploy_timeout_seconds=request.deploy_timeout_seconds,
            source_label="service:dokploy-targets:setup:create-application",
            apply=apply_changes,
            mutate_provider=_mutate_dokploy_payload_for_target_setup,
            fetch_target_payload=_fetch_dokploy_target_payload_for_setup,
        )
    else:
        result = create_dokploy_compose_target(
            record_store=record_store,
            host=host,
            token=token,
            context=request.context,
            instance=request.instance,
            target_name=request.target_name,
            project_id=request.project_id,
            project_name=request.project_name,
            project_description=request.project_description,
            environment_id=request.environment_id,
            environment_name=request.environment_name,
            environment_description=request.environment_description,
            server_id=request.server_id,
            app_name=request.app_name,
            compose_description=request.description,
            source_git_ref=request.source_git_ref,
            source_type=request.source_type,
            compose_path=request.compose_path,
            healthcheck_path=request.healthcheck_path,
            domains=request.domains,
            deploy_timeout_seconds=request.deploy_timeout_seconds,
            source_label="service:dokploy-targets:setup:create-compose",
            apply=apply_changes,
            mutate_provider=_mutate_dokploy_payload_for_target_setup,
            fetch_target_payload=_fetch_dokploy_target_payload_for_setup,
        )
    route_domain_ids = (
        list(result.route_domain_ids)
        if isinstance(result, DokployComposeDomainReconcileResult)
        else []
    )
    if apply_changes and request.operation == "create-compose" and request.runtime_port:
        for domain in request.domains:
            route_domain_ids.append(
                control_plane_dokploy.ensure_compose_web_domain_route(
                    host=host,
                    token=token,
                    compose_id=result.target_id_record.target_id,
                    domain_host=domain,
                    runtime_port=request.runtime_port,
                )
            )
    return {
        "mode": request.mode,
        "operation": request.operation,
        "context": request.context,
        "instance": request.instance,
        "applied": apply_changes,
        "route_domain_ids": route_domain_ids,
        "setup": _dokploy_target_setup_result_payload(result),
    }


def _execute_dokploy_compose_domain_reconcile(
    *,
    record_store: PostgresRecordStore,
    request: DokployTargetSetupEnvelope,
    host: str,
    token: str,
    apply_changes: bool,
) -> DokployComposeDomainReconcileResult:
    try:
        target_record = record_store.read_dokploy_target_record(
            context_name=request.context,
            instance_name=request.instance,
        )
        target_id_record = record_store.read_dokploy_target_id_record(
            context_name=request.context,
            instance_name=request.instance,
        )
    except FileNotFoundError as error:
        raise ValueError(
            "Dokploy compose domain reconciliation requires tracked target records."
        ) from error
    if target_record.target_type != "compose":
        raise ValueError("Dokploy compose domain reconciliation requires a compose target.")
    runtime_port = request.runtime_port
    if runtime_port is None:
        raise ValueError("Dokploy compose domain reconciliation requires runtime_port.")
    requested_domains = tuple(dict.fromkeys(request.domains))
    route_domain_ids: list[str] = []
    if apply_changes:
        for domain in requested_domains:
            route_domain_ids.append(
                control_plane_dokploy.ensure_compose_web_domain_route(
                    host=host,
                    token=token,
                    compose_id=target_id_record.target_id,
                    domain_host=domain,
                    runtime_port=runtime_port,
                )
            )
        merged_domains = tuple(dict.fromkeys((*target_record.domains, *requested_domains)))
        if merged_domains != target_record.domains:
            target_record = target_record.model_copy(
                update={
                    "domains": merged_domains,
                    "updated_at": _utc_now_timestamp(),
                    "source_label": ("service:dokploy-targets:setup:reconcile-compose-domain"),
                }
            )
            record_store.write_dokploy_target_record(target_record)
    return DokployComposeDomainReconcileResult(
        applied=apply_changes,
        target_record=target_record,
        target_id_record=target_id_record,
        domains=requested_domains,
        runtime_port=runtime_port,
        route_domain_ids=tuple(route_domain_ids),
        warnings=()
        if apply_changes
        else ("dry run only; Dokploy compose domain routes were not reconciled",),
    )


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
        request=request.run,
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
        _start_odoo_stable_target_replacement_operation_worker(
            operation_id=replacement_operation.operation_id,
            control_plane_root_path=control_plane_root_path,
            record_store=record_store,
            trace_id=trace_id,
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

    _start_odoo_stable_bootstrap_operation_worker(
        operation_id=operation.operation_id,
        control_plane_root_path=control_plane_root_path,
        record_store=record_store,
        trace_id=trace_id,
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
    del resolved_context
    driver_result = execute_verireel_prod_backup_gate(
        control_plane_root=control_plane_root_path,
        record_store=cast(VeriReelProdBackupGateStore, record_store),
        request=request.backup_gate,
        run_async=True,
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


def _descriptor_driver_dispatch_routes(
    *,
    ingress_provider_factory: _IngressProviderFactory | None = None,
) -> dict[str, _DescriptorDriverDispatchRoute[Any]]:
    resolved_ingress_provider_factory = ingress_provider_factory or default_ingress_provider

    def dispatch_npmplus_ingress_apply(
        request: NpmplusIngressApplyEnvelope,
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
        return _handle_npmplus_ingress_apply(
            request,
            resolved_context,
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
            resolved_ingress_provider_factory,
        )

    return {
        _NPMPLUS_INGRESS_APPLY_ROUTE.route_path: _DescriptorDriverDispatchRoute(
            execution_metadata=_NPMPLUS_INGRESS_APPLY_ROUTE,
            context_resolver=lambda request: _DescriptorDriverDispatchContext(
                product=request.product,
                context=request.context,
                use_resolved_profile_product_for_authorization=False,
            ),
            authorization_action_resolver=lambda request: (
                "ingress_route.apply" if request.ingress.mode == "apply" else "ingress_route.plan"
            ),
            custom_dispatch_handler=dispatch_npmplus_ingress_apply,
            skip_pre_idempotency_check=True,
            skip_driver_context_resolution=True,
        ),
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
            _NPMPLUS_INGRESS_APPLY_ROUTE.route_path,
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
    return frozenset()


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
    if len(segments) == 3 and segments == ["v1", "artifacts", "protected"]:
        return "artifact_protection.read", {}
    if len(segments) == 3 and segments[:2] == ["v1", "drivers"]:
        return "driver.read", {"driver_id": segments[2]}
    if (
        len(segments) == 6
        and segments[:4] == ["v1", "drivers", "odoo", "stable-bootstrap"]
        and segments[4] == "operations"
    ):
        return "odoo_stable_bootstrap.execute", {"operation_id": segments[5]}
    if (
        len(segments) == 6
        and segments[:4] == ["v1", "drivers", "odoo", "target-replacement"]
        and segments[4] == "operations"
    ):
        return "odoo_target_replacement_apply.execute", {"operation_id": segments[5]}
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
    if len(segments) == 4 and segments == ["v1", "ingress", "route-audits", "records"]:
        return "ingress_route.plan", {"ingress_route_audit_list": "true"}
    if len(segments) == 5 and segments[:4] == ["v1", "ingress", "route-audits", "records"]:
        return "ingress_route.plan", {"ingress_route_audit_record_id": segments[4]}
    if len(segments) == 3 and segments == ["v1", "edge-endpoints", "records"]:
        return "edge_endpoint.read", {"edge_endpoint_list": "true"}
    if len(segments) == 4 and segments[:3] == ["v1", "edge-endpoints", "records"]:
        return "edge_endpoint.read", {"edge_endpoint_key": segments[3]}
    if len(segments) == 4 and segments == ["v1", "ingress", "canary-routes", "records"]:
        return "ingress_canary_route.read", {"ingress_canary_route_list": "true"}
    if len(segments) == 5 and segments[:4] == ["v1", "ingress", "canary-routes", "records"]:
        return "ingress_canary_route.read", {"ingress_canary_route_key": segments[4]}
    if path == _MERGE_TRAIN_ADMISSION_ROUTE:
        return "merge_train.admission", {}
    if path == _MERGE_TRAIN_CONTROLLER_STATUS_ROUTE:
        return "merge_train.controller_status", {}
    if path == _MERGE_TRAIN_POLICY_TARGETS_ROUTE:
        return "merge_train.policy_targets", {}
    if len(segments) == 3 and segments == ["v1", "work-graph", "snapshot"]:
        return "work_graph.rank", {}
    if len(segments) == 4 and segments == ["v1", "work-graph", "github", "issues"]:
        return "work_graph.issue_inbox", {}
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
    if len(segments) == 4 and segments == [
        "v1",
        "products",
        "public-ingress-monitor",
        "run-once",
    ]:
        return "public_ingress_monitor.run_once", {}
    if len(segments) == 4 and segments == [
        "v1",
        "public-ingress",
        "notification-policies",
        "apply",
    ]:
        return "public_ingress_notification_policy.apply", {}
    if len(segments) == 4 and segments[:2] == ["v1", "products"] and segments[3] == "activity":
        return "product_environment.read", {"product": segments[2], "activity": "true"}
    if len(segments) == 3 and segments[:2] == ["v1", "products"]:
        return "product_environment.read", {"product": segments[2]}
    if len(segments) == 4 and segments[:2] == ["v1", "products"] and segments[3] == "environments":
        return "product_environment.read", {"product": segments[2], "environments": "true"}
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
        if route_metadata.method == "POST"
    )


def _build_write_routes() -> frozenset[str]:
    launchplane_write_routes = {
        _EVERY_CODE_GITHUB_WEBHOOK_ROUTE,
        _MERGE_TRAIN_BATCH_CANDIDATE_RUN_ONCE_ROUTE,
        _MERGE_TRAIN_BATCH_LANDING_RUN_ONCE_ROUTE,
        _MERGE_TRAIN_CONTROLLER_RUN_ONCE_ROUTE,
        _MERGE_TRAIN_PR_FEEDBACK_ROUTE,
        _MERGE_TRAIN_STACK_COLLAPSE_RUN_ONCE_ROUTE,
        _MERGE_TRAIN_RUN_ONCE_ROUTE,
        "/v1/agent/write-intents/evaluate",
        "/v1/every-code/work-requests/create",
        "/v1/every-code/work-requests/claim",
        "/v1/every-code/work-requests/rerun",
        "/v1/every-code/work-requests/status",
        "/v1/every-code/pr-feedback",
        "/v1/every-code/pr-feedback/status",
        "/v1/every-code/preview-gates",
        "/v1/work-graph/github/issues/reconcile",
        "/v1/work-graph/rank",
        "/v1/products/public-ingress-monitor/run-once",
        "/v1/public-ingress/notification-policies/apply",
        "/v1/evidence/deployments",
        "/v1/evidence/backup-gates",
        "/v1/evidence/runner-host-hygiene/audits",
        "/v1/evidence/runner-lane-registration/audits",
        "/v1/evidence/previews/generations",
        "/v1/evidence/previews/destroyed",
        "/v1/authz-policies/github-actions/grants",
        "/v1/authz-policies/github-actions/removals",
        "/v1/authz-policies/github-humans/grants",
        "/v1/authz-policies/terminal-agents/grants",
        "/v1/authz-policies/local-operators/grants",
        "/v1/authz-policies/local-admins/grants",
        "/v1/merge-train/policies/import",
        "/v1/runtime-key-safety/policies/apply",
        "/v1/live-target-runtime/apply",
        "/v1/product-onboarding/apply",
        "/v1/dokploy-targets/setup",
        PROVIDER_TARGET_OPERATIONS_ROUTE,
        _EDGE_ENDPOINT_APPLY_ROUTE,
        _INGRESS_CANARY_ROUTE_RECORD_APPLY_ROUTE,
        _INGRESS_CANARY_ROUTE_APPLY_ROUTE,
        "/v1/product-config/apply",
        "/v1/product-profiles/context-cutover/apply",
        "/v1/product-profiles/legacy-context-cleanup/apply",
        "/v1/previews/desired-state",
        "/v1/previews/pr-feedback",
        "/v1/previews/lifecycle-cleanup",
        "/v1/previews/lifecycle-plan",
        "/v1/previews/lifecycle-sweep",
        "/v1/product-profiles",
        "/v1/evidence/promotions",
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


class _AgentWriteIntentRecordStore(Protocol):
    def write_agent_write_intent_record(self, record: AgentWriteIntentRecord) -> object: ...

    def read_agent_write_intent_record(self, record_id: str) -> AgentWriteIntentRecord: ...

    def list_agent_write_intent_records(
        self,
        *,
        product: str = "",
        context_name: str = "",
        status: str = "",
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[AgentWriteIntentRecord, ...]: ...


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


def _local_operator_product_config_continuity_payload(
    *, payload: dict[str, object]
) -> dict[str, object]:
    canonical_payload = json.loads(json.dumps(payload))
    if isinstance(canonical_payload, dict):
        canonical_payload.pop("mode", None)
        canonical_payload.pop("reason", None)
    return cast(dict[str, object], canonical_payload)


def _local_operator_product_config_dry_run_key(*, payload: dict[str, object]) -> str:
    return "local-operator-product-config-dry-run:" + _request_fingerprint(
        _local_operator_product_config_continuity_payload(payload=payload)
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
        "agent_write_intent_record_id",
        "merge_train_batch_candidate_record_id",
        "merge_train_batch_landing_plan_record_id",
        "merge_train_stack_collapse_plan_record_id",
        "merge_train_run_id",
        "odoo_stable_bootstrap_operation_id",
        "odoo_stable_target_replacement_operation_id",
        "runner_host_hygiene_audit_record_key",
        "runner_lane_registration_audit_record_key",
        "generic_web_rollback_plan_id",
        "public_ingress_notification_policy_id",
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
    if route_path == _NPMPLUS_INGRESS_APPLY_ROUTE.route_path:
        return frozenset({"ingress_provider"})
    if route_path == _INGRESS_CANARY_ROUTE_APPLY_ROUTE:
        return frozenset({"ingress_provider"})
    if route_path == _EDGE_ENDPOINT_APPLY_ROUTE:
        return frozenset({"edge_endpoint_status"})
    if route_path == _INGRESS_CANARY_ROUTE_RECORD_APPLY_ROUTE:
        return frozenset({"ingress_canary_route_status"})
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


def _ingress_route_audit_record_store(record_store: object) -> _IngressRouteAuditRecordStore:
    required_methods = (
        "write_ingress_route_audit_record",
        "read_ingress_route_audit_record",
        "list_ingress_route_audit_records",
    )
    if all(hasattr(record_store, method_name) for method_name in required_methods):
        return cast(_IngressRouteAuditRecordStore, record_store)
    raise click.ClickException(
        "Ingress route audit reads require Launchplane ingress-audit record storage."
    )


def _edge_endpoint_record_store(record_store: object) -> _EdgeEndpointRecordStore:
    required_methods = (
        "write_edge_endpoint_record",
        "read_edge_endpoint_record",
        "list_edge_endpoint_records",
    )
    if all(hasattr(record_store, method_name) for method_name in required_methods):
        return cast(_EdgeEndpointRecordStore, record_store)
    raise click.ClickException("Edge endpoint operations require Launchplane record storage.")


def _ingress_canary_route_record_store(
    record_store: object,
) -> _IngressCanaryRouteRecordStore:
    required_methods = (
        "write_ingress_canary_route_record",
        "read_ingress_canary_route_record",
        "list_ingress_canary_route_records",
    )
    if all(hasattr(record_store, method_name) for method_name in required_methods):
        return cast(_IngressCanaryRouteRecordStore, record_store)
    raise click.ClickException(
        "Ingress canary route operations require Launchplane canary-route record storage."
    )


def _resolve_ingress_edge_endpoint(
    *,
    record_store: object,
    request: NpmplusIngressApplyRequest,
) -> NpmplusIngressApplyRequest:
    endpoint_key = request.route.edge_endpoint_key.strip()
    if not endpoint_key:
        return request
    endpoint_store = _edge_endpoint_record_store(record_store)
    try:
        endpoint = endpoint_store.read_edge_endpoint_record(endpoint_key)
    except FileNotFoundError as exc:
        raise click.ClickException(
            f"Ingress edge endpoint {endpoint_key!r} was not found."
        ) from exc
    if endpoint.status != "active":
        raise click.ClickException(
            f"Ingress edge endpoint {endpoint_key!r} is {endpoint.status}, not active."
        )
    resolved_route = request.route.model_copy(
        update={
            "forward_scheme": endpoint.upstream_scheme,
            "forward_host": endpoint.upstream_host,
            "forward_port": endpoint.upstream_port,
        }
    )
    return request.model_copy(update={"route": resolved_route})


def _active_ingress_canary_route_record(
    *,
    record_store: object,
    canary_key: str,
    product: str,
    context: str,
) -> IngressCanaryRouteRecord:
    canary_store = _ingress_canary_route_record_store(record_store)
    try:
        record = canary_store.read_ingress_canary_route_record(canary_key)
    except FileNotFoundError as exc:
        raise click.ClickException(f"Ingress canary route {canary_key!r} was not found.") from exc
    if record.product != product or record.context != context:
        raise click.ClickException(
            f"Ingress canary route {canary_key!r} is not scoped to {product}/{context}."
        )
    if record.status != "active":
        raise click.ClickException(
            f"Ingress canary route {canary_key!r} is {record.status}, not active."
        )
    return record


def _ingress_request_from_canary_route_record(
    *,
    record: IngressCanaryRouteRecord,
    mode: Literal["dry-run", "apply"],
    reason: str,
) -> NpmplusIngressApplyRequest:
    return NpmplusIngressApplyRequest.model_validate(
        {
            "schema_version": 1,
            "mode": mode,
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


def _filter_ingress_route_audit_records(
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


def _start_odoo_stable_bootstrap_operation_worker(
    *,
    operation_id: str,
    control_plane_root_path: Path,
    record_store: object,
    trace_id: str,
) -> None:
    worker = threading.Thread(
        target=_run_odoo_stable_bootstrap_operation_worker,
        kwargs={
            "operation_id": operation_id,
            "control_plane_root_path": control_plane_root_path,
            "record_store": record_store,
            "trace_id": trace_id,
        },
        name=f"odoo-stable-bootstrap-{operation_id}",
        daemon=True,
    )
    worker.start()


def _start_odoo_stable_target_replacement_operation_worker(
    *,
    operation_id: str,
    control_plane_root_path: Path,
    record_store: object,
    trace_id: str,
) -> None:
    worker = threading.Thread(
        target=_run_odoo_stable_target_replacement_operation_worker,
        kwargs={
            "operation_id": operation_id,
            "control_plane_root_path": control_plane_root_path,
            "record_store": record_store,
            "trace_id": trace_id,
        },
        name=f"odoo-target-replacement-{operation_id}",
        daemon=True,
    )
    worker.start()


def _run_odoo_stable_bootstrap_operation_worker(
    *,
    operation_id: str,
    control_plane_root_path: Path,
    record_store: object,
    trace_id: str,
) -> None:
    operation_store = _odoo_stable_bootstrap_operation_store(record_store)
    operation = operation_store.read_odoo_stable_bootstrap_operation_record(operation_id)
    if odoo_stable_bootstrap_operation_is_terminal(operation):
        return
    started_at = _utc_now_timestamp()
    running_operation = operation.model_copy(
        update={
            "status": "running",
            "phase": "running",
            "started_at": operation.started_at or started_at,
            "updated_at": started_at,
            "runner_trace_id": trace_id,
        }
    )
    operation_store.write_odoo_stable_bootstrap_operation_record(running_operation)
    try:
        result = execute_odoo_stable_bootstrap(
            control_plane_root=control_plane_root_path,
            record_store=cast(OdooStableBootstrapStore, record_store),
            request=running_operation.request,
        )
    except Exception as error:
        logging.exception(
            "Odoo stable bootstrap operation %s failed before producing a result.",
            operation_id,
        )
        finished_at = _utc_now_timestamp()
        failed_operation = running_operation.model_copy(
            update={
                "status": "fail",
                "phase": "failed",
                "updated_at": finished_at,
                "finished_at": finished_at,
                "error_message": str(error),
            }
        )
        operation_store.write_odoo_stable_bootstrap_operation_record(failed_operation)
        return
    finished_at = _utc_now_timestamp()
    passed = _odoo_stable_bootstrap_operation_result_passed(result)
    terminal_operation = running_operation.model_copy(
        update={
            "status": "pass" if passed else "fail",
            "phase": "completed" if passed else "failed",
            "deployment_record_id": result.deployment_record_id,
            "updated_at": finished_at,
            "finished_at": finished_at,
            "result": result,
            "error_message": ""
            if passed
            else (result.error_message or "Odoo stable bootstrap failed."),
        }
    )
    operation_store.write_odoo_stable_bootstrap_operation_record(terminal_operation)


def _run_odoo_stable_target_replacement_operation_worker(
    *,
    operation_id: str,
    control_plane_root_path: Path,
    record_store: object,
    trace_id: str,
) -> None:
    operation_store = _odoo_stable_target_replacement_operation_store(record_store)
    operation = operation_store.read_odoo_stable_target_replacement_operation_record(operation_id)
    if odoo_stable_target_replacement_operation_is_terminal(operation):
        return
    started_at = _utc_now_timestamp()
    running_operation = operation.model_copy(
        update={
            "status": "running",
            "phase": "running",
            "started_at": operation.started_at or started_at,
            "updated_at": started_at,
            "runner_trace_id": trace_id,
        }
    )
    operation_store.write_odoo_stable_target_replacement_operation_record(running_operation)
    try:
        result = execute_odoo_stable_target_replacement_apply(
            control_plane_root=control_plane_root_path,
            record_store=cast(OdooStableTargetReplacementStore, record_store),
            request=running_operation.request,
        )
    except Exception as error:
        logging.exception(
            "Odoo stable target replacement operation %s failed before producing a result.",
            operation_id,
        )
        finished_at = _utc_now_timestamp()
        failed_operation = running_operation.model_copy(
            update={
                "status": "fail",
                "phase": "failed",
                "updated_at": finished_at,
                "finished_at": finished_at,
                "error_message": str(error),
            }
        )
        operation_store.write_odoo_stable_target_replacement_operation_record(failed_operation)
        return
    finished_at = _utc_now_timestamp()
    passed = _odoo_stable_target_replacement_operation_result_passed(result)
    terminal_operation = running_operation.model_copy(
        update={
            "status": "pass" if passed else "fail",
            "phase": "completed" if passed else "failed",
            "deployment_record_id": result.deployment_record_id,
            "updated_at": finished_at,
            "finished_at": finished_at,
            "result": result,
            "error_message": ""
            if passed
            else (result.error_message or "Odoo stable target replacement failed."),
        }
    )
    operation_store.write_odoo_stable_target_replacement_operation_record(terminal_operation)


def _odoo_stable_bootstrap_operation_result_passed(
    result: OdooStableBootstrapResult,
) -> bool:
    return (
        result.bootstrap_status == "pass"
        and result.post_deploy_status == "pass"
        and result.health_status != "fail"
        and result.canonical_status != "fail"
        and result.logo_status != "fail"
    )


def _odoo_stable_target_replacement_operation_result_passed(
    result: OdooStableTargetReplacementApplyResult,
) -> bool:
    return (
        result.deploy_status == "pass"
        and result.post_deploy_status == "pass"
        and result.health_status != "fail"
        and result.canonical_status != "fail"
        and result.logo_status != "fail"
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


def _write_local_operator_product_config_dry_run_record(
    *,
    record_store: object,
    scope: str,
    request_payload: dict[str, object],
    response_trace_id: str,
    response_payload: dict[str, object],
) -> None:
    _write_idempotency_record(
        record_store=record_store,
        scope=scope,
        route_path="/v1/product-config/apply",
        idempotency_key=_local_operator_product_config_dry_run_key(payload=request_payload),
        request_fingerprint=_request_fingerprint(
            _local_operator_product_config_continuity_payload(payload=request_payload)
        ),
        response_status_code=202,
        response_trace_id=f"{response_trace_id}-local-operator-dry-run",
        response_payload=response_payload,
    )


def _local_operator_product_config_dry_run_exists(
    *, record_store: object, scope: str, request_payload: dict[str, object]
) -> bool:
    stored_record = _read_idempotency_record(
        record_store=record_store,
        scope=scope,
        route_path="/v1/product-config/apply",
        idempotency_key=_local_operator_product_config_dry_run_key(payload=request_payload),
    )
    if stored_record is None:
        return False
    return stored_record.request_fingerprint == _request_fingerprint(
        _local_operator_product_config_continuity_payload(payload=request_payload)
    )


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


def _public_ingress_managed_secret_resolver(
    *,
    record_store: control_plane_secrets.SecretReadStore,
) -> Callable[[str, PublicIngressIncidentRecord], str]:
    def resolve(secret_id: str, incident: PublicIngressIncidentRecord) -> str:
        normalized_secret_id = secret_id.strip()
        if not normalized_secret_id:
            return ""
        try:
            record = record_store.read_secret_record(normalized_secret_id)
        except Exception:  # noqa: BLE001 - delivery records capture missing secrets per destination.
            return ""
        if record.status != control_plane_secrets.SECRET_STATUS_CONFIGURED:
            return ""
        if not control_plane_secrets._scope_matches_record(
            record,
            context_name=incident.context,
            instance_name=incident.instance,
        ):
            return ""
        try:
            version = record_store.read_secret_version(record.current_version_id)
            return control_plane_secrets._decrypt_secret_value(version.ciphertext)
        except Exception:  # noqa: BLE001 - delivery records capture unreadable secrets per destination.
            return ""

    return resolve


def _public_ingress_notification_drivers(
    *,
    record_store: object,
) -> PublicIngressNotificationDriverSet:
    secret_store = _secret_capable_store(record_store)
    if secret_store is None:
        return PublicIngressNotificationDriverSet()
    return PublicIngressNotificationDriverSet(
        incident_secret_resolver=_public_ingress_managed_secret_resolver(record_store=secret_store)
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
    if path == _NPMPLUS_INGRESS_APPLY_ROUTE.route_path:
        if isinstance(driver_result, BaseModel):
            ingress_result = driver_result.model_dump(mode="json")
        else:
            ingress_result = driver_result or {}
        if ingress_result.get("dry_run") is True:
            return False
    if (
        path
        in {
            "/v1/authz-policies/github-actions/grants",
            "/v1/authz-policies/github-actions/removals",
            "/v1/authz-policies/github-humans/grants",
            "/v1/authz-policies/terminal-agents/grants",
            "/v1/authz-policies/local-operators/grants",
            "/v1/authz-policies/local-admins/grants",
        }
        and isinstance(driver_result, dict)
        and driver_result.get("mode") == "dry_run"
    ):
        return False
    if (
        path == "/v1/merge-train/policies/import"
        and isinstance(driver_result, dict)
        and driver_result.get("mode") == "dry_run"
    ):
        return False
    if (
        path == _INGRESS_CANARY_ROUTE_RECORD_APPLY_ROUTE
        and isinstance(driver_result, dict)
        and driver_result.get("mode") == "dry-run"
    ):
        return False
    if (
        path == PROVIDER_TARGET_OPERATIONS_ROUTE
        and isinstance(driver_result, dict)
        and driver_result.get("mode") != "backfill-apply"
    ):
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


def _write_ingress_route_audit_record(
    *,
    record_store: object,
    trace_id: str,
    product: str,
    context: str,
    provider: str,
    request: NpmplusIngressApplyRequest,
    result: NpmplusIngressApplyResult,
    idempotency_key: str,
) -> IngressRouteAuditRecord:
    write_record = getattr(record_store, "write_ingress_route_audit_record", None)
    if not callable(write_record):
        raise RuntimeError("Ingress route audit record storage is unavailable")
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
        recorded_at=_utc_now_timestamp(),
    )
    write_record(record)
    return record


def _edge_endpoint_apply_result(
    *,
    record_store: object,
    request: EdgeEndpointApplyEnvelope,
) -> tuple[dict[str, object], dict[str, object]]:
    endpoint_store = _edge_endpoint_record_store(record_store)
    if request.mode == "apply":
        endpoint_store.write_edge_endpoint_record(request.endpoint)
        status = "applied"
    else:
        status = "planned"
    return {
        "edge_endpoint_key": request.endpoint.endpoint_key,
        "edge_endpoint_status": status,
        "mode": request.mode,
        "reason": request.reason,
    }, {
        "mode": request.mode,
        "endpoint_key": request.endpoint.endpoint_key,
        "endpoint_status": status,
        "record": request.endpoint.model_dump(mode="json"),
    }


def _ingress_canary_route_record_apply_result(
    *,
    record_store: object,
    request: IngressCanaryRouteRecordApplyEnvelope,
) -> tuple[dict[str, object], dict[str, object]]:
    canary_store = _ingress_canary_route_record_store(record_store)
    if request.mode == "apply":
        canary_store.write_ingress_canary_route_record(request.route)
        status = "applied"
    else:
        status = "planned"
    return {
        "ingress_canary_route_key": request.route.canary_key,
        "ingress_canary_route_status": status,
        "mode": request.mode,
        "reason": request.reason,
    }, {
        "mode": request.mode,
        "canary_key": request.route.canary_key,
        "route_status": status,
        "record": request.route.model_dump(mode="json"),
    }


def _write_ingress_route_pending_audit_record(
    *,
    record_store: object,
    trace_id: str,
    product: str,
    context: str,
    provider: str,
    request: NpmplusIngressApplyRequest,
    idempotency_key: str,
) -> IngressRouteAuditRecord:
    write_record = getattr(record_store, "write_ingress_route_audit_record", None)
    if not callable(write_record):
        raise RuntimeError("Ingress route audit record storage is unavailable")
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
        recorded_at=_utc_now_timestamp(),
    )
    write_record(record)
    return record


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


def _required_bearer_identity_env_value(value: str, env_var_name: str) -> str:
    if not value:
        raise PermissionError(f"{env_var_name} is required for configured bearer auth.")
    return value


def _local_operator_identity_from_bearer(
    environ: dict[str, object],
) -> LocalOperatorIdentity | None:
    expected_token = _local_operator_token_from_env()
    if not expected_token:
        return None
    try:
        provided_token = _bearer_token(environ)
    except PermissionError:
        return None
    if not secrets.compare_digest(provided_token, expected_token):
        return None
    subject = _required_bearer_identity_env_value(
        _local_operator_subject_from_env(), "LAUNCHPLANE_LOCAL_OPERATOR_SUBJECT"
    )
    token_label = _required_bearer_identity_env_value(
        _local_operator_token_label_from_env(),
        "LAUNCHPLANE_LOCAL_OPERATOR_TOKEN_LABEL",
    )
    return LocalOperatorIdentity(subject=subject, token_label=token_label)


def _local_admin_identity_from_bearer(
    environ: dict[str, object],
) -> LocalAdminIdentity | None:
    expected_token = _local_admin_token_from_env()
    if not expected_token:
        return None
    try:
        provided_token = _bearer_token(environ)
    except PermissionError:
        return None
    if not secrets.compare_digest(provided_token, expected_token):
        return None
    subject = _required_bearer_identity_env_value(
        _local_admin_subject_from_env(), "LAUNCHPLANE_LOCAL_ADMIN_SUBJECT"
    )
    token_label = _required_bearer_identity_env_value(
        _local_admin_token_label_from_env(), "LAUNCHPLANE_LOCAL_ADMIN_TOKEN_LABEL"
    )
    return LocalAdminIdentity(subject=subject, token_label=token_label)


def _terminal_agent_identity_from_bearer(
    environ: dict[str, object],
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
    subject = _required_bearer_identity_env_value(
        _terminal_agent_subject_from_env(), "LAUNCHPLANE_TERMINAL_AGENT_SUBJECT"
    )
    token_label = _required_bearer_identity_env_value(
        _terminal_agent_token_label_from_env(),
        "LAUNCHPLANE_TERMINAL_AGENT_TOKEN_LABEL",
    )
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
        "/v1/every-code/pr-feedback",
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
    idempotency_key: str = "",
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
    if path == "/v1/every-code/pr-feedback":
        feedback_record = EveryCodePrFeedbackEnvelope.model_validate(payload)
        every_code_store.write_every_code_pr_feedback_record(feedback_record)
        return _json_response(
            start_response=start_response,
            status_code=202,
            payload=_accepted_payload(
                trace_id=trace_id,
                result={
                    "request_id": feedback_record.request_id,
                    "feedback_id": feedback_record.feedback_id,
                    "status": feedback_record.status,
                },
                driver_result={"feedback": feedback_record.model_dump(mode="json")},
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
        rerun_checked_at = datetime.now(timezone.utc)
        intent_record, intent_response = _validate_every_code_rerun_write_intent(
            record_store=record_store,
            rerun_request=rerun_request,
            idempotency_key=idempotency_key,
            now=rerun_checked_at,
            trace_id=trace_id,
            start_response=start_response,
        )
        if intent_response is not None:
            return intent_response
        existing_record = every_code_store.read_every_code_work_request_record(
            rerun_request.request_id.strip()
        )
        if intent_record is None:
            intent_record = _matching_every_code_rerun_intent_record(
                record_store=record_store,
                source_url=existing_record.issue_url,
                now=rerun_checked_at,
            )
            if intent_record is None:
                return _reject_agent_write_intent(
                    start_response=start_response,
                    trace_id=trace_id,
                    code="agent_write_intent_required",
                    message="Every Code rerun requires matching approved write-intent evidence.",
                )
        if rerun_request.source_url and rerun_request.source_url != existing_record.issue_url:
            return _reject_agent_write_intent(
                start_response=start_response,
                trace_id=trace_id,
                code="agent_write_intent_source_mismatch",
                message="Every Code rerun source_url does not match the work-request issue URL.",
                record_id=intent_record.record_id,
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
                result={
                    "request_id": requeued_record.request_id,
                    "state": requeued_record.state,
                    **(
                        {"agent_write_intent_record_id": intent_record.record_id}
                        if intent_record is not None
                        else {}
                    ),
                },
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


def _agent_write_intent_rejection_payload(
    *, trace_id: str, code: str, message: str, record_id: str = ""
) -> dict[str, object]:
    payload: dict[str, object] = {
        "status": "rejected",
        "trace_id": trace_id,
        "error": {"code": code, "message": message},
    }
    if record_id:
        payload["records"] = {"agent_write_intent_record_id": record_id}
    return payload


def _reject_agent_write_intent(
    *,
    start_response: _StartResponse,
    trace_id: str,
    code: str,
    message: str,
    record_id: str = "",
) -> list[bytes]:
    status_code = 404 if code == "agent_write_intent_not_found" else 409
    return _json_response(
        start_response=start_response,
        status_code=status_code,
        payload=_agent_write_intent_rejection_payload(
            trace_id=trace_id,
            code=code,
            message=message,
            record_id=record_id,
        ),
    )


def _validate_every_code_rerun_write_intent(
    *,
    record_store: object,
    rerun_request: EveryCodeWorkRequestRerunEnvelope,
    idempotency_key: str,
    now: datetime,
    trace_id: str,
    start_response: _StartResponse,
) -> tuple[AgentWriteIntentRecord | None, list[bytes] | None]:
    record_id = rerun_request.agent_write_intent_record_id.strip()
    if not record_id:
        return None, None
    try:
        record = _agent_write_intent_record_store(record_store).read_agent_write_intent_record(
            record_id
        )
    except FileNotFoundError:
        return None, _reject_agent_write_intent(
            start_response=start_response,
            trace_id=trace_id,
            code="agent_write_intent_not_found",
            message="Agent write-intent evidence record was not found.",
            record_id=record_id,
        )
    if (
        record.evaluation.status != "allowed"
        or record.evaluation.intent != "every_code_rerun"
        or record.evaluation.mode != "apply"
        or not record.evaluation.safe_to_execute
    ):
        return record, _reject_agent_write_intent(
            start_response=start_response,
            trace_id=trace_id,
            code="agent_write_intent_not_executable",
            message="Every Code rerun requires an allowed apply-mode every_code_rerun intent record.",
            record_id=record.record_id,
        )
    if (
        record.evaluation.product != "launchplane"
        or record.evaluation.context != _LAUNCHPLANE_SERVICE_CONTEXT
    ):
        return record, _reject_agent_write_intent(
            start_response=start_response,
            trace_id=trace_id,
            code="agent_write_intent_scope_mismatch",
            message="Agent write-intent evidence does not match the Every Code rerun product/context.",
            record_id=record.record_id,
        )
    if record.evaluation.authz_action != "every_code_work_request.rerun":
        return record, _reject_agent_write_intent(
            start_response=start_response,
            trace_id=trace_id,
            code="agent_write_intent_action_mismatch",
            message="Agent write-intent evidence was evaluated for a different route action.",
            record_id=record.record_id,
        )
    if rerun_request.source_url and rerun_request.source_url != record.request.source_url:
        return record, _reject_agent_write_intent(
            start_response=start_response,
            trace_id=trace_id,
            code="agent_write_intent_source_mismatch",
            message="Every Code rerun source_url does not match the write-intent source_url.",
            record_id=record.record_id,
        )
    if idempotency_key and record.idempotency_key and idempotency_key != record.idempotency_key:
        return record, _reject_agent_write_intent(
            start_response=start_response,
            trace_id=trace_id,
            code="agent_write_intent_idempotency_mismatch",
            message="Every Code rerun idempotency key does not match the write-intent evidence.",
            record_id=record.record_id,
        )
    try:
        recorded_at = _parse_utc_timestamp(record.recorded_at)
    except ValueError:
        return record, _reject_agent_write_intent(
            start_response=start_response,
            trace_id=trace_id,
            code="agent_write_intent_stale",
            message="Agent write-intent evidence timestamp is invalid or stale.",
            record_id=record.record_id,
        )
    if recorded_at > now or now - recorded_at > _AGENT_WRITE_INTENT_MAX_AGE:
        return record, _reject_agent_write_intent(
            start_response=start_response,
            trace_id=trace_id,
            code="agent_write_intent_stale",
            message="Agent write-intent evidence is too old for Every Code rerun execution.",
            record_id=record.record_id,
        )
    return record, None


def _matching_every_code_rerun_intent_record(
    *,
    record_store: object,
    source_url: str,
    now: datetime,
) -> AgentWriteIntentRecord | None:
    for record in _agent_write_intent_record_store(record_store).list_agent_write_intent_records(
        product="launchplane",
        context_name=_LAUNCHPLANE_SERVICE_CONTEXT,
        status="allowed",
        limit=50,
    ):
        if record.evaluation.intent != "every_code_rerun":
            continue
        if record.evaluation.mode != "apply" or not record.evaluation.safe_to_execute:
            continue
        if record.evaluation.authz_action != "every_code_work_request.rerun":
            continue
        if record.request.source_url != source_url:
            continue
        try:
            recorded_at = _parse_utc_timestamp(record.recorded_at)
        except ValueError:
            continue
        if recorded_at <= now and now - recorded_at <= _AGENT_WRITE_INTENT_MAX_AGE:
            return record
    return None


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
    local_admin_identity = _local_admin_identity_from_bearer(environ)
    if local_admin_identity is not None:
        return local_admin_identity
    local_operator_identity = _local_operator_identity_from_bearer(environ)
    if local_operator_identity is not None:
        return local_operator_identity
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


def _authz_policy_grant_database_required_response(
    *, trace_id: str, principal_label: str, start_response: _StartResponse
) -> list[bytes]:
    return _json_response(
        start_response=start_response,
        status_code=503,
        payload={
            "status": "rejected",
            "trace_id": trace_id,
            "error": {
                "code": "database_required",
                "message": (
                    f"Authz {principal_label} policy grant writes require Launchplane database storage."
                ),
            },
        },
    )


def _authz_policy_grant_denied_response(
    *, trace_id: str, principal_label: str, start_response: _StartResponse
) -> list[bytes]:
    return _json_response(
        start_response=start_response,
        status_code=403,
        payload={
            "status": "rejected",
            "trace_id": trace_id,
            "error": {
                "code": "authorization_denied",
                "message": (
                    f"Workflow cannot write Launchplane authz {principal_label} policy grants."
                ),
            },
        },
    )


def _authz_policy_unavailable_response(
    *, trace_id: str, start_response: _StartResponse
) -> list[bytes]:
    return _json_response(
        start_response=start_response,
        status_code=503,
        payload={
            "status": "rejected",
            "trace_id": trace_id,
            "error": {
                "code": "authz_policy_unavailable",
                "message": "Launchplane active authz policy is unavailable.",
            },
        },
    )


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
        "docker_image_reference": os.environ.get(LAUNCHPLANE_IMAGE_REFERENCE_ENV_KEY, "").strip(),
        "service_audience": os.environ.get("LAUNCHPLANE_SERVICE_AUDIENCE", "").strip(),
        "storage_backend": storage_backend,
    }


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
    work_graph_planning_facts_provider: WorkGraphPlanningFactsProvider | None = None,
    work_graph_issue_inbox_provider: WorkGraphIssueInboxProvider | None = None,
    work_graph_issue_inbox_reconcile_provider: WorkGraphIssueInboxReconcileProvider | None = None,
    ingress_provider_factory: _IngressProviderFactory | None = None,
    npmplus_ingress_client_factory: _NpmplusIngressClientFactory | None = None,
) -> _WsgiApp:
    resolved_root = control_plane_root_path or control_plane_root()
    ui_static_root = resolved_root / "control_plane" / "ui_static"
    record_store = cast(
        PostgresRecordStore,
        local_record_store_for_tests or build_shared_record_store(database_url=database_url),
    )
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
    resolved_ingress_provider_factory = ingress_provider_factory
    if resolved_ingress_provider_factory is None:
        if npmplus_ingress_client_factory is not None:

            def npmplus_ingress_provider_from_client_factory() -> IngressProvider:
                return NpmplusIngressProvider(client=npmplus_ingress_client_factory())

            resolved_ingress_provider_factory = npmplus_ingress_provider_from_client_factory
        else:
            resolved_ingress_provider_factory = default_ingress_provider
    descriptor_driver_dispatch_routes = _descriptor_driver_dispatch_routes(
        ingress_provider_factory=resolved_ingress_provider_factory,
    )
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
                    idempotency_key=_idempotency_key(environ),
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
                    on_renewed_session=record_renewed_session,
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
                        on_renewed_session=record_renewed_session,
                    )
                else:
                    local_admin_identity = _local_admin_identity_from_bearer(environ)
                    if local_admin_identity is not None:
                        identity = local_admin_identity
                    else:
                        local_operator_identity = _local_operator_identity_from_bearer(environ)
                        if local_operator_identity is not None:
                            identity = local_operator_identity
                        else:
                            token = _bearer_token(environ)
                            identity = verifier.verify(token)
                            if not isinstance(identity, GitHubActionsIdentity):
                                raise PermissionError(
                                    "Mutation routes require GitHub Actions OIDC."
                                )
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
                if action == "odoo_stable_bootstrap.execute":
                    operation_id = params["operation_id"]
                    operation_store = _odoo_stable_bootstrap_operation_store(record_store)
                    try:
                        operation = operation_store.read_odoo_stable_bootstrap_operation_record(
                            operation_id
                        )
                    except FileNotFoundError:
                        return _not_found_response(
                            start_response=start_response,
                            trace_id=request_trace_id,
                            path=path,
                        )
                    if not authz_policy.allows(
                        identity=identity,
                        action=action,
                        product=operation.product,
                        context=operation.context,
                    ):
                        return _json_response(
                            start_response=start_response,
                            status_code=403,
                            payload={
                                "status": "rejected",
                                "trace_id": request_trace_id,
                                "error": {
                                    "code": "authorization_denied",
                                    "message": "Workflow cannot read Odoo stable bootstrap operation status for the requested product/context.",
                                },
                            },
                        )
                    return _json_response(
                        start_response=start_response,
                        status_code=200,
                        payload={
                            "status": "ok",
                            "trace_id": request_trace_id,
                            "operation": _operation_payload(operation),
                            **(
                                {"result": operation.result.model_dump(mode="json")}
                                if operation.result is not None
                                else {}
                            ),
                        },
                    )
                if action == "odoo_target_replacement_apply.execute":
                    replacement_operation_id = params["operation_id"]
                    replacement_operation_store = _odoo_stable_target_replacement_operation_store(
                        record_store
                    )
                    try:
                        replacement_operation = replacement_operation_store.read_odoo_stable_target_replacement_operation_record(
                            replacement_operation_id
                        )
                    except FileNotFoundError:
                        return _not_found_response(
                            start_response=start_response,
                            trace_id=request_trace_id,
                            path=path,
                        )
                    if not authz_policy.allows(
                        identity=identity,
                        action=action,
                        product=replacement_operation.product,
                        context=replacement_operation.context,
                    ):
                        return _json_response(
                            start_response=start_response,
                            status_code=403,
                            payload={
                                "status": "rejected",
                                "trace_id": request_trace_id,
                                "error": {
                                    "code": "authorization_denied",
                                    "message": "Workflow cannot read Odoo target replacement operation status for the requested product/context.",
                                },
                            },
                        )
                    return _json_response(
                        start_response=start_response,
                        status_code=200,
                        payload={
                            "status": "ok",
                            "trace_id": request_trace_id,
                            "operation": _target_replacement_operation_payload(
                                replacement_operation
                            ),
                            **(
                                {"result": replacement_operation.result.model_dump(mode="json")}
                                if replacement_operation.result is not None
                                else {}
                            ),
                        },
                    )
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
                if action == "artifact_protection.read":
                    requested_product = _query_string_value(query, "product")
                    requested_context = _query_string_value(query, "context")
                    if not requested_product:
                        return _json_response(
                            start_response=start_response,
                            status_code=400,
                            payload={
                                "status": "rejected",
                                "trace_id": request_trace_id,
                                "error": {
                                    "code": "invalid_query",
                                    "message": "Protected artifact inventory requires a product query parameter.",
                                },
                            },
                        )
                    authz_product = requested_product
                    authz_context = requested_context or _WHOLE_PRODUCT_CONTEXT
                    if not authz_policy.allows(
                        identity=identity,
                        action=action,
                        product=authz_product,
                        context=authz_context,
                    ):
                        return _json_response(
                            start_response=start_response,
                            status_code=403,
                            payload={
                                "status": "rejected",
                                "trace_id": request_trace_id,
                                "error": {
                                    "code": "authorization_denied",
                                    "message": "Workflow cannot read protected artifact inventory.",
                                },
                                "authz": _authz_diagnostic_payload(
                                    identity=identity,
                                    authz_policy_sha256_value=resolved_authz_policy_sha256,
                                    authz_policy_source=resolved_authz_policy_source,
                                    action=action,
                                    product=authz_product,
                                    context=authz_context,
                                ),
                            },
                        )
                    protected = build_protected_artifact_set(
                        record_store,
                        product=requested_product,
                        context_name=requested_context,
                    )
                    return _json_response(
                        start_response=start_response,
                        status_code=200,
                        payload={
                            "status": "ok",
                            "trace_id": request_trace_id,
                            "protected_artifacts": protected.model_dump(mode="json"),
                        },
                    )
                if action == "ingress_route.plan":
                    audit_store = _ingress_route_audit_record_store(record_store)
                    if "ingress_route_audit_record_id" in params:
                        product = _query_string_value(query, "product")
                        context_name = _query_string_value(query, "context")
                        if not product or not context_name:
                            return _json_response(
                                start_response=start_response,
                                status_code=400,
                                payload={
                                    "status": "rejected",
                                    "trace_id": request_trace_id,
                                    "error": {
                                        "code": "invalid_query",
                                        "message": "Ingress route audit record reads require product and context query parameters.",
                                    },
                                },
                            )
                        if not authz_policy.allows(
                            identity=identity,
                            action=action,
                            product=product,
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
                                        "message": "Workflow cannot read ingress route audit records for the requested product/context.",
                                    },
                                },
                            )
                        try:
                            audit_record = audit_store.read_ingress_route_audit_record(
                                params["ingress_route_audit_record_id"]
                            )
                        except FileNotFoundError:
                            return _not_found_response(
                                start_response=start_response,
                                trace_id=request_trace_id,
                                path=path,
                            )
                        if audit_record.product != product or audit_record.context != context_name:
                            return _not_found_response(
                                start_response=start_response,
                                trace_id=request_trace_id,
                                path=path,
                            )
                        return _json_response(
                            start_response=start_response,
                            status_code=200,
                            payload={
                                "status": "ok",
                                "trace_id": request_trace_id,
                                "record": audit_record.model_dump(mode="json"),
                            },
                        )
                    product = _query_string_value(query, "product")
                    context_name = _query_string_value(query, "context")
                    if not product or not context_name:
                        return _json_response(
                            start_response=start_response,
                            status_code=400,
                            payload={
                                "status": "rejected",
                                "trace_id": request_trace_id,
                                "error": {
                                    "code": "invalid_query",
                                    "message": "Ingress route audit list requires product and context query parameters.",
                                },
                            },
                        )
                    if not authz_policy.allows(
                        identity=identity,
                        action=action,
                        product=product,
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
                                    "message": "Workflow cannot read ingress route audit records for the requested product/context.",
                                },
                            },
                        )
                    limit = _query_int_value(query, "limit", default=25, minimum=1, maximum=100)
                    provider_host_id = _query_int_value(query, "provider_host_id", minimum=1)
                    listed_records = audit_store.list_ingress_route_audit_records(
                        product=product,
                        context_name=context_name,
                        limit=None,
                    )
                    listed_records = _filter_ingress_route_audit_records(
                        listed_records,
                        status=_query_string_value(query, "status"),
                        mode=_query_string_value(query, "mode"),
                        provider_host_id=provider_host_id,
                        trace_id=_query_string_value(query, "trace_id"),
                        idempotency_key=_query_string_value(query, "idempotency_key"),
                    )
                    limited_records = listed_records[:limit]
                    return _json_response(
                        start_response=start_response,
                        status_code=200,
                        payload={
                            "status": "ok",
                            "trace_id": request_trace_id,
                            "product": product,
                            "context": context_name,
                            "limit": limit,
                            "count": len(limited_records),
                            "records": [
                                record.model_dump(mode="json") for record in limited_records
                            ],
                        },
                    )
                if action == "edge_endpoint.read":
                    endpoint_store = _edge_endpoint_record_store(record_store)
                    if "edge_endpoint_key" in params:
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
                                        "message": "Workflow cannot read Launchplane edge endpoint records.",
                                    },
                                },
                            )
                        try:
                            endpoint_record = endpoint_store.read_edge_endpoint_record(
                                params["edge_endpoint_key"]
                            )
                        except FileNotFoundError:
                            return _not_found_response(
                                start_response=start_response,
                                trace_id=request_trace_id,
                                path=path,
                            )
                        return _json_response(
                            start_response=start_response,
                            status_code=200,
                            payload={
                                "status": "ok",
                                "trace_id": request_trace_id,
                                "record": endpoint_record.model_dump(mode="json"),
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
                                    "message": "Workflow cannot read Launchplane edge endpoint records.",
                                },
                            },
                        )
                    limit = _query_int_value(query, "limit", default=25, minimum=1, maximum=100)
                    endpoint_records = endpoint_store.list_edge_endpoint_records(
                        provider=_query_string_value(query, "provider"),
                        status=_query_string_value(query, "status"),
                        limit=limit,
                    )
                    return _json_response(
                        start_response=start_response,
                        status_code=200,
                        payload={
                            "status": "ok",
                            "trace_id": request_trace_id,
                            "limit": limit,
                            "count": len(endpoint_records),
                            "records": [
                                record.model_dump(mode="json") for record in endpoint_records
                            ],
                        },
                    )
                if action == "ingress_canary_route.read":
                    canary_store = _ingress_canary_route_record_store(record_store)
                    if "ingress_canary_route_key" in params:
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
                                        "message": "Workflow cannot read Launchplane ingress canary route records.",
                                    },
                                },
                            )
                        try:
                            canary_record = canary_store.read_ingress_canary_route_record(
                                params["ingress_canary_route_key"]
                            )
                        except FileNotFoundError:
                            return _not_found_response(
                                start_response=start_response,
                                trace_id=request_trace_id,
                                path=path,
                            )
                        return _json_response(
                            start_response=start_response,
                            status_code=200,
                            payload={
                                "status": "ok",
                                "trace_id": request_trace_id,
                                "record": canary_record.model_dump(mode="json"),
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
                                    "message": "Workflow cannot read Launchplane ingress canary route records.",
                                },
                            },
                        )
                    limit = _query_int_value(query, "limit", default=25, minimum=1, maximum=100)
                    canary_records = canary_store.list_ingress_canary_route_records(
                        product=_query_string_value(query, "product"),
                        context_name=_query_string_value(query, "context"),
                        status=_query_string_value(query, "status"),
                        limit=limit,
                    )
                    return _json_response(
                        start_response=start_response,
                        status_code=200,
                        payload={
                            "status": "ok",
                            "trace_id": request_trace_id,
                            "limit": limit,
                            "count": len(canary_records),
                            "records": [
                                record.model_dump(mode="json") for record in canary_records
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
                if action == "work_graph.issue_inbox":
                    return handle_work_graph_issue_inbox_read(
                        authz_policy=authz_policy,
                        identity=identity,
                        trace_id=request_trace_id,
                        issue_inbox_provider=work_graph_issue_inbox_provider,
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
                    policy_record = resolve_merge_train_policy_record(record_store)
                    policy = policy_record.policy
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
                        current_policy_key=repository_policy.policy_key,
                        current_policy_sha256=policy_record.policy_sha256,
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
                if action == "merge_train.controller_status":
                    status_request = MergeTrainAdmissionEnvelope.model_validate(
                        {
                            "repository": str((query.get("repository") or [""])[0] or ""),
                            "base_branch": str((query.get("base_branch") or ["main"])[0] or ""),
                        }
                    )
                    policy_record = resolve_merge_train_policy_record(record_store)
                    policy = policy_record.policy
                    repository_policy = policy.find_repository_policy(
                        repository=status_request.repository,
                        base_branch=status_request.base_branch,
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
                                    "message": "Workflow cannot read the requested merge train controller status.",
                                },
                            },
                        )
                    read_model = build_merge_train_controller_status_read_model(
                        store=record_store,
                        repository=status_request.repository,
                        base_branch=status_request.base_branch,
                        generated_at=_utc_now_timestamp(),
                        current_policy_key=repository_policy.policy_key,
                        current_policy_sha256=policy_record.policy_sha256,
                    )
                    return _json_response(
                        start_response=start_response,
                        status_code=200,
                        payload={
                            "status": "ok",
                            "trace_id": request_trace_id,
                            "controller_status": read_model.model_dump(mode="json"),
                        },
                    )
                if action == "merge_train.policy_targets":
                    policy_record = resolve_merge_train_policy_record(record_store)
                    targets = []
                    local_operator_can_read_targets = authz_policy.allows(
                        identity=identity,
                        action=action,
                        product="launchplane",
                        context=_LAUNCHPLANE_SERVICE_CONTEXT,
                    )
                    for repository_policy in policy_record.policy.policies:
                        service_authz_allowed = authz_policy.allows(
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
                                "service_authz": repository_policy.service_authz.model_dump(
                                    mode="json"
                                ),
                            }
                        )
                    targets.sort(key=lambda target: (target["repository"], target["base_branch"]))
                    return _json_response(
                        start_response=start_response,
                        status_code=200,
                        payload={
                            "status": "ok",
                            "trace_id": request_trace_id,
                            "policy": {
                                "record_id": policy_record.record_id,
                                "updated_at": policy_record.updated_at,
                                "policy_sha256": policy_record.policy_sha256,
                            },
                            "targets": targets,
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
                        repository_filter = str((query.get("repository") or [""])[0] or "").strip()
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
                        try:
                            product_read_store = control_plane_product_read_service.require_product_environment_read_model_store(
                                record_store
                            )
                        except (
                            control_plane_product_read_service.ProductReadModelStoreCapabilityError
                        ) as error:
                            return _json_response(
                                start_response=start_response,
                                status_code=503,
                                payload={
                                    "status": "rejected",
                                    "trace_id": request_trace_id,
                                    "error": {
                                        "code": "database_storage_required",
                                        "message": str(error),
                                    },
                                },
                            )
                        try:
                            product_read_result = control_plane_product_read_service.build_product_environment_read_service_result(
                                record_store=product_read_store,
                                params=params,
                                action_allowed=product_action_allowed,
                            )
                        except FileNotFoundError as error:
                            return _json_response(
                                start_response=start_response,
                                status_code=404,
                                payload={
                                    "status": "rejected",
                                    "trace_id": request_trace_id,
                                    "error": {
                                        "code": "not_found",
                                        "message": str(error),
                                    },
                                },
                            )
                        except ValueError as error:
                            return _json_response(
                                start_response=start_response,
                                status_code=400,
                                payload={
                                    "status": "rejected",
                                    "trace_id": request_trace_id,
                                    "error": {
                                        "code": "invalid_request",
                                        "message": str(error),
                                    },
                                },
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
                    try:
                        product_read_store = control_plane_product_read_service.require_product_environment_read_model_store(
                            record_store
                        )
                    except (
                        control_plane_product_read_service.ProductReadModelStoreCapabilityError
                    ) as error:
                        return _json_response(
                            start_response=start_response,
                            status_code=503,
                            payload={
                                "status": "rejected",
                                "trace_id": request_trace_id,
                                "error": {
                                    "code": "database_storage_required",
                                    "message": str(error),
                                },
                            },
                        )
                    product_list_payload = control_plane_product_read_service.build_product_environment_list_service_payload(
                        record_store=product_read_store,
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
            effective_idempotency_route_path = path
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
            elif path == "/v1/agent/write-intents/evaluate":
                intent_request = AgentWriteIntentRequest.model_validate(payload)
                intent_authz_action = authz_action_for_agent_write_intent(intent_request.intent)
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
            elif path == "/v1/products/public-ingress-monitor/run-once":
                monitor_request = PublicIngressMonitorRunOnceEnvelope.model_validate(payload)
                if not authz_policy.allows(
                    identity=identity,
                    action="public_ingress_monitor.run_once",
                    product=monitor_request.product,
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
                                "message": "Workflow cannot run public ingress monitoring.",
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
                recorded_at = _utc_now_timestamp()
                notifier = build_github_issue_notifier() if monitor_request.notify else None
                monitor_result = run_public_ingress_monitor_once(
                    record_store=record_store,
                    checked_at=recorded_at,
                    timeout_seconds=monitor_request.timeout_seconds,
                    notify=monitor_request.notify,
                    notifier=notifier,
                    notification_drivers=(
                        _public_ingress_notification_drivers(record_store=record_store)
                        if monitor_request.notify
                        else None
                    ),
                )
                result = monitor_result.model_dump(mode="json")
                driver_result = result
            elif path == "/v1/public-ingress/notification-policies/apply":
                policy_request = PublicIngressNotificationPolicyApplyEnvelope.model_validate(
                    payload
                )
                if (
                    isinstance(identity, (LocalOperatorIdentity, LocalAdminIdentity))
                    and not policy_request.reason
                ):
                    return _json_response(
                        start_response=start_response,
                        status_code=400,
                        payload={
                            "status": "rejected",
                            "trace_id": request_trace_id,
                            "error": {
                                "code": "reason_required",
                                "message": (
                                    "Local operator public ingress notification policy apply"
                                    " requires a reason."
                                ),
                            },
                        },
                    )
                if not authz_policy.allows(
                    identity=identity,
                    action="public_ingress_notification_policy.apply",
                    product=policy_request.policy.product or "launchplane",
                    context=policy_request.policy.context or _LAUNCHPLANE_SERVICE_CONTEXT,
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
                                    "Workflow cannot apply public ingress notification policy."
                                ),
                            },
                        },
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
                                "message": (
                                    "Public ingress notification policy apply requires"
                                    " DB-backed Launchplane storage."
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
                if policy_request.mode == "apply":
                    record_store.write_public_ingress_notification_policy_record(
                        policy_request.policy
                    )
                summary = _public_ingress_notification_policy_summary(policy_request.policy)
                result = {
                    "public_ingress_notification_policy_id": policy_request.policy.policy_id,
                }
                driver_result = {
                    "mode": policy_request.mode,
                    "changed": policy_request.mode == "apply",
                    "policy": summary,
                }
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
                    idempotency_key=request_idempotency_key,
                )
            elif path == "/v1/every-code/work-requests/rerun":
                if not authz_policy.allows(
                    identity=identity,
                    action="every_code_work_request.rerun",
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
                    idempotency_key=request_idempotency_key,
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
                    idempotency_key=request_idempotency_key,
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
            elif path == "/v1/work-graph/github/issues/reconcile":
                reconcile_result = reconcile_work_graph_issue_inbox(
                    authz_policy=authz_policy,
                    identity=identity,
                    payload=payload,
                    issue_inbox_reconcile_provider=work_graph_issue_inbox_reconcile_provider,
                )
                if reconcile_result is None:
                    return work_graph_issue_inbox_reconcile_denied_response(
                        trace_id=request_trace_id,
                        json_response=_json_response,
                        start_response=start_response,
                    )
                result = reconcile_result.model_dump(mode="json")
                driver_result = {"reconcile": result}
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
                        record_store=cast(EvidenceIngestionStore, record_store),
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
            elif path == "/v1/evidence/runner-host-hygiene/audits":
                runner_host_hygiene_request = RunnerHostHygieneAuditEvidenceEnvelope.model_validate(
                    payload
                )
                if not authz_policy.allows(
                    identity=identity,
                    action="runner_host_hygiene_audit.write",
                    product=runner_host_hygiene_request.product,
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
                                "message": (
                                    "Workflow cannot write runner host hygiene audit evidence."
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
                record_store.write_runner_host_hygiene_audit_record(
                    runner_host_hygiene_request.audit
                )
                result = {
                    "runner_host_hygiene_audit_record_key": runner_host_hygiene_request.audit.audit_record_key,
                }
                driver_result = {
                    "runner_host_hygiene_audit_record_key": runner_host_hygiene_request.audit.audit_record_key,
                    "host_name": runner_host_hygiene_request.audit.request.host_name,
                    "audit_status": runner_host_hygiene_request.audit.status,
                    "mutate": runner_host_hygiene_request.audit.request.mutate,
                    "audit": runner_host_hygiene_request.audit.model_dump(mode="json"),
                }
            elif path == "/v1/evidence/runner-lane-registration/audits":
                runner_lane_registration_request = (
                    RunnerLaneRegistrationAuditEvidenceEnvelope.model_validate(payload)
                )
                if not authz_policy.allows(
                    identity=identity,
                    action="runner_lane_registration_audit.write",
                    product=runner_lane_registration_request.product,
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
                                "message": (
                                    "Workflow cannot write runner lane registration audit evidence."
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
                record_store.write_runner_lane_registration_audit_record(
                    runner_lane_registration_request.audit
                )
                result = {
                    "runner_lane_registration_audit_record_key": (
                        runner_lane_registration_request.audit.audit_record_key
                    ),
                }
                driver_result = {
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
                if (
                    isinstance(identity, (LocalOperatorIdentity, LocalAdminIdentity))
                    and product_config_request.mode == "apply"
                    and not _local_operator_product_config_dry_run_exists(
                        record_store=record_store,
                        scope=request_scope,
                        request_payload=payload,
                    )
                ):
                    return _json_response(
                        start_response=start_response,
                        status_code=409,
                        payload={
                            "status": "rejected",
                            "trace_id": request_trace_id,
                            "error": {
                                "code": "matching_dry_run_required",
                                "message": (
                                    "Local operator product-config apply requires a prior matching dry-run."
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
                if (
                    isinstance(identity, (LocalOperatorIdentity, LocalAdminIdentity))
                    and product_config_request.mode == "dry-run"
                ):
                    _write_local_operator_product_config_dry_run_record(
                        record_store=record_store,
                        scope=request_scope,
                        request_payload=payload,
                        response_trace_id=request_trace_id,
                        response_payload=_accepted_payload(
                            trace_id=request_trace_id,
                            result=result,
                            driver_result=driver_result,
                        ),
                    )
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
                    action="authz_policy_grant.write",
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
            elif path == "/v1/authz-policies/github-actions/removals":
                authz_removal_request = control_plane_authz_grant_service.AuthzPolicyGitHubActionsRemovalEnvelope.model_validate(
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
                                "message": "Authz policy removals require Launchplane database storage.",
                            },
                        },
                    )
                if not authz_policy.allows(
                    identity=identity,
                    action="authz_policy_grant.write",
                    product=authz_removal_request.product,
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
                                "message": "Workflow cannot remove Launchplane authz policy grants.",
                            },
                        },
                    )
                if authz_removal_request.mode == "apply":
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
                    ) = control_plane_authz_grant_service.plan_github_actions_authz_policy_removal(
                        record_store=record_store,
                        removal=authz_removal_request.removal,
                    )
                    audit = control_plane_authz_grant_service.authz_policy_github_actions_removal_audit_payload(
                        request=authz_removal_request,
                        identity=identity,
                        previous_record=current_record,
                        new_record=None,
                        changed=bool(diff["changed"]),
                        trace_id=request_trace_id,
                        now_timestamp=_now_timestamp,
                    )
                    authz_policy_record = current_record
                    changed = bool(diff["changed"])
                    if authz_removal_request.mode == "apply":
                        (
                            updated_policy,
                            authz_policy_record,
                            changed,
                            diff,
                            audit,
                        ) = control_plane_authz_grant_service.write_github_actions_authz_policy_removal(
                            record_store=record_store,
                            request=authz_removal_request,
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
                if authz_removal_request.mode == "apply":
                    authz_policy = updated_policy
                    resolved_authz_policy_sha256 = authz_policy_record.policy_sha256
                    resolved_authz_policy_source = "db"
                result, driver_result = (
                    control_plane_authz_grant_service.build_authz_policy_github_actions_removal_service_result(
                        authz_policy_record=authz_policy_record,
                        changed=changed,
                        mode=authz_removal_request.mode,
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
                    action="authz_policy_grant.write",
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
                    action="authz_policy_grant.write",
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
            elif path == "/v1/authz-policies/local-operators/grants":
                local_operator_grant_request = control_plane_authz_grant_service.AuthzPolicyLocalOperatorGrantEnvelope.model_validate(
                    payload
                )
                if not isinstance(record_store, PostgresRecordStore):
                    return _authz_policy_grant_database_required_response(
                        trace_id=request_trace_id,
                        principal_label="local-operator",
                        start_response=start_response,
                    )
                if not authz_policy.allows(
                    identity=identity,
                    action="authz_policy_grant.write",
                    product=local_operator_grant_request.product,
                    context=_LAUNCHPLANE_SERVICE_CONTEXT,
                ):
                    return _authz_policy_grant_denied_response(
                        trace_id=request_trace_id,
                        principal_label="local-operator",
                        start_response=start_response,
                    )
                if local_operator_grant_request.mode == "apply":
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
                    ) = control_plane_authz_grant_service.plan_local_operator_authz_policy_grant(
                        record_store=record_store,
                        grant=local_operator_grant_request.grant,
                    )
                    audit = control_plane_authz_grant_service.authz_policy_grant_audit_payload(
                        request=local_operator_grant_request,
                        identity=identity,
                        previous_record=current_record,
                        new_record=None,
                        changed=bool(diff["changed"]),
                        trace_id=request_trace_id,
                        now_timestamp=_now_timestamp,
                    )
                    authz_policy_record = current_record
                    changed = bool(diff["changed"])
                    if local_operator_grant_request.mode == "apply":
                        (
                            updated_policy,
                            authz_policy_record,
                            changed,
                            diff,
                            audit,
                        ) = control_plane_authz_grant_service.write_local_operator_authz_policy_grant(
                            record_store=record_store,
                            request=local_operator_grant_request,
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
                    return _authz_policy_unavailable_response(
                        trace_id=request_trace_id,
                        start_response=start_response,
                    )
                if local_operator_grant_request.mode == "apply":
                    authz_policy = updated_policy
                    resolved_authz_policy_sha256 = authz_policy_record.policy_sha256
                    resolved_authz_policy_source = "db"
                result, driver_result = (
                    control_plane_authz_grant_service.build_authz_policy_grant_service_result(
                        authz_policy_record=authz_policy_record,
                        changed=changed,
                        mode=local_operator_grant_request.mode,
                        diff=diff,
                        audit=audit,
                    )
                )
            elif path == "/v1/authz-policies/local-admins/grants":
                local_admin_grant_request = control_plane_authz_grant_service.AuthzPolicyLocalAdminGrantEnvelope.model_validate(
                    payload
                )
                if not isinstance(record_store, PostgresRecordStore):
                    return _authz_policy_grant_database_required_response(
                        trace_id=request_trace_id,
                        principal_label="local-admin",
                        start_response=start_response,
                    )
                if not authz_policy.allows(
                    identity=identity,
                    action="authz_policy_grant.write",
                    product=local_admin_grant_request.product,
                    context=_LAUNCHPLANE_SERVICE_CONTEXT,
                ):
                    return _authz_policy_grant_denied_response(
                        trace_id=request_trace_id,
                        principal_label="local-admin",
                        start_response=start_response,
                    )
                if local_admin_grant_request.mode == "apply":
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
                    ) = control_plane_authz_grant_service.plan_local_admin_authz_policy_grant(
                        record_store=record_store,
                        grant=local_admin_grant_request.grant,
                    )
                    audit = control_plane_authz_grant_service.authz_policy_grant_audit_payload(
                        request=local_admin_grant_request,
                        identity=identity,
                        previous_record=current_record,
                        new_record=None,
                        changed=bool(diff["changed"]),
                        trace_id=request_trace_id,
                        now_timestamp=_now_timestamp,
                    )
                    authz_policy_record = current_record
                    changed = bool(diff["changed"])
                    if local_admin_grant_request.mode == "apply":
                        (
                            updated_policy,
                            authz_policy_record,
                            changed,
                            diff,
                            audit,
                        ) = control_plane_authz_grant_service.write_local_admin_authz_policy_grant(
                            record_store=record_store,
                            request=local_admin_grant_request,
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
                    return _authz_policy_unavailable_response(
                        trace_id=request_trace_id,
                        start_response=start_response,
                    )
                if local_admin_grant_request.mode == "apply":
                    authz_policy = updated_policy
                    resolved_authz_policy_sha256 = authz_policy_record.policy_sha256
                    resolved_authz_policy_source = "db"
                result, driver_result = (
                    control_plane_authz_grant_service.build_authz_policy_grant_service_result(
                        authz_policy_record=authz_policy_record,
                        changed=changed,
                        mode=local_admin_grant_request.mode,
                        diff=diff,
                        audit=audit,
                    )
                )
            elif path == "/v1/merge-train/policies/import":
                merge_train_policy_request = MergeTrainPolicyImportEnvelope.model_validate(payload)
                if not isinstance(record_store, PostgresRecordStore):
                    return _json_response(
                        start_response=start_response,
                        status_code=503,
                        payload={
                            "status": "rejected",
                            "trace_id": request_trace_id,
                            "error": {
                                "code": "database_required",
                                "message": "Merge train policy writes require Launchplane database storage.",
                            },
                        },
                    )
                if not authz_policy.allows(
                    identity=identity,
                    action="merge_train.policy_import",
                    product=merge_train_policy_request.product,
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
                                "message": "Workflow cannot write Launchplane merge train policies.",
                            },
                        },
                    )
                if merge_train_policy_request.mode == "apply":
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
                    record_store.write_merge_train_policy_record(merge_train_policy_request.record)
                result = {
                    "mode": merge_train_policy_request.mode,
                    "record": {
                        "record_id": merge_train_policy_request.record.record_id,
                        "status": merge_train_policy_request.record.status,
                        "source": merge_train_policy_request.record.source,
                        "updated_at": merge_train_policy_request.record.updated_at,
                        "policy_sha256": merge_train_policy_request.record.policy_sha256,
                        "repository_count": len(merge_train_policy_request.record.policy.policies),
                        "policy_keys": [
                            repository_policy.policy_key
                            for repository_policy in merge_train_policy_request.record.policy.policies
                        ],
                    },
                }
                driver_result = result
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
                        product_name=live_target_runtime_request.product,
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
                    action="product_onboarding.apply",
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
                try:
                    onboarding_result = apply_product_onboarding_manifest(
                        record_store=record_store,
                        manifest=onboarding_request.manifest,
                    )
                except ValueError as error:
                    return _json_response(
                        start_response=start_response,
                        status_code=400,
                        payload={
                            "status": "rejected",
                            "trace_id": request_trace_id,
                            "error": {
                                "code": "invalid_product_onboarding_manifest",
                                "message": str(error),
                            },
                        },
                    )
                result, driver_result = (
                    control_plane_product_onboarding_service.build_product_onboarding_service_result(
                        onboarding_result
                    )
                )
            elif path == "/v1/dokploy-targets/setup":
                setup_request = DokployTargetSetupEnvelope.model_validate(payload)
                if not isinstance(record_store, PostgresRecordStore):
                    return _json_response(
                        start_response=start_response,
                        status_code=503,
                        payload={
                            "status": "rejected",
                            "trace_id": request_trace_id,
                            "error": {
                                "code": "database_required",
                                "message": "Dokploy target setup requires Launchplane database storage.",
                            },
                        },
                    )
                if not authz_policy.allows(
                    identity=identity,
                    action="dokploy_target.setup",
                    product=setup_request.product,
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
                                "message": "Workflow cannot run Launchplane Dokploy target setup.",
                            },
                        },
                    )
                if setup_request.mode == "apply":
                    if setup_request.confirmation != "APPLY DOKPLOY TARGET SETUP":
                        return _json_response(
                            start_response=start_response,
                            status_code=400,
                            payload={
                                "status": "rejected",
                                "trace_id": request_trace_id,
                                "error": {
                                    "code": "confirmation_required",
                                    "message": "Dokploy target setup apply requires exact confirmation text.",
                                },
                            },
                        )
                    if not setup_request.reason:
                        return _json_response(
                            start_response=start_response,
                            status_code=400,
                            payload={
                                "status": "rejected",
                                "trace_id": request_trace_id,
                                "error": {
                                    "code": "reason_required",
                                    "message": "Dokploy target setup apply requires a reason.",
                                },
                            },
                        )
                    if not request_idempotency_key:
                        return _json_response(
                            start_response=start_response,
                            status_code=400,
                            payload={
                                "status": "rejected",
                                "trace_id": request_trace_id,
                                "error": {
                                    "code": "idempotency_key_required",
                                    "message": "Dokploy target setup apply requires an Idempotency-Key header.",
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
                    result = _execute_dokploy_target_setup(
                        control_plane_root_path=resolved_root,
                        record_store=record_store,
                        request=setup_request,
                    )
                except ValueError as error:
                    return _json_response(
                        start_response=start_response,
                        status_code=400,
                        payload={
                            "status": "rejected",
                            "trace_id": request_trace_id,
                            "error": {
                                "code": "invalid_dokploy_target_setup",
                                "message": str(error),
                            },
                        },
                    )
                driver_result = {
                    **result,
                    "reason": setup_request.reason,
                }
            elif path == PROVIDER_TARGET_OPERATIONS_ROUTE:
                provider_target_request = ProviderTargetOperationEnvelope.model_validate(payload)
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
                                    "Provider-target operations require Launchplane"
                                    " database storage."
                                ),
                            },
                        },
                    )
                if provider_target_operation_requires_reason(
                    identity=identity,
                    request=provider_target_request,
                ):
                    return _json_response(
                        start_response=start_response,
                        status_code=400,
                        payload={
                            "status": "rejected",
                            "trace_id": request_trace_id,
                            "error": {
                                "code": "reason_required",
                                "message": (
                                    "Local operator provider-target backfill apply"
                                    " requires a reason."
                                ),
                            },
                        },
                    )
                if not provider_target_operation_authorized(
                    authz_policy=authz_policy,
                    identity=identity,
                    request=provider_target_request,
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
                                    "Workflow cannot run Launchplane provider-target operations."
                                ),
                            },
                        },
                    )
                if provider_target_request.mode == "backfill-apply":
                    if not request_idempotency_key:
                        return _json_response(
                            start_response=start_response,
                            status_code=400,
                            payload={
                                "status": "rejected",
                                "trace_id": request_trace_id,
                                "error": {
                                    "code": "idempotency_key_required",
                                    "message": (
                                        "Provider-target backfill apply requests require"
                                        " an Idempotency-Key header."
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
                provider_target_result = execute_provider_target_operation_route(
                    record_store=record_store,
                    request=provider_target_request,
                )
                assert isinstance(provider_target_result, ProviderTargetOperationRouteResult)
                result = provider_target_result.result
                driver_result = provider_target_result.driver_result
            elif path == _EDGE_ENDPOINT_APPLY_ROUTE:
                edge_endpoint_request = EdgeEndpointApplyEnvelope.model_validate(payload)
                if not authz_policy.allows(
                    identity=identity,
                    action="edge_endpoint.apply",
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
                                "message": "Workflow cannot apply Launchplane edge endpoint records.",
                            },
                        },
                    )
                if edge_endpoint_request.mode == "apply":
                    if not request_idempotency_key:
                        return _json_response(
                            start_response=start_response,
                            status_code=400,
                            payload={
                                "status": "rejected",
                                "trace_id": request_trace_id,
                                "error": {
                                    "code": "idempotency_key_required",
                                    "message": "Edge endpoint apply requests require an Idempotency-Key header.",
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
                result, driver_result = _edge_endpoint_apply_result(
                    record_store=record_store,
                    request=edge_endpoint_request,
                )
            elif path == _INGRESS_CANARY_ROUTE_RECORD_APPLY_ROUTE:
                canary_record_request = IngressCanaryRouteRecordApplyEnvelope.model_validate(
                    payload
                )
                if not authz_policy.allows(
                    identity=identity,
                    action="ingress_canary_route.apply",
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
                                "message": "Workflow cannot apply Launchplane ingress canary route records.",
                            },
                        },
                    )
                if canary_record_request.mode == "apply":
                    if not request_idempotency_key:
                        return _json_response(
                            start_response=start_response,
                            status_code=400,
                            payload={
                                "status": "rejected",
                                "trace_id": request_trace_id,
                                "error": {
                                    "code": "idempotency_key_required",
                                    "message": "Ingress canary route record apply requests require an Idempotency-Key header.",
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
                result, driver_result = _ingress_canary_route_record_apply_result(
                    record_store=record_store,
                    request=canary_record_request,
                )
            elif path == _INGRESS_CANARY_ROUTE_APPLY_ROUTE:
                canary_apply_request = IngressCanaryRouteApplyEnvelope.model_validate(payload)
                if not authz_policy.allows(
                    identity=identity,
                    action="ingress_route.apply",
                    product=canary_apply_request.product,
                    context=canary_apply_request.context,
                ):
                    return _json_response(
                        start_response=start_response,
                        status_code=403,
                        payload={
                            "status": "rejected",
                            "trace_id": request_trace_id,
                            "error": {
                                "code": "authorization_denied",
                                "message": "Workflow cannot apply the ingress canary route for the requested product/context.",
                            },
                        },
                    )
                if not request_idempotency_key:
                    return _json_response(
                        start_response=start_response,
                        status_code=400,
                        payload={
                            "status": "rejected",
                            "trace_id": request_trace_id,
                            "error": {
                                "code": "idempotency_key_required",
                                "message": "Ingress canary route apply requests require an Idempotency-Key header.",
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
                    canary_record = _active_ingress_canary_route_record(
                        record_store=record_store,
                        canary_key=canary_apply_request.canary_key,
                        product=canary_apply_request.product,
                        context=canary_apply_request.context,
                    )
                except click.ClickException as error:
                    return _json_response(
                        start_response=start_response,
                        status_code=400,
                        payload={
                            "status": "rejected",
                            "trace_id": request_trace_id,
                            "error": {
                                "code": "invalid_ingress_canary_route",
                                "message": str(error),
                            },
                        },
                    )
                ingress_request = _ingress_request_from_canary_route_record(
                    record=canary_record,
                    mode="apply",
                    reason=canary_apply_request.reason,
                )
                ingress_result = _handle_npmplus_ingress_apply(
                    NpmplusIngressApplyEnvelope(
                        product=canary_record.product,
                        context=canary_record.context,
                        ingress=ingress_request,
                    ),
                    _ResolvedProductDriverContext(profile=None),
                    record_store,
                    resolved_root,
                    state_dir,
                    database_url,
                    identity,
                    request_scope,
                    request_idempotency_key,
                    request_fingerprint,
                    start_response,
                    request_trace_id,
                    resolved_ingress_provider_factory,
                    idempotency_route_path=path,
                )
                if isinstance(ingress_result, list):
                    return ingress_result
                ingress_records, driver_result = ingress_result
                result = {
                    **ingress_records,
                    "ingress_canary_route_key": canary_record.canary_key,
                }
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
                        record_store=cast(EvidenceIngestionStore, record_store),
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
    from control_plane.service_auth import GitHubOidcVerifier

    authz_policy = load_authz_policy(policy_file)
    verifier = GitHubOidcVerifier(audience=audience)
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
        authz_policy=authz_policy,
        database_url=database_url,
        work_graph_planning_facts_provider=work_graph_planning_facts_provider,
        work_graph_issue_inbox_provider=work_graph_issue_inbox_provider,
        work_graph_issue_inbox_reconcile_provider=work_graph_issue_inbox_reconcile_provider,
    )
    with make_server(
        host,
        port,
        cast(WSGIApplication, application),
        server_class=ThreadingWSGIServer,
    ) as server:
        click.echo(f"Launchplane service listening on http://{host}:{port}")
        server.serve_forever()
