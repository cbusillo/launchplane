import asyncio
import hashlib
import json
import logging
import mimetypes
import os
import re
import secrets
from copy import deepcopy
from dataclasses import dataclass, replace
from functools import cache
from urllib.parse import unquote
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, MutableMapping
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path as FilePath
from typing import Annotated, Any, Literal, NoReturn, Protocol, Self, cast
from uuid import uuid4
import click
import fastapi.exceptions as fastapi_exceptions
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.datastructures import DefaultPlaceholder
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse
from jwt import InvalidTokenError
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from requests.exceptions import RequestException
from sqlalchemy.exc import SQLAlchemyError
from control_plane import authz_grant_service as control_plane_authz_grant_service
from control_plane import authz_activation_preflight as control_plane_authz_activation_preflight
from control_plane import authz_diagnostics as control_plane_authz_diagnostics
from control_plane import authz_repository_scope as control_plane_authz_repository_scope
from control_plane import ingress_route_scope as control_plane_ingress_route_scope
from control_plane.dokploy_target_setup_http import (
    DokployTargetSetupEnvelope,
    execute_dokploy_target_setup,
)
from control_plane import product_config as control_plane_product_config
from control_plane import product_config_service as control_plane_product_config_service
from control_plane import product_health_monitoring as control_plane_product_health_monitoring
from control_plane import product_onboarding_service as control_plane_product_onboarding_service
from control_plane.generic_web_onboarding import (
    GenericWebOnboardingIntent,
    build_generic_web_onboarding_manifest,
    generic_web_onboarding_plan_sha256,
    validate_generic_web_onboarding_is_new_product,
)
from control_plane.generic_web_preview_authz import (
    GenericWebPreviewAuthzPlanResult,
    GenericWebPreviewAuthzPlanRequest,
    plan_generic_web_preview_authz_reconcile,
)
from control_plane import (
    product_prelaunch_rebuild_policy as control_plane_product_prelaunch_rebuild_policy,
)
from control_plane import product_preview_tls as control_plane_product_preview_tls
from control_plane import product_stable_lane_repair as control_plane_product_stable_lane_repair
from control_plane import product_retirement as control_plane_product_retirement
from control_plane import (
    detached_application_retirement as control_plane_detached_application_retirement,
)
from control_plane import (
    route_binding_external_reconcile as control_plane_route_binding_external_reconcile,
)
from control_plane import route_binding_reconcile as control_plane_route_binding_reconcile
from control_plane import (
    route_binding_refresh_controller as control_plane_route_binding_refresh_controller,
)
from control_plane import secrets as control_plane_secrets
from control_plane import service_status as control_plane_service_status
from control_plane import live_target_runtime as control_plane_live_target_runtime
from control_plane.change_impact_github import GitHubChangeImpactRepositoryEvidenceProvider
from control_plane.change_impact_service import ChangeImpactRepositoryEvidenceProvider
from control_plane.contracts.change_impact import ChangeImpactTargetReference
from control_plane.contracts.generic_web_deploy_recovery import (
    GenericWebDeployRecoveryProviderEvidenceResponse,
)
from control_plane.contracts.authz_access_read import (
    AUTHZ_DENIAL_EXPLANATION_READ_ACTION,
    AUTHZ_POLICY_CANDIDATE_PREVIEW_READ_ACTION,
    AUTHZ_POLICY_HEALTH_READ_ACTION,
    AuthzActivationPreflightSelfResponse,
    AUTHZ_REPOSITORY_SCOPE_READ_ACTION,
    EFFECTIVE_ACCESS_READ_ACTION,
    AuthzDenialExplanationResponse,
    AuthzPolicyCandidatePreviewRequest,
    AuthzPolicyCandidatePreviewResponse,
    AuthzPolicyHealthResponse,
    AuthzRepositoryScopeReadRequest,
    AuthzRepositoryScopeResponse,
    EffectiveAccessDecision,
    EffectiveAccessEvaluateRequest,
    EffectiveAccessEvaluateResponse,
    EffectiveAccessRequestSummary,
)
from control_plane.contracts.authz_denial_record import build_authz_denial_record
from control_plane.contracts.owner_acceptance import OwnerAcceptanceDecisionStatus
from control_plane.engineering_review_service import (
    EngineeringReviewTargetResolver,
    resolve_engineering_review_pull_request_target,
)
from control_plane.every_code_worker import EVERY_CODE_GITHUB_TOKEN_ENV_KEY
from control_plane.github_app_identity import (
    mint_repository_installation_token,
    resolve_advisory_github_app_identity,
)
from control_plane.owner_acceptance_projection import (
    OwnerAcceptanceProjectionService,
    owner_acceptance_workbench_reference_url,
)
from control_plane.http_routes import (
    AcceptedEvidenceResponse as AcceptedEvidenceResponse,
    DriverReadRouteDependencies,
    EVIDENCE_INGRESS_ROUTES as _EVIDENCE_INGRESS_ROUTES,
    EvidenceWriteRouteDependencies,
    EngineeringReviewDecisionRouteDependencies,
    EngineeringReviewWorkerIdentity,
    EngineeringReviewWriteRouteDependencies,
    GenericWebWriteRouteDependencies,
    ChangeImpactReadRouteDependencies,
    ChangeImpactWriteRouteDependencies,
    CHANGE_IMPACT_EVALUATION_ROUTE,
    CHANGE_IMPACT_POLICY_APPLY_ROUTE,
    OWNER_ACCEPTANCE_EVENTS_ROUTE,
    OWNER_ACCEPTANCE_PROJECT_ROUTE,
    PRIVILEGED_OPERATION_AGENT_PLANS_ROUTE,
    PRIVILEGED_OPERATION_PLANS_ROUTE,
    PRODUCT_OWNER_POLICY_APPLY_ROUTE,
    PRODUCT_OWNER_REQUIREMENT_APPLY_ROUTE,
    PRODUCT_OWNER_ROUTING_APPLY_ROUTE,
    ProductOwnerWriteRouteDependencies,
    OwnerAcceptanceRouteDependencies,
    PrivilegedOperationRouteDependencies,
    GovernanceProjectionRouteDependencies,
    ProductReadRouteDependencies,
    ReadRouteDependencies,
    WorkGraphReadRouteDependencies,
    accepted_evidence_response,
    build_generic_web_write_route_handlers,
    idempotency_capable_store,
    idempotency_scope as idempotency_scope,
    provider_operation_response_payload as _provider_operation_response_payload,
    register_agent_context_read_routes,
    register_change_impact_read_routes,
    register_change_impact_write_routes,
    register_deployment_promotion_read_routes,
    register_dokploy_target_inspect_read_routes,
    register_driver_descriptor_read_routes,
    register_evidence_write_routes,
    register_engineering_review_decision_routes,
    register_engineering_review_routes,
    register_every_code_feedback_read_routes,
    register_every_code_notification_attempt_read_routes,
    register_every_code_preview_gate_read_routes,
    register_every_code_work_request_read_routes,
    register_generic_web_rollback_write_routes,
    register_generic_web_write_routes,
    register_governance_projection_routes,
    register_ingress_read_routes,
    register_inventory_operation_read_routes,
    register_managed_secret_read_routes,
    register_merge_train_read_routes,
    register_owner_acceptance_routes,
    register_privileged_operation_routes,
    register_operation_status_read_routes,
    register_preview_notification_attempt_read_routes,
    register_preview_readiness_read_routes,
    register_preview_record_read_routes,
    register_product_owner_read_routes,
    register_product_owner_write_routes,
    register_product_config_status_read_routes,
    register_product_environment_read_routes,
    register_product_promotion_status_read_routes,
    register_product_profile_read_routes,
    register_protected_artifact_read_routes,
    register_runner_host_hygiene_read_routes,
    REPOSITORY_HUMAN_ROLE_POLICY_APPLY_ROUTE,
    TENANT_ADMISSION_CONTROLLER_RUN_ONCE_ROUTE,
    TENANT_ADMISSION_STATUS_RECONCILE_ROUTE,
    TENANT_TECHNICAL_HUMAN_WAIVER_APPLY_ROUTE,
    TENANT_REPOSITORY_CLASSIFICATION_APPLY_ROUTE,
    TRUSTED_MAINTENANCE_POLICY_APPLY_ROUTE,
    TenantAdmissionReadRouteDependencies,
    TenantAdmissionWriteRouteDependencies,
    register_tenant_admission_read_routes,
    register_tenant_admission_write_routes,
    register_topology_read_routes,
    register_tracked_target_log_read_routes,
    register_work_graph_issue_inbox_read_routes,
    register_work_graph_snapshot_read_routes,
    replay_idempotent_response,
    request_fingerprint as build_request_fingerprint,
    require_product_profile_read_store,
)
from control_plane.generic_web_deploy_recovery_http import (
    GENERIC_WEB_DEPLOY_RECOVERY_PROVIDER_EVIDENCE_ROUTE,
    GenericWebDeployRecoveryDependencies,
    build_generic_web_deploy_recovery_provider_evidence_handler,
)
from control_plane.contracts.authz_policy_record import (
    LaunchplaneAuthzPolicyRecord,
    authz_policy_sha256,
    build_authz_policy_record_id,
)
from control_plane.contracts.durable_operation_authorization import (
    DurableOperationCancellationRequest,
)
from control_plane.durable_operation_authorization import (
    DurableOperationAuthorizationCaptureError,
    build_durable_operation_cancellation,
    capture_durable_operation_authorization,
    read_active_authz_policy_record,
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
from control_plane.contracts.edge_endpoint_record import EdgeEndpointRecord
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
from control_plane.contracts.every_code_work_request import (
    EveryCodeWorkRequestRecord,
    EveryCodeWorkRequestStatusUpdate,
    add_seconds_to_timestamp as add_lease_seconds,
    requeue_every_code_work_request,
)
from control_plane.contracts.idempotency_record import (
    LaunchplaneIdempotencyRecord,
    build_launchplane_idempotency_record_id,
    build_launchplane_mutation_reservation_id,
    complete_launchplane_mutation_reservation,
    format_launchplane_mutation_timestamp,
)
from control_plane.contracts.manager_preview_approval import (
    MANAGER_PREVIEW_APPROVAL_READ_ACTION,
)
from control_plane.contracts.manager_preview_approval_projection import (
    ManagerPreviewApprovalReconcileEnvelope,
)
from control_plane.manager_preview_approval_github_webhook import (
    MANAGER_PREVIEW_APPROVAL_RECONCILE_ROUTE,
    MANAGER_PREVIEW_APPROVAL_WEBHOOK_ROUTE,
    record_manager_preview_approval_invalidation_for_pr,
    reconcile_all_manager_preview_approvals_best_effort,
    reconcile_manager_preview_approval_for_pr,
    reconcile_manager_preview_approval_for_pr_best_effort,
)
from control_plane.provider_operations import (
    DurableProviderMutationAdapter,
    DurableProviderOperationResult,
    ProviderMutationOutcome,
    ProviderMutationRejectedError,
    ProviderMutationUnknownError,
    ProviderObservation,
    ProviderObservationOutcome,
    ProviderOperationLease,
    ProviderTargetSupersession,
    provider_operation_title,
    run_durable_provider_operation,
)
from control_plane.contracts.ingress_canary_route_record import IngressCanaryRouteRecord
from control_plane.contracts.ingress_route_audit_record import (
    IngressRouteAuditOperation,
    IngressRouteAuditRecord,
    IngressRouteTlsOwner,
    build_ingress_route_audit_record_id,
)
from control_plane.contracts.merge_train_policy import MergeTrainPolicyRecord
from control_plane.merge_train_policy_source import (
    MergeTrainPolicyStoreMissingError,
    resolve_merge_train_policy_record,
)
from control_plane.merge_train_batch_landing import (
    MergeTrainBatchLandingPlanRecordNotFoundError,
    MergeTrainBatchLandingRunOnceEnvelope,
    execute_merge_train_batch_landing_run_once,
    require_merge_train_batch_landing_plan_record_store,
)
from control_plane.merge_train_batch_candidate import (
    MergeTrainBatchCandidateRecordNotFoundError,
    MergeTrainBatchCandidateRunOnceEnvelope,
    execute_merge_train_batch_candidate_run_once,
    require_merge_train_batch_candidate_record_store,
)
from control_plane.merge_admission import (
    MergeAdmissionDeniedError,
    MergeAdmissionReconciliationRequiredError,
    require_merge_admission_record_store,
)
from control_plane.merge_admission_live import LiveMergeAdmissionEvaluator
from control_plane.governance_projection import LiveGovernanceCurrentReadinessProvider
from control_plane.contracts.merge_train_controller_state import (
    MergeTrainControllerAdoptionRejectedError,
    MergeTrainControllerLeaseHeldError,
    MergeTrainControllerLeaseLostError,
    MergeTrainControllerReconciliationRequiredError,
)
from control_plane.merge_train_controller_run_once import (
    MergeTrainControllerRequestError,
    MergeTrainControllerRunOnceEnvelope,
    MergeTrainControllerRunOnceResult,
    execute_merge_train_controller_run_once,
    merge_train_controller_mutation_fence,
    require_merge_train_controller_state_record_store,
)
from control_plane.merge_train_github import (
    MergeTrainGitHubError,
    MergeTrainGitHubStaleHeadError,
    UrllibMergeTrainGitHubTransport,
)
from control_plane.merge_train_pr_feedback import (
    MergeTrainPrFeedbackEnvelope,
    build_merge_train_pr_feedback_record,
    require_merge_train_pr_feedback_record_store,
)
from control_plane.merge_train_run_once import (
    MergeTrainRunOnceEnvelope,
    execute_merge_train_run_once,
    require_merge_train_run_record_store,
)
from control_plane.tenant_admission_controller import TenantAdmissionControllerGitHubClient
from control_plane.merge_train_stack_collapse import (
    MergeTrainStackCollapseBatchCandidateStoreMissingError,
    MergeTrainStackCollapsePlanRecordNotFoundError,
    MergeTrainStackCollapseRunOnceEnvelope,
    execute_merge_train_stack_collapse_run_once,
    require_merge_train_stack_collapse_plan_record_store,
)
from control_plane.launchplane_self_deploy_http import (
    LAUNCHPLANE_SELF_DEPLOY_ROUTE as _LAUNCHPLANE_SELF_DEPLOY_ROUTE,
    LaunchplaneSelfDeployEnvelope,
    launchplane_self_deploy_records,
)
from control_plane.generic_web_promotion_http import (
    dispatch_generic_web_promotion_workflow_result,
    execute_generic_web_prod_promotion_result,
)
from control_plane.product_promotion_http import (
    PRODUCT_PROMOTION_DRY_RUN_MARKER_ROUTE as _PRODUCT_PROMOTION_DRY_RUN_MARKER_ROUTE,
    PRODUCT_PROMOTION_DRY_RUN_ROUTE as _PRODUCT_PROMOTION_DRY_RUN_ROUTE,
    PRODUCT_PROMOTION_WORKFLOW_ROUTE as _PRODUCT_PROMOTION_WORKFLOW_ROUTE,
    ProductPromotionDryRunEnvelope,
    ProductPromotionDryRunResponse,
    ProductPromotionDryRunResult,
    ProductPromotionStatus,
    ProductPromotionWorkflowDispatchEnvelope,
    ProductPromotionWorkflowDispatchResponse,
    ProductPromotionWorkflowDispatchResult,
    build_product_promotion_status,
    product_promotion_confirmation,
    product_promotion_continuity_payload,
    product_promotion_direct_request,
    product_promotion_dry_run_key,
    product_promotion_request_payload,
    product_promotion_workflow_request,
    resolve_product_promotion_target,
)
from control_plane.verireel_read_http import (
    VERIREEL_PREVIEW_DESTROY_ROUTE as _VERIREEL_PREVIEW_DESTROY_ROUTE,
    VERIREEL_PREVIEW_INVENTORY_ROUTE as _VERIREEL_PREVIEW_INVENTORY_ROUTE,
    VERIREEL_PREVIEW_REFRESH_ROUTE as _VERIREEL_PREVIEW_REFRESH_ROUTE,
    VERIREEL_PREVIEW_VERIFICATION_ROUTE as _VERIREEL_PREVIEW_VERIFICATION_ROUTE,
    VERIREEL_RUNTIME_VERIFICATION_ROUTE as _VERIREEL_RUNTIME_VERIFICATION_ROUTE,
    VERIREEL_STABLE_ENVIRONMENT_ROUTE as _VERIREEL_STABLE_ENVIRONMENT_ROUTE,
    VERIREEL_TESTING_VERIFICATION_ROUTE as _VERIREEL_TESTING_VERIFICATION_ROUTE,
    VeriReelPreviewDestroyEnvelope,
    VeriReelPreviewInventoryEnvelope,
    VeriReelPreviewRefreshEnvelope,
    VeriReelPreviewVerificationEnvelope,
    VeriReelProductMismatchError,
    VeriReelRouteDependencyError,
    VeriReelPreviewRefreshTransportError,
    VeriReelRuntimeVerificationEnvelope,
    VeriReelStableEnvironmentEnvelope,
    VeriReelTestingVerificationEnvelope,
    apply_verireel_preview_destroy_result,
    apply_verireel_preview_inventory_result,
    apply_verireel_preview_refresh_result,
    apply_verireel_preview_verification_result,
    apply_verireel_testing_verification_result,
    read_verireel_stable_environment_result,
    resolve_verireel_driver_context,
    run_verireel_runtime_verification_result,
    should_store_verireel_result_idempotency,
    verireel_preview_verification_response_records,
    verireel_testing_verification_response_records,
)
from control_plane.verireel_nonprod_http import (
    VERIREEL_APP_MAINTENANCE_ROUTE as _VERIREEL_APP_MAINTENANCE_ROUTE,
    VERIREEL_TESTING_DEPLOY_ROUTE as _VERIREEL_TESTING_DEPLOY_ROUTE,
    VeriReelAppMaintenanceEnvelope,
    VeriReelTestingDeployEnvelope,
    apply_verireel_app_maintenance_result,
    apply_verireel_testing_deploy_result,
)
from control_plane.verireel_prod_http import (
    VERIREEL_PROD_BACKUP_GATE_ROUTE as _VERIREEL_PROD_BACKUP_GATE_ROUTE,
    VERIREEL_PROD_DEPLOY_ROUTE as _VERIREEL_PROD_DEPLOY_ROUTE,
    VERIREEL_PROD_PROMOTION_ROUTE as _VERIREEL_PROD_PROMOTION_ROUTE,
    VERIREEL_PROD_ROLLBACK_ROUTE as _VERIREEL_PROD_ROLLBACK_ROUTE,
    VeriReelProdBackupGateEnvelope,
    VeriReelProdDeployEnvelope,
    VeriReelProdPromotionEnvelope,
    VeriReelProdRollbackEnvelope,
    apply_verireel_prod_backup_gate_result,
    apply_verireel_prod_deploy_result,
    apply_verireel_prod_promotion_result,
    apply_verireel_prod_rollback_result,
    should_store_verireel_prod_result_idempotency,
)
from control_plane.odoo_artifact_publish_inputs_http import (
    ODOO_ARTIFACT_PUBLISH_INPUTS_ROUTE as _ODOO_ARTIFACT_PUBLISH_INPUTS_ROUTE,
    OdooArtifactPublishInputsEnvelope,
    OdooArtifactPublishInputsProductMismatchError,
    OdooArtifactPublishInputsRouteDependencyError,
    build_odoo_artifact_publish_inputs_result,
    resolve_odoo_artifact_publish_inputs_profile,
)
from control_plane.odoo_artifact_publish_http import (
    ODOO_ARTIFACT_PUBLISH_ROUTE as _ODOO_ARTIFACT_PUBLISH_ROUTE,
    OdooArtifactPublishEnvelope,
    OdooArtifactPublishProductMismatchError,
    OdooArtifactPublishRouteDependencyError,
    ingest_odoo_artifact_publish_evidence_result,
    resolve_odoo_artifact_publish_product_route,
    should_store_odoo_artifact_publish_idempotency,
    validate_odoo_artifact_publish_product_evidence,
)
from control_plane.odoo_preview_apply_http import (
    ODOO_PREVIEW_APPLY_INPUTS_ROUTE as _ODOO_PREVIEW_APPLY_INPUTS_ROUTE,
    ODOO_PREVIEW_APPLY_ROUTE as _ODOO_PREVIEW_APPLY_ROUTE,
    OdooPreviewApplyConfigError,
    OdooPreviewApplyEnvelope,
    OdooPreviewApplyInputsEnvelope,
    OdooPreviewApplyProductMismatchError,
    OdooPreviewApplyRouteDependencyError,
    OdooPreviewPlanProvenanceError,
    apply_odoo_preview_lifecycle_evidence,
    build_odoo_preview_apply_inputs_result,
    build_odoo_preview_plan_id,
    build_odoo_preview_runtime_identity,
    driver_result_contains_status,
    execute_odoo_preview_apply_result,
    issue_odoo_preview_apply_plan,
    observe_odoo_preview_apply_result,
    odoo_preview_destroy_supersession_is_quiescent,
    resolve_odoo_preview_apply_profile,
    validate_odoo_preview_issued_plan,
    validate_odoo_preview_lifecycle_response_current,
    validate_odoo_preview_profile_authority,
)
from control_plane.odoo_post_deploy_http import (
    ODOO_CONFIG_PARAMETER_OVERRIDE_ROUTE as _ODOO_CONFIG_PARAMETER_OVERRIDE_ROUTE,
    ODOO_POST_DEPLOY_ROUTE as _ODOO_POST_DEPLOY_ROUTE,
    ODOO_WEBSITE_BOOTSTRAP_OVERRIDE_ROUTE as _ODOO_WEBSITE_BOOTSTRAP_OVERRIDE_ROUTE,
    OdooConfigParameterOverrideEnvelope,
    OdooInstanceOverrideStore,
    OdooPostDeployEnvelope,
    OdooPostDeployProductMismatchError,
    OdooPostDeployRouteDependencyError,
    OdooWebsiteBootstrapOverrideEnvelope,
    execute_odoo_post_deploy_result,
    resolve_odoo_post_deploy_product_route,
    write_odoo_config_parameter_override_result,
    write_odoo_website_bootstrap_override_result,
)
from control_plane.odoo_app_maintenance_http import (
    ODOO_APP_MAINTENANCE_ROUTE as _ODOO_APP_MAINTENANCE_ROUTE,
    OdooAppMaintenanceEnvelope,
    OdooAppMaintenanceProductMismatchError,
    OdooAppMaintenanceRouteDependencyError,
    execute_odoo_app_maintenance_result,
    resolve_odoo_app_maintenance_product_route,
    should_store_odoo_app_maintenance_idempotency,
)
from control_plane.odoo_prod_backup_gate_http import (
    ODOO_PROD_BACKUP_GATE_ROUTE as _ODOO_PROD_BACKUP_GATE_ROUTE,
    ODOO_PROD_BACKUP_VERIFICATION_ROUTE as _ODOO_PROD_BACKUP_VERIFICATION_ROUTE,
    OdooProdBackupGateEnvelope,
    OdooProdBackupGateProductMismatchError,
    OdooProdBackupGateRouteDependencyError,
    OdooProdBackupVerificationEnvelope,
    execute_odoo_prod_backup_gate_result,
    execute_odoo_prod_backup_verification_result,
    resolve_odoo_prod_backup_gate_product_route,
    should_store_odoo_prod_backup_gate_idempotency,
    should_store_odoo_prod_backup_verification_idempotency,
)
from control_plane.odoo_prod_backup_restore_http import (
    ODOO_PROD_BACKUP_RESTORE_APPLY_ROUTE as _ODOO_PROD_BACKUP_RESTORE_APPLY_ROUTE,
    ODOO_PROD_BACKUP_RESTORE_PLAN_ROUTE as _ODOO_PROD_BACKUP_RESTORE_PLAN_ROUTE,
    OdooProdBackupRestoreApplyEnvelope,
    OdooProdBackupRestoreIdempotencyKeyReusedError,
    OdooProdBackupRestoreLaneBusyError,
    OdooProdBackupRestoreOperationActiveError,
    OdooProdBackupRestorePlanChangedError,
    OdooProdBackupRestorePlanEnvelope,
    OdooProdBackupRestoreProductMismatchError,
    OdooProdBackupRestoreReplayNotEligibleError,
    OdooProdBackupRestoreRouteDependencyError,
    enqueue_odoo_prod_backup_restore_operation,
    odoo_prod_backup_restore_operation_payload,
    resolve_odoo_prod_backup_restore_lane,
)
from control_plane.odoo_prod_retained_volume_backup_import_http import (
    ODOO_PROD_RETAINED_VOLUME_BACKUP_IMPORT_APPLY_ROUTE as _ODOO_PROD_RETAINED_VOLUME_BACKUP_IMPORT_APPLY_ROUTE,
    ODOO_PROD_RETAINED_VOLUME_BACKUP_IMPORT_PLAN_ROUTE as _ODOO_PROD_RETAINED_VOLUME_BACKUP_IMPORT_PLAN_ROUTE,
    OdooProdRetainedVolumeBackupImportApplyEnvelope,
    OdooProdRetainedVolumeBackupImportIdempotencyKeyReusedError,
    OdooProdRetainedVolumeBackupImportLaneBusyError,
    OdooProdRetainedVolumeBackupImportOperationActiveError,
    OdooProdRetainedVolumeBackupImportPlanChangedError,
    OdooProdRetainedVolumeBackupImportPlanEnvelope,
    OdooProdRetainedVolumeBackupImportProductMismatchError,
    OdooProdRetainedVolumeBackupImportRouteDependencyError,
    enqueue_odoo_prod_retained_volume_backup_import_apply_operation,
    enqueue_odoo_prod_retained_volume_backup_import_plan_operation,
    operation_payload as odoo_prod_retained_volume_backup_import_operation_payload,
    resolve_odoo_prod_retained_volume_backup_import_lane,
)
from control_plane.odoo_prod_promotion_http import (
    ODOO_PROD_PROMOTION_INPUTS_ROUTE as _ODOO_PROD_PROMOTION_INPUTS_ROUTE,
    ODOO_PROD_PROMOTION_ROUTE as _ODOO_PROD_PROMOTION_ROUTE,
    ODOO_PROD_PROMOTION_RUN_ROUTE as _ODOO_PROD_PROMOTION_RUN_ROUTE,
    OdooProdPromotionEnvelope,
    OdooProdPromotionInputsEnvelope,
    OdooProdPromotionProductMismatchError,
    OdooProdPromotionRouteDependencyError,
    OdooProdPromotionRunEnvelope,
    execute_odoo_prod_promotion_result,
    execute_odoo_prod_promotion_run_result,
    resolve_odoo_prod_promotion_inputs_result,
    resolve_odoo_prod_promotion_product_route,
    should_store_prod_promotion_idempotency,
)
from control_plane.odoo_prod_rollback_http import (
    ODOO_PROD_ROLLBACK_ROUTE as _ODOO_PROD_ROLLBACK_ROUTE,
    OdooProdRollbackEnvelope,
    OdooProdRollbackProductMismatchError,
    OdooProdRollbackRouteDependencyError,
    execute_odoo_prod_rollback_result,
    resolve_odoo_prod_rollback_product_route,
    should_store_odoo_prod_rollback_idempotency,
)
from control_plane.odoo_stable_bootstrap_http import (
    ODOO_STABLE_BOOTSTRAP_ROUTE as _ODOO_STABLE_BOOTSTRAP_ROUTE,
    OdooStableBootstrapEnvelope,
    OdooStableBootstrapIdempotencyKeyReusedError,
    OdooStableBootstrapLaneBusyError,
    OdooStableBootstrapOperationActiveError,
    OdooStableBootstrapProductMismatchError,
    OdooStableBootstrapRouteDependencyError,
    enqueue_odoo_stable_bootstrap_operation,
    operation_payload as odoo_stable_bootstrap_operation_payload,
    resolve_odoo_stable_bootstrap_product_route,
)
from control_plane.odoo_target_replacement_plan_http import (
    ODOO_TARGET_REPLACEMENT_PLAN_ROUTE as _ODOO_TARGET_REPLACEMENT_PLAN_ROUTE,
    OdooTargetReplacementPlanEnvelope,
    OdooTargetReplacementPlanProductMismatchError,
    OdooTargetReplacementPlanRouteDependencyError,
    resolve_odoo_target_replacement_plan_lane,
)
from control_plane.odoo_target_replacement_apply_http import (
    ODOO_TARGET_REPLACEMENT_APPLY_ROUTE as _ODOO_TARGET_REPLACEMENT_APPLY_ROUTE,
    OdooTargetReplacementApplyEnvelope,
    OdooTargetReplacementApplyCurrentArtifactChangedError,
    OdooTargetReplacementApplyIdempotencyKeyReusedError,
    OdooTargetReplacementApplyLaneBusyError,
    OdooTargetReplacementApplyOperationActiveError,
    OdooTargetReplacementApplyProductMismatchError,
    OdooTargetReplacementApplyRouteDependencyError,
    enqueue_odoo_target_replacement_apply_operation,
    operation_payload as odoo_target_replacement_apply_operation_payload,
    resolve_odoo_target_replacement_apply_lane,
)
from control_plane.workflows.odoo_stable_target_replacement import (
    OdooStableTargetReplacementStore,
    build_odoo_stable_target_replacement_plan,
)
from control_plane.workflows.odoo_prod_backup_restore import (
    OdooProdBackupRestoreStore,
    build_odoo_prod_backup_restore_plan,
)
from control_plane.workflows.odoo_preview_runtime import (
    ODOO_PREVIEW_DESTROY_SUPERSESSION_GRACE_SECONDS,
    OdooPreviewApplyInputsResult,
    OdooPreviewDokployApplyResult,
)
from control_plane.contracts.product_environment_read_model import (
    ActionAllowed,
)
from control_plane.contracts.product_onboarding_manifest import ProductOnboardingManifest
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductExpectedConfigProfile,
    ProductLaneProfile,
    ProductRuntimeConfigRequirement,
    ProductSecretConfigRequirement,
    product_profile_record_sha256,
    validate_product_profile_history_transition,
)
from control_plane.contracts.product_retirement import (
    ProductRetirementIdentity,
    ProductRetirementRequest,
)
from control_plane.contracts.detached_application_retirement import (
    DetachedApplicationRetirementIdentity,
    DetachedApplicationRetirementRequest,
)
from control_plane.contracts.preview_desired_state_record import PreviewDesiredStateRecord
from control_plane.contracts.preview_inventory_scan_record import PreviewInventoryScanRecord
from control_plane.contracts.preview_lifecycle_plan_record import (
    PreviewLifecycleDesiredPreview,
    PreviewLifecyclePlanRecord,
)
from control_plane.contracts.preview_pr_feedback_notifications import (
    PreviewPrFeedbackNotificationPolicyRecord,
)
from control_plane.contracts.preview_pr_feedback_record import (
    PreviewPrFeedbackRecord,
    PreviewPrFeedbackStatus,
)
from control_plane.contracts.preview_pr_feedback_remediation import (
    PreviewPrFeedbackRemediationRecord,
    PreviewPrFeedbackRemediationRequest,
)
from control_plane.contracts.private_health_endpoint_record import PrivateHealthEndpointRecord
from control_plane.contracts.route_binding_record import (
    EnvironmentRouteBindingRecord,
    redacted_route_binding_record,
)
from control_plane.contracts.runtime_key_safety_policy import RuntimeKeySafetyTarget
from control_plane.contracts.secret_reencryption_request import SecretReencryptionRequest
from control_plane.contracts.public_ingress_monitoring import PublicIngressNotificationPolicyRecord
from control_plane.drivers import native_routes
from control_plane.drivers.route_paths import (
    INGRESS_ROUTE_APPLY_ROUTE as _INGRESS_ROUTE_APPLY_ROUTE,
)
from control_plane.every_code_work_request_write import (
    EveryCodeWorkRequestCreateEnvelope,
    build_every_code_work_request_record,
)
from control_plane.every_code_notifications_delivery import deliver_every_code_blocked_notifications
from control_plane.notifications import post_discord_webhook
from control_plane.preview_pr_feedback_notifications import (
    deliver_preview_pr_feedback_notifications,
)
from control_plane.preview_lifecycle_cleanup_routes import (
    PreviewLifecycleCleanupEnvelope,
    PreviewLifecycleSweepEnvelope,
    build_preview_lifecycle_sweep,
    latest_preview_lifecycle_plan,
    preview_lifecycle_cleanup_profile_settings,
    preview_lifecycle_sweep_profiles,
    require_preview_lifecycle_cleanup_apply_store,
    require_preview_lifecycle_cleanup_mutation_store,
    require_preview_lifecycle_sweep_store,
    write_preview_lifecycle_cleanup_apply_record,
)
from control_plane.product_config_http import (
    ProductConfigApplyEnvelope,
    ProductConfigApplyResponse,
    ProductConfigApplyResult,
    ProductEnvironmentConfigApplyEnvelope,
    product_config_live_target_next_actions,
    product_environment_config_apply_request,
    product_environment_config_confirmation,
)
from control_plane.provider_target_operations_http import (
    PROVIDER_TARGET_OPERATIONS_ROUTE,
    ProviderTargetOperationEnvelope,
    execute_provider_target_operation_route,
    provider_target_operation_authorized,
    provider_target_operation_requires_reason,
)
from control_plane.runtime_key_safety_http import (
    RuntimeKeySafetyPolicyApplyEnvelope,
    apply_runtime_key_safety_policy_route,
)
from control_plane.runtime_key_safety import (
    evaluate_runtime_key_safety_from_store,
    latest_active_runtime_key_safety_policy,
    runtime_key_safety_environment_class,
)
from control_plane.service_auth import (
    AgentAuthzDecision,
    AuthorizationTarget,
    AuthzEvaluation,
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
    clear_authz_evaluation,
    current_authz_evaluation,
)
from control_plane.service_human_auth import (
    BROWSER_CSRF_HEADER_NAME,
    HumanSessionManager,
    LaunchplaneHumanSession,
    OAuthLoginState,
    OAuthLoginStateStore,
    build_pkce_verifier,
    safe_oauth_return_to,
    validate_browser_mutation_request_headers,
)
from control_plane.storage.factory import build_shared_record_store
from control_plane.storage.factory import storage_backend_name
from control_plane.storage.product_authority_bundle import ProductAuthorityBundle
from control_plane.storage.postgres import (
    DbOnlyMutationPreflightResult,
    DbOnlyMutationRequest,
    MutationReservationCompletionResult,
    MutationReservationResult,
    OutboxWithIdempotencyRequest,
    PostgresRecordStore,
    RouteBindingReconcileWriteResult,
)
from control_plane.workflows.product_onboarding import plan_product_onboarding_authority_bundle
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
from control_plane.workflows.verireel_prod_backup_gate_operation_worker import (
    DEFAULT_VERIREEL_BACKUP_GATE_WORKER_MAX_ATTEMPTS,
    reconcile_stale_verireel_prod_backup_gate_operation_records,
)
from control_plane.workflows.preview_lifecycle import build_preview_lifecycle_plan
from control_plane.workflows.preview_lifecycle_cleanup import (
    build_preview_lifecycle_cleanup_record,
)
from control_plane.workflows.preview_desired_state import discover_github_preview_desired_state
from control_plane.workflows.preview_pr_feedback import (
    DEFAULT_PREVIEW_FEEDBACK_MARKER,
    EveryCodeWorkRequestReadStore,
    PreviewPrFeedbackPreviewReadStore,
    build_preview_pr_feedback_record,
)
from control_plane.preview_pr_feedback_remediation import (
    PreviewPrFeedbackRemediationStore,
    PreviewPrFeedbackRemediationApplyError,
    apply_remediation,
    bind_preview_pr_feedback_target,
    build_remediation_record,
    matching_dry_run,
    observe_managed_preview_pr_feedback,
    resolve_remediation_token,
)
from control_plane.workflows.launchplane_self_deploy import execute_launchplane_self_deploy
from control_plane.workflows.ship import utc_now_timestamp
from control_plane.workflows.launchplane import (
    github_api_request,
    resolve_launchplane_github_token,
)
from control_plane.work_graph_issue_inbox import (
    GitHubIssueInboxReconcileRequest,
)
from control_plane.work_graph_service import (
    WorkGraphIssueInboxProvider,
    WorkGraphIssueInboxReconcileProvider,
    WorkGraphIssueInboxReconcileResponse,
    WorkGraphIssueInboxReconcileResponseResult,
    WorkGraphPlanningFactsProvider,
    WorkGraphRankEnvelope,
    WorkGraphRankResponse,
    WorkGraphRankResult,
    build_work_graph_rank_result,
)

EveryCodeGitHubWebhookHandler = Callable[
    [bytes, str, str, str, object, FilePath, str], tuple[int, dict[str, object]]
]
ManagerPreviewApprovalGitHubWebhookHandler = Callable[
    [bytes, str, str, str, object, FilePath, str], tuple[int, dict[str, object]]
]


Message = MutableMapping[str, Any]
Scope = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]
STARLETTE_HTTP_EXCEPTION: Any = getattr(fastapi_exceptions, "StarletteHTTPException")


_LOGGER = logging.getLogger(__name__)

_ODOO_STABLE_BOOTSTRAP_OPERATION_CANCEL_ROUTE = (
    "/v1/drivers/odoo/stable-bootstrap/operations/{operation_id}/cancel"
)
_ODOO_TARGET_REPLACEMENT_OPERATION_CANCEL_ROUTE = (
    "/v1/drivers/odoo/target-replacement/operations/{operation_id}/cancel"
)
_ODOO_PROD_BACKUP_RESTORE_OPERATION_CANCEL_ROUTE = (
    "/v1/drivers/odoo/prod-backup-restore/operations/{operation_id}/cancel"
)
_ODOO_PROD_RETAINED_VOLUME_BACKUP_IMPORT_OPERATION_CANCEL_ROUTE = (
    "/v1/drivers/odoo/prod-retained-volume-backup-import/operations/{operation_id}/cancel"
)
_VERIREEL_PROD_BACKUP_GATE_OPERATION_CANCEL_ROUTE = (
    "/v1/drivers/verireel/prod-backup-gate/operations/{operation_id}/cancel"
)


_BEARER_CHALLENGE_HEADER = {"WWW-Authenticate": 'Bearer realm="Launchplane API"'}
_EVERY_CODE_GITHUB_WEBHOOK_ROUTE = "/v1/every-code/github-webhook"
_PRODUCT_CONFIG_APPLY_ROUTE = "/v1/product-config/apply"
_PRODUCT_ENVIRONMENT_CONFIG_APPLY_ROUTE = (
    "/v1/products/{product}/environments/{environment}/config/apply"
)
_SECRET_REENCRYPT_ROUTE = "/v1/secrets/reencrypt"
_EVIDENCE_INGRESS_MAX_BODY_BYTES = 2 * 1024 * 1024
_GITHUB_WEBHOOK_MAX_BODY_BYTES = 2 * 1024 * 1024
_PRODUCT_CONFIG_MAX_BODY_BYTES = 2 * 1024 * 1024
_PRODUCT_HEALTH_MONITORING_MAX_BODY_BYTES = 64 * 1024
_PRODUCT_PRELAUNCH_REBUILD_POLICY_MAX_BODY_BYTES = 64 * 1024
_PRODUCT_STABLE_LANE_REPAIR_MAX_BODY_BYTES = 64 * 1024
_SECRET_REENCRYPT_MAX_BODY_BYTES = 64 * 1024
_TENANT_REPOSITORY_CLASSIFICATION_MAX_BODY_BYTES = 64 * 1024
_REPOSITORY_HUMAN_ROLE_POLICY_MAX_BODY_BYTES = 64 * 1024
_TENANT_TECHNICAL_HUMAN_WAIVER_MAX_BODY_BYTES = 64 * 1024
_TENANT_ADMISSION_CONTROLLER_RUN_ONCE_MAX_BODY_BYTES = 64 * 1024
_TENANT_ADMISSION_STATUS_RECONCILE_MAX_BODY_BYTES = 64 * 1024
_TRUSTED_MAINTENANCE_POLICY_MAX_BODY_BYTES = 64 * 1024
_PRODUCT_OWNER_POLICY_MAX_BODY_BYTES = 64 * 1024
_OWNER_ACCEPTANCE_MAX_BODY_BYTES = 16 * 1024
_CHANGE_IMPACT_EVALUATION_MAX_BODY_BYTES = 16 * 1024
_CHANGE_IMPACT_POLICY_MAX_BODY_BYTES = 64 * 1024
_PRIVILEGED_OPERATION_MAX_BODY_BYTES = 256 * 1024
_PRODUCT_HEALTH_MONITORING_APPLY_ROUTE = "/v1/product-profiles/health-monitoring/apply"
_PRODUCT_PRELAUNCH_REBUILD_POLICY_APPLY_ROUTE = "/v1/product-profiles/prelaunch-rebuild/apply"
_PRODUCT_STABLE_LANE_REPAIR_APPLY_ROUTE = "/v1/product-profiles/stable-lane-repair/apply"
_BOUNDED_REQUEST_BODY_CONTRACTS: dict[str, tuple[str, int, bool, bool]] = {
    **{
        route: ("Evidence ingress", _EVIDENCE_INGRESS_MAX_BODY_BYTES, True, False)
        for route in _EVIDENCE_INGRESS_ROUTES
    },
    _EVERY_CODE_GITHUB_WEBHOOK_ROUTE: (
        "GitHub webhook",
        _GITHUB_WEBHOOK_MAX_BODY_BYTES,
        False,
        True,
    ),
    MANAGER_PREVIEW_APPROVAL_WEBHOOK_ROUTE: (
        "Manager preview approval GitHub webhook",
        _GITHUB_WEBHOOK_MAX_BODY_BYTES,
        False,
        True,
    ),
    MANAGER_PREVIEW_APPROVAL_RECONCILE_ROUTE: (
        "Manager preview approval reconciliation",
        _PRODUCT_HEALTH_MONITORING_MAX_BODY_BYTES,
        True,
        True,
    ),
    _PRODUCT_CONFIG_APPLY_ROUTE: (
        "Product config",
        _PRODUCT_CONFIG_MAX_BODY_BYTES,
        True,
        True,
    ),
    _PRODUCT_HEALTH_MONITORING_APPLY_ROUTE: (
        "Product health monitoring",
        _PRODUCT_HEALTH_MONITORING_MAX_BODY_BYTES,
        True,
        True,
    ),
    _PRODUCT_PRELAUNCH_REBUILD_POLICY_APPLY_ROUTE: (
        "Product prelaunch rebuild policy",
        _PRODUCT_PRELAUNCH_REBUILD_POLICY_MAX_BODY_BYTES,
        True,
        True,
    ),
    _PRODUCT_STABLE_LANE_REPAIR_APPLY_ROUTE: (
        "Product stable lane repair",
        _PRODUCT_STABLE_LANE_REPAIR_MAX_BODY_BYTES,
        True,
        True,
    ),
    _SECRET_REENCRYPT_ROUTE: (
        "Managed-secret re-encryption",
        _SECRET_REENCRYPT_MAX_BODY_BYTES,
        True,
        True,
    ),
    PRIVILEGED_OPERATION_PLANS_ROUTE: (
        "Privileged-operation plan",
        _PRIVILEGED_OPERATION_MAX_BODY_BYTES,
        True,
        True,
    ),
    PRIVILEGED_OPERATION_AGENT_PLANS_ROUTE: (
        "Agent privileged-operation proposal",
        _PRIVILEGED_OPERATION_MAX_BODY_BYTES,
        True,
        True,
    ),
    TENANT_REPOSITORY_CLASSIFICATION_APPLY_ROUTE: (
        "Tenant repository classification",
        _TENANT_REPOSITORY_CLASSIFICATION_MAX_BODY_BYTES,
        True,
        True,
    ),
    REPOSITORY_HUMAN_ROLE_POLICY_APPLY_ROUTE: (
        "Repository human role policy",
        _REPOSITORY_HUMAN_ROLE_POLICY_MAX_BODY_BYTES,
        True,
        True,
    ),
    TENANT_TECHNICAL_HUMAN_WAIVER_APPLY_ROUTE: (
        "Tenant technical human waiver",
        _TENANT_TECHNICAL_HUMAN_WAIVER_MAX_BODY_BYTES,
        True,
        True,
    ),
    TENANT_ADMISSION_CONTROLLER_RUN_ONCE_ROUTE: (
        "Tenant admission controller run",
        _TENANT_ADMISSION_CONTROLLER_RUN_ONCE_MAX_BODY_BYTES,
        True,
        True,
    ),
    TENANT_ADMISSION_STATUS_RECONCILE_ROUTE: (
        "Tenant admission status reconciliation",
        _TENANT_ADMISSION_STATUS_RECONCILE_MAX_BODY_BYTES,
        True,
        True,
    ),
    TRUSTED_MAINTENANCE_POLICY_APPLY_ROUTE: (
        "Trusted-maintenance policy",
        _TRUSTED_MAINTENANCE_POLICY_MAX_BODY_BYTES,
        True,
        True,
    ),
    CHANGE_IMPACT_EVALUATION_ROUTE: (
        "Change impact evaluation",
        _CHANGE_IMPACT_EVALUATION_MAX_BODY_BYTES,
        True,
        True,
    ),
    OWNER_ACCEPTANCE_EVENTS_ROUTE: (
        "Owner acceptance event",
        _OWNER_ACCEPTANCE_MAX_BODY_BYTES,
        True,
        True,
    ),
    OWNER_ACCEPTANCE_PROJECT_ROUTE: (
        "Owner acceptance projection",
        _OWNER_ACCEPTANCE_MAX_BODY_BYTES,
        True,
        True,
    ),
    CHANGE_IMPACT_POLICY_APPLY_ROUTE: (
        "Change impact policy",
        _CHANGE_IMPACT_POLICY_MAX_BODY_BYTES,
        True,
        True,
    ),
    PRODUCT_OWNER_POLICY_APPLY_ROUTE: (
        "Product Owner policy",
        _PRODUCT_OWNER_POLICY_MAX_BODY_BYTES,
        True,
        True,
    ),
    PRODUCT_OWNER_REQUIREMENT_APPLY_ROUTE: (
        "Product Owner requirement",
        _PRODUCT_OWNER_POLICY_MAX_BODY_BYTES,
        True,
        True,
    ),
    PRODUCT_OWNER_ROUTING_APPLY_ROUTE: (
        "Product Owner routing",
        _PRODUCT_OWNER_POLICY_MAX_BODY_BYTES,
        True,
        True,
    ),
}
_DOKPLOY_TARGET_SETUP_ROUTE = "/v1/dokploy-targets/setup"
_PUBLIC_INGRESS_MONITOR_RUN_ONCE_ROUTE = "/v1/products/public-ingress-monitor/run-once"
_PUBLIC_INGRESS_NOTIFICATION_POLICY_APPLY_ROUTE = "/v1/public-ingress/notification-policies/apply"
_EVERY_CODE_NOTIFICATION_POLICY_APPLY_ROUTE = "/v1/every-code/notification-policies/apply"
_PREVIEW_PR_FEEDBACK_NOTIFICATION_POLICY_APPLY_ROUTE = (
    "/v1/previews/pr-feedback/notification-policies/apply"
)
_PREVIEW_PR_FEEDBACK_ROUTE = "/v1/previews/pr-feedback"
_PREVIEW_PR_FEEDBACK_REMEDIATION_ROUTE = "/v1/previews/pr-feedback/remediation"
_PREVIEW_DESIRED_STATE_ROUTE = "/v1/previews/desired-state"
_PREVIEW_LIFECYCLE_PLAN_ROUTE = "/v1/previews/lifecycle-plan"
_PREVIEW_LIFECYCLE_CLEANUP_ROUTE = "/v1/previews/lifecycle-cleanup"
_PREVIEW_LIFECYCLE_SWEEP_ROUTE = "/v1/previews/lifecycle-sweep"
_MERGE_TRAIN_BATCH_LANDING_RUN_ONCE_ROUTE = "/v1/work-graph/merge-train/batch-landing/run-once"
_MERGE_TRAIN_BATCH_CANDIDATE_RUN_ONCE_ROUTE = "/v1/work-graph/merge-train/batch-candidate/run-once"
_MERGE_TRAIN_CONTROLLER_RUN_ONCE_ROUTE = "/v1/work-graph/merge-train/controller/run-once"
_MERGE_TRAIN_STACK_COLLAPSE_RUN_ONCE_ROUTE = "/v1/work-graph/merge-train/stack-collapse/run-once"
_MERGE_TRAIN_RUN_ONCE_ROUTE = "/v1/work-graph/merge-train/run-once"
_MERGE_TRAIN_PR_FEEDBACK_ROUTE = "/v1/work-graph/merge-train/pr-feedback"
_RUNTIME_KEY_SAFETY_POLICY_APPLY_ROUTE = "/v1/runtime-key-safety/policies/apply"
_EDGE_ENDPOINT_APPLY_ROUTE = "/v1/edge-endpoints/apply"
_PRIVATE_HEALTH_ENDPOINT_APPLY_ROUTE = "/v1/private-health-endpoints/apply"
_LIVE_TARGET_RUNTIME_APPLY_ROUTE = "/v1/live-target-runtime/apply"
_INGRESS_CANARY_ROUTE_RECORD_APPLY_ROUTE = "/v1/ingress/canary-routes/records/apply"
_INGRESS_CANARY_ROUTE_APPLY_ROUTE = "/v1/ingress/canary-routes/apply"
_EXTERNAL_ROUTE_BINDING_RECONCILE_ROUTE = "/v1/route-bindings/external/reconcile"
_ROUTE_BINDING_RECONCILE_ROUTE = "/v1/route-bindings/reconcile"
_ODOO_TESTING_ROUTE_BINDING_REFRESH_ROUTE = "/v1/route-bindings/odoo-testing/controller/run-once"
_ODOO_TESTING_ROUTE_BINDING_REFRESH_TARGET_LIMIT = 25
_PRODUCT_PROFILES_ROUTE = "/v1/product-profiles"
_PRODUCT_EXPECTED_CONFIG_APPLY_ROUTE = "/v1/product-profiles/expected-config/apply"
_PRODUCT_PREVIEW_TLS_APPLY_ROUTE = "/v1/product-profiles/preview-tls/apply"
_PRODUCT_RETIREMENT_ROUTE = "/v1/product-retirement"
_DETACHED_APPLICATION_RETIREMENT_ROUTE = "/v1/detached-application-retirement"
_PRODUCT_ONBOARDING_APPLY_ROUTE = "/v1/product-onboarding/apply"
_MERGE_TRAIN_POLICY_IMPORT_ROUTE = "/v1/merge-train/policies/import"
_AUTHZ_POLICY_ACTIVE_ROUTE = "/v1/authz-policies/active"
_AUTHZ_DIAGNOSTIC_EVALUATE_ROUTE = "/v1/authz-diagnostics/github-actions/evaluate"
_AUTHZ_EFFECTIVE_ACCESS_EVALUATE_ROUTE = "/v1/authz-diagnostics/effective-access/evaluate"
_AUTHZ_POLICY_HEALTH_ROUTE = "/v1/authz-diagnostics/active-policy/health"
_AUTHZ_ACTIVATION_PREFLIGHT_ROUTE = "/v1/authz-diagnostics/activation-preflight/self"
_AUTHZ_POLICY_CANDIDATE_PREVIEW_ROUTE = "/v1/authz-diagnostics/candidate-policy/preview"
_AUTHZ_REPOSITORY_SCOPE_READ_ROUTE = "/v1/authz-diagnostics/repository-scope/read"
_AUTHZ_DENIAL_EXPLANATION_ROUTE = "/v1/authz-diagnostics/denials/{trace_id}"
_AUTHZ_POLICY_MANAGED_RECONCILE_ROUTE = "/v1/authz-policies/managed-rule-sets/reconcile"
_GENERIC_WEB_PREVIEW_AUTHZ_PLAN_ROUTE = (
    "/v1/authz-policies/managed-rule-sets/generic-web-preview/plan"
)
_AUTH_SESSION_ROUTE = "/v1/auth/session"
_AUTH_GITHUB_LOGIN_ROUTE = "/auth/github/login"
_AUTH_GITHUB_CALLBACK_ROUTE = "/auth/github/callback"
_AUTH_LOGOUT_ROUTE = "/auth/logout"
_LAUNCHPLANE_SERVICE_CONTEXT = "launchplane"
_AGENT_WRITE_INTENT_EVALUATE_ROUTE = "/v1/agent/write-intents/evaluate"
_EVERY_CODE_WORK_REQUEST_RERUN_ROUTE = "/v1/every-code/work-requests/rerun"
_EVERY_CODE_WORK_REQUEST_HEARTBEAT_ROUTE = "/v1/every-code/work-requests/heartbeat"
_EVERY_CODE_WORK_REQUEST_RECOVER_STALE_ROUTE = "/v1/every-code/work-requests/recover-stale"
_AGENT_WRITE_INTENT_MAX_AGE = timedelta(hours=24)
_DB_ONLY_MUTATION_LEASE = timedelta(minutes=5)


class ProductProfileWriteStore(Protocol):
    def read_product_profile_record(self, product: str) -> LaunchplaneProductProfileRecord: ...

    def write_product_profile_record(self, record: LaunchplaneProductProfileRecord) -> object: ...


class PreviewDesiredStateWriteStore(Protocol):
    def write_preview_desired_state_record(
        self,
        record: PreviewDesiredStateRecord,
    ) -> object: ...


class GitHubOAuthLoginClient(Protocol):
    def authorization_url(self, *, state: str, code_challenge: str) -> str: ...

    def fetch_identity(
        self,
        *,
        code: str,
        code_verifier: str,
        authz_policy: LaunchplaneAuthzPolicy,
    ) -> GitHubHumanIdentity: ...


class OAuthLoginStateRepository(Protocol):
    def put(self, *, state: str, code_verifier: str, return_to: str) -> OAuthLoginState: ...

    def pop(self, state: str) -> OAuthLoginState | None: ...


class LaunchplaneErrorDetail(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    message: str


class LaunchplaneErrorResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str = "rejected"
    trace_id: str
    error: LaunchplaneErrorDetail
    records: dict[str, str] | None = Field(
        default=None,
        json_schema_extra={"x-launchplane-optional-response": True},
    )
    authz: dict[str, object] | None = Field(
        default=None,
        json_schema_extra={"x-launchplane-optional-response": True},
    )


@dataclass(frozen=True)
class AuthzPolicyProvenance:
    source: str
    record_id: str
    revision: int
    policy_sha256: str

    @classmethod
    def from_record(cls, record: LaunchplaneAuthzPolicyRecord) -> Self:
        return cls(
            source="db",
            record_id=record.record_id,
            revision=record.revision,
            policy_sha256=record.policy_sha256,
        )


class LaunchplaneHTTPException(HTTPException):
    def __init__(
        self,
        *,
        status_code: int,
        detail: dict[str, object],
        headers: dict[str, str] | None = None,
        authz_evaluation: AuthzEvaluation | None = None,
        authz_policy_provenance: AuthzPolicyProvenance | None = None,
    ) -> None:
        super().__init__(status_code=status_code, detail=detail, headers=headers)
        self.structured_detail = detail
        self.authz_evaluation = authz_evaluation
        self.authz_policy_provenance = authz_policy_provenance


class OdooStableBootstrapOperationActiveResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["rejected"] = "rejected"
    trace_id: str
    error: LaunchplaneErrorDetail
    operation: dict[str, object]


class DurableOperationCancellationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    operation: dict[str, object]


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


class GitHubHumanIdentityResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: Literal["github"] = "github"
    login: str
    github_id: int
    name: str
    email: str
    organizations: tuple[str, ...]
    teams: tuple[str, ...]
    role: Literal["read_only", "admin"]


class AuthSessionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    identity: GitHubHumanIdentityResponse
    csrf_token: str


class AuthSessionRequiredResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["rejected"] = "rejected"
    trace_id: str
    error: LaunchplaneErrorDetail
    configured: bool


class AuthLogoutResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str


def bounded_request_body_contract(path: str) -> tuple[str, int, bool, bool] | None:
    contract = _BOUNDED_REQUEST_BODY_CONTRACTS.get(path)
    if contract is not None:
        return contract
    path_parts = path.strip("/").split("/")
    if (
        len(path_parts) == 7
        and path_parts[:2] == ["v1", "products"]
        and path_parts[3] == "environments"
        and path_parts[5:] == ["config", "apply"]
    ):
        return "Product config", _PRODUCT_CONFIG_MAX_BODY_BYTES, True, True
    return None


class BoundedRequestBodyMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http" or str(scope.get("method", "")).upper() != "POST":
            await self.app(scope, receive, send)
            return
        contract = bounded_request_body_contract(str(scope.get("path", "")))
        if contract is None:
            await self.app(scope, receive, send)
            return
        request_label, max_body_bytes, require_json, require_exact_length = contract

        if require_json:
            content_type_values = _asgi_header_values(scope=scope, name="content-type")
            content_type = content_type_values[0] if len(content_type_values) == 1 else ""
            media_type = content_type.split(";", 1)[0].strip().lower()
            if media_type != "application/json":
                await _send_launchplane_error_response(
                    scope=scope,
                    receive=receive,
                    send=send,
                    status_code=400,
                    code="invalid_request",
                    message=f"{request_label} requests require Content-Type: application/json.",
                )
                return

        transfer_encoding_tokens = {
            token.split(";", 1)[0].strip().lower()
            for value in _asgi_header_values(scope=scope, name="transfer-encoding")
            for token in value.split(",")
            if token.strip()
        }
        if "chunked" in transfer_encoding_tokens or (
            require_exact_length and transfer_encoding_tokens
        ):
            await _send_launchplane_error_response(
                scope=scope,
                receive=receive,
                send=send,
                status_code=413,
                code="request_entity_too_large",
                message=f"{request_label} requests require a bounded Content-Length.",
            )
            return

        content_length_values = _asgi_header_values(scope=scope, name="content-length")
        if not content_length_values:
            await _send_launchplane_error_response(
                scope=scope,
                receive=receive,
                send=send,
                status_code=413,
                code="request_entity_too_large",
                message=f"{request_label} requests require a bounded Content-Length.",
            )
            return
        if len(content_length_values) != 1:
            await _send_launchplane_error_response(
                scope=scope,
                receive=receive,
                send=send,
                status_code=400,
                code="invalid_request",
                message=f"{request_label} requests require exactly one Content-Length header.",
            )
            return
        content_length = content_length_values[0].strip()
        if not content_length or not all("0" <= character <= "9" for character in content_length):
            await _send_launchplane_error_response(
                scope=scope,
                receive=receive,
                send=send,
                status_code=400,
                code="invalid_request",
                message=f"{request_label} Content-Length must be an unsigned decimal integer.",
            )
            return
        normalized_content_length = content_length.lstrip("0") or "0"
        max_body_size_text = str(max_body_bytes)
        if len(normalized_content_length) > len(max_body_size_text) or (
            len(normalized_content_length) == len(max_body_size_text)
            and normalized_content_length > max_body_size_text
        ):
            await _send_launchplane_error_response(
                scope=scope,
                receive=receive,
                send=send,
                status_code=413,
                code="request_entity_too_large",
                message=f"{request_label} request body is too large.",
            )
            return
        declared_body_size = int(normalized_content_length)

        buffered_messages: list[Message] = []
        observed_body_size = 0
        while True:
            message = await receive()
            if message.get("type") == "http.disconnect":
                return
            if message.get("type") != "http.request":
                await _send_launchplane_error_response(
                    scope=scope,
                    receive=receive,
                    send=send,
                    status_code=400,
                    code="invalid_request",
                    message=f"{request_label} request body ended before completion.",
                )
                return
            body = message.get("body", b"")
            if not isinstance(body, bytes):
                await _send_launchplane_error_response(
                    scope=scope,
                    receive=receive,
                    send=send,
                    status_code=400,
                    code="invalid_request",
                    message=f"{request_label} request body is invalid.",
                )
                return
            observed_body_size += len(body)
            if observed_body_size > max_body_bytes:
                await _send_launchplane_error_response(
                    scope=scope,
                    receive=receive,
                    send=send,
                    status_code=413,
                    code="request_entity_too_large",
                    message=f"{request_label} request body is too large.",
                )
                return
            buffered_messages.append(message)
            if not message.get("more_body", False):
                break

        if require_exact_length and observed_body_size != declared_body_size:
            await _send_launchplane_error_response(
                scope=scope,
                receive=receive,
                send=send,
                status_code=400,
                code="invalid_request",
                message=f"{request_label} Content-Length does not match the request body.",
            )
            return

        next_message_index = 0

        async def replay_receive() -> Message:
            nonlocal next_message_index
            if next_message_index < len(buffered_messages):
                buffered_message = buffered_messages[next_message_index]
                next_message_index += 1
                return buffered_message
            return await receive()

        await self.app(scope, replay_receive, send)


def _asgi_header_values(*, scope: Scope, name: str) -> list[str]:
    header_values: list[str] = []
    normalized_name = name.lower().encode("latin-1")
    for raw_name, raw_value in scope.get("headers", []):
        if raw_name.lower() == normalized_name:
            header_values.append(raw_value.decode("latin-1"))
    return header_values


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


async def _send_launchplane_error_response(
    *,
    scope: Scope,
    receive: Receive,
    send: Send,
    status_code: int,
    code: str,
    message: str,
) -> None:
    payload = LaunchplaneErrorResponse(
        trace_id=f"launchplane_req_{uuid4().hex}",
        error=LaunchplaneErrorDetail(code=code, message=message),
    )
    response = JSONResponse(
        status_code=status_code,
        content=payload.model_dump(mode="json", exclude_none=True),
    )
    await response(scope, receive, send)


class LaunchplaneRuntimeStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    authz_policy_sha256: str
    authz_policy_schema_version: Literal[1, 2]
    authz_policy_source: str
    bootstrap_authz_policy_sha256: str
    compatible_database_schema_revisions: tuple[str, ...]
    database_schema_revision: str
    deployment_marker: str
    docker_image_reference: str
    schema_migration_target_revision: str
    service_audience: str
    storage_backend: str


class LaunchplaneRuntimeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    runtime: LaunchplaneRuntimeStatus


class LaunchplaneActiveAuthzPolicyResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    policy: dict[str, object]


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
    reconciled_restore_ids: tuple[str, ...]
    reconciled_replacement_ids: tuple[str, ...]
    reconciled_count: int


class OdooStableOperationWorkerReconcileResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    reconcile_result: OdooStableOperationWorkerReconcileResultResponse


class VeriReelProdBackupGateOperationLeaseSummaryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation_id: str
    product: str
    context: str
    instance: str
    backup_record_id: str
    status: str
    phase: str
    attempt: int
    lease_owner: str
    lease_expires_at: str
    heartbeat_at: str
    heartbeat_age_seconds: int | None
    lease_expired: bool


class VeriReelProdBackupGateOperationWorkerStatusResponseModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str
    recorded_at: str
    pending_count: int
    running_count: int
    stalled_count: int
    terminal_count: int
    counts_by_status: dict[str, int]
    operations: tuple[VeriReelProdBackupGateOperationLeaseSummaryResponse, ...]


class VeriReelProdBackupGateOperationWorkerStatusResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    worker_status: VeriReelProdBackupGateOperationWorkerStatusResponseModel


class VeriReelProdBackupGateOperationWorkerReconcileResultResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reconciled_operation_ids: tuple[str, ...]
    reconciled_count: int


class VeriReelProdBackupGateOperationWorkerReconcileResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    reconcile_result: VeriReelProdBackupGateOperationWorkerReconcileResultResponse


class EveryCodeWorkRequestClaimEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str
    host: str
    lease_seconds: int = Field(default=1800, ge=60, le=86400)

    @model_validator(mode="after")
    def _validate_claim(self) -> "EveryCodeWorkRequestClaimEnvelope":
        if not self.request_id.strip():
            raise ValueError("Every Code work request claim requires request_id")
        if not self.host.strip():
            raise ValueError("Every Code work request claim requires host")
        return self


class EveryCodeWorkRequestHeartbeatEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str
    host: str
    fencing_token: int = Field(ge=1)
    lease_seconds: int = Field(default=1800, ge=60, le=86400)

    @model_validator(mode="after")
    def _validate_heartbeat(self) -> "EveryCodeWorkRequestHeartbeatEnvelope":
        if not self.request_id.strip():
            raise ValueError("Every Code heartbeat requires request_id")
        if not self.host.strip():
            raise ValueError("Every Code heartbeat requires host")
        return self


class EveryCodeWorkRequestStatusEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str
    host: str
    state: Literal["running", "done", "blocked"]
    fencing_token: int = Field(ge=1)
    result_pr_url: str = ""
    result_summary: str = ""
    error_message: str = ""
    updated_at: str = ""

    @model_validator(mode="after")
    def _validate_status(self) -> "EveryCodeWorkRequestStatusEnvelope":
        if not self.request_id.strip():
            raise ValueError("Every Code work request status requires request_id")
        if not self.host.strip():
            raise ValueError("Every Code work request status requires host")
        return self


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


class _OdooPreviewProviderMutationAdapter:
    def __init__(
        self,
        *,
        control_plane_root: FilePath,
        record_store: object,
        profile: LaunchplaneProductProfileRecord,
        apply_request: OdooPreviewApplyEnvelope,
        issued_plan: OdooPreviewApplyInputsResult,
        database_url: str | None,
        trace_id: str,
        deployment_record_id: str,
    ) -> None:
        self._control_plane_root = control_plane_root
        self._record_store = record_store
        self._profile = profile
        self._apply_request = apply_request
        self._issued_plan = issued_plan
        self._database_url = database_url
        self._trace_id = trace_id
        self._deployment_record_id = deployment_record_id
        self._runtime_identity = (
            build_odoo_preview_runtime_identity(
                profile=profile,
                issued_plan=issued_plan,
                deployment_record_id=deployment_record_id,
            )
            if issued_plan.operation == "refresh"
            else None
        )

    def reconciliation_key(self) -> str:
        plan = self._apply_request.apply.dry_run_plan
        return (
            f"dokploy:compose:{self._profile.preview.context.strip()}:{plan.compose_name.strip()}"
        )

    def target_key(self) -> str:
        reconciliation_key = self.reconciliation_key()
        return f"dokploy-provider-target:{hashlib.sha256(reconciliation_key.encode()).hexdigest()}"

    def _destroy_invalidation_records(self) -> dict[str, object]:
        provenance = self._issued_plan.plan_provenance
        if provenance is None:
            raise ValueError("Odoo preview destroy requires issued plan provenance.")
        result = record_manager_preview_approval_invalidation_for_pr(
            repository=self._profile.repository,
            pr_number=self._issued_plan.plan_request.pr_number,
            reason="The serving preview was destroyed.",
            source_event_kind="preview_destroy",
            source_event_id=f"odoo-preview-destroy:{provenance.plan_id}",
            record_store=cast(Any, self._record_store),
            occurred_at=format_launchplane_mutation_timestamp(provenance.issued_at),
        )
        return {
            "manager_preview_approval_required": bool(result.get("required")),
            "manager_preview_invalidation_event_status": string_value(
                result.get("event_status") or ""
            ),
        }

    def _finalize_successful_result(
        self,
        driver_result: dict[str, object],
    ) -> tuple[dict[str, object], dict[str, object], int]:
        lifecycle_records = apply_odoo_preview_lifecycle_evidence(
            control_plane_root_path=self._control_plane_root,
            record_store=self._record_store,
            profile=self._profile,
            issued_plan=self._issued_plan,
            driver_result=driver_result,
            runtime_identity=self._runtime_identity,
            before_destroy=(
                self._destroy_invalidation_records
                if self._issued_plan.operation == "destroy"
                else None
            ),
        )
        lifecycle_status = string_value(
            lifecycle_records.get("lifecycle_evidence_status") or ""
        ).strip()
        if lifecycle_status == "stale":
            blocked_result = OdooPreviewDokployApplyResult.model_validate(driver_result).model_copy(
                update={
                    "status": "blocked",
                    "error_message": (
                        "The Odoo preview operation completed at the provider but was "
                        "superseded by newer Launchplane lifecycle authority."
                    ),
                }
            )
            return blocked_result.model_dump(mode="json"), lifecycle_records, 409
        if lifecycle_status not in {"applied", "replayed"}:
            raise ValueError("Successful Odoo preview apply requires lifecycle evidence.")
        return driver_result, lifecycle_records, 202

    def observe(
        self,
        provider_operation_key: str,
        provider_effect_phase: str,
        reconciliation_key: str,
    ) -> ProviderObservation:
        del reconciliation_key
        observation_outcome, driver_result, retry_safe = observe_odoo_preview_apply_result(
            control_plane_root_path=self._control_plane_root,
            profile=self._profile,
            request=self._apply_request,
            database_url=self._database_url,
            provider_operation_title=provider_operation_title(provider_operation_key),
            provider_effect_phase=provider_effect_phase,
        )
        if driver_result is None:
            return ProviderObservation(
                outcome=cast(ProviderObservationOutcome, observation_outcome),
                retry_safe=retry_safe,
            )
        driver_result.pop("provider_effect_attempted", None)
        response_status_code = 202
        records: dict[str, object] = {}
        if string_value(driver_result.get("status", "")).strip() == "pass":
            driver_result, records, response_status_code = self._finalize_successful_result(
                driver_result
            )
        terminal_failure = string_value(driver_result.get("status", "")).strip() == "fail"
        return ProviderObservation(
            outcome="present",
            response_status_code=502 if terminal_failure else response_status_code,
            response_payload=_provider_operation_response_payload(
                trace_id=self._trace_id,
                records=records,
                result=driver_result,
            ),
        )

    def apply(
        self, provider_operation_key: str, lease: ProviderOperationLease
    ) -> ProviderMutationOutcome:
        try:
            driver_result = execute_odoo_preview_apply_result(
                control_plane_root_path=self._control_plane_root,
                record_store=self._record_store,
                profile=self._profile,
                request=self._apply_request,
                issued_plan=self._issued_plan,
                database_url=self._database_url,
                provider_operation_title=provider_operation_title(provider_operation_key),
                provider_effect_checkpoint=lease.checkpoint_effect,
                provider_lease_check=lease.assert_current,
                deployment_record_id=self._deployment_record_id,
                runtime_identity=self._runtime_identity,
            )
        except (
            OdooPreviewApplyConfigError,
            FileNotFoundError,
            ValueError,
            click.ClickException,
        ) as error:
            raise ProviderMutationRejectedError(error)
        provider_effect_attempted = driver_result.pop("provider_effect_attempted", False) is True
        driver_status = string_value(driver_result.get("status", "")).strip()
        if driver_status == "fail" and provider_effect_attempted:
            raise ProviderMutationUnknownError(
                string_value(driver_result.get("error_message", "")).strip()
                or "Odoo preview provider outcome requires reconciliation."
            )
        response_status_code = 202
        records: dict[str, object] = {}
        lifecycle_finalized = driver_status == "pass"
        if lifecycle_finalized:
            lease.assert_current()
            driver_result, records, response_status_code = self._finalize_successful_result(
                driver_result
            )
            lease.assert_current()
            driver_status = string_value(driver_result.get("status", "")).strip()
        return ProviderMutationOutcome(
            response_status_code=response_status_code,
            response_payload=_provider_operation_response_payload(
                trace_id=self._trace_id,
                records=records,
                result=driver_result,
            ),
            durable=lifecycle_finalized
            or (
                not driver_result_contains_status(driver_result, "blocked")
                and driver_status != "fail"
            ),
            provider_effect_performed=provider_effect_attempted,
        )


class ProductOnboardingApplyEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    product: str
    mode: Literal["dry_run", "apply"] = "apply"
    reviewed_plan_sha256: str = ""
    manifest: ProductOnboardingManifest | None = None
    generic_web: GenericWebOnboardingIntent | None = None
    resolved_target_id: str = ""

    @model_validator(mode="after")
    def _validate_alignment(self) -> "ProductOnboardingApplyEnvelope":
        if self.product.strip() != "launchplane":
            raise ValueError("Product onboarding writes require product 'launchplane'.")
        self.product = "launchplane"
        self.reviewed_plan_sha256 = self.reviewed_plan_sha256.strip().lower()
        self.resolved_target_id = self.resolved_target_id.strip()
        if (self.manifest is None) == (self.generic_web is None):
            raise ValueError("Product onboarding requires exactly one of manifest or generic_web.")
        if self.manifest is not None:
            if self.mode != "apply":
                raise ValueError("Legacy product onboarding manifests support apply mode only.")
            if self.reviewed_plan_sha256 or self.resolved_target_id:
                raise ValueError(
                    "Legacy product onboarding manifests reject generic-web plan fields."
                )
            return self
        if self.mode == "dry_run":
            if self.reviewed_plan_sha256 or self.resolved_target_id:
                raise ValueError(
                    "Generic-web onboarding dry-run rejects reviewed plan and target id."
                )
            return self
        if re.fullmatch(r"[0-9a-f]{64}", self.reviewed_plan_sha256) is None:
            raise ValueError("Generic-web onboarding apply requires reviewed_plan_sha256.")
        if not self.resolved_target_id:
            raise ValueError("Generic-web onboarding apply requires resolved_target_id.")
        return self


class ProductExpectedConfigApplyEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    product: str
    mode: Literal["dry-run", "apply"] = "dry-run"
    reason: str
    source_label: str = "product-expected-config"
    runtime_environment_keys: tuple[ProductRuntimeConfigRequirement, ...] = ()
    managed_secret_bindings: tuple[ProductSecretConfigRequirement, ...] = ()

    @model_validator(mode="after")
    def _validate_request(self) -> "ProductExpectedConfigApplyEnvelope":
        self.product = self.product.strip()
        self.reason = self.reason.strip()
        self.source_label = self.source_label.strip() or "product-expected-config"
        if not self.product:
            raise ValueError("Product expected config request requires product.")
        if not self.reason:
            raise ValueError("Product expected config request requires reason.")
        if not self.runtime_environment_keys and not self.managed_secret_bindings:
            raise ValueError(
                "Product expected config request requires at least one runtime key "
                "or managed secret binding."
            )
        return self


def _runtime_config_requirement_key(
    requirement: ProductRuntimeConfigRequirement,
) -> tuple[str, str, str]:
    return requirement.context, requirement.instance, requirement.key


def _secret_config_requirement_key(
    requirement: ProductSecretConfigRequirement,
) -> tuple[str, str, str, str]:
    return (
        requirement.integration,
        requirement.context,
        requirement.instance,
        requirement.binding_key,
    )


def _merge_product_expected_config(
    *,
    profile: LaunchplaneProductProfileRecord,
    request: ProductExpectedConfigApplyEnvelope,
    updated_at: str,
) -> tuple[LaunchplaneProductProfileRecord, dict[str, object]]:
    existing_runtime_keys = {
        _runtime_config_requirement_key(requirement)
        for requirement in profile.expected_config.runtime_environment_keys
    }
    existing_secret_keys = {
        _secret_config_requirement_key(requirement)
        for requirement in profile.expected_config.managed_secret_bindings
    }

    runtime_requirements = list(profile.expected_config.runtime_environment_keys)
    secret_requirements = list(profile.expected_config.managed_secret_bindings)
    added_runtime_requirements: list[ProductRuntimeConfigRequirement] = []
    unchanged_runtime_requirements: list[ProductRuntimeConfigRequirement] = []
    added_secret_requirements: list[ProductSecretConfigRequirement] = []
    unchanged_secret_requirements: list[ProductSecretConfigRequirement] = []

    for runtime_requirement in request.runtime_environment_keys:
        runtime_key = _runtime_config_requirement_key(runtime_requirement)
        if runtime_key in existing_runtime_keys:
            unchanged_runtime_requirements.append(runtime_requirement)
            continue
        existing_runtime_keys.add(runtime_key)
        runtime_requirements.append(runtime_requirement)
        added_runtime_requirements.append(runtime_requirement)

    for secret_requirement in request.managed_secret_bindings:
        secret_key = _secret_config_requirement_key(secret_requirement)
        if secret_key in existing_secret_keys:
            unchanged_secret_requirements.append(secret_requirement)
            continue
        existing_secret_keys.add(secret_key)
        secret_requirements.append(secret_requirement)
        added_secret_requirements.append(secret_requirement)

    changed = bool(added_runtime_requirements or added_secret_requirements)
    merged_profile = profile
    if changed:
        merged_profile = profile.model_copy(
            update={
                "expected_config": ProductExpectedConfigProfile(
                    runtime_environment_keys=tuple(runtime_requirements),
                    managed_secret_bindings=tuple(secret_requirements),
                ),
                "updated_at": updated_at,
                "source": request.source_label,
            }
        )

    return merged_profile, {
        "status": "ok",
        "mode": request.mode,
        "product": request.product,
        "source_label": request.source_label,
        "changed": changed,
        "runtime_environment_keys": {
            "added": [
                requirement.model_dump(mode="json") for requirement in added_runtime_requirements
            ],
            "unchanged": [
                requirement.model_dump(mode="json")
                for requirement in unchanged_runtime_requirements
            ],
        },
        "managed_secret_bindings": {
            "added": [
                requirement.model_dump(mode="json") for requirement in added_secret_requirements
            ],
            "unchanged": [
                requirement.model_dump(mode="json") for requirement in unchanged_secret_requirements
            ],
        },
        "summary": {
            "runtime_environment_key_add_count": len(added_runtime_requirements),
            "managed_secret_binding_add_count": len(added_secret_requirements),
            "runtime_environment_key_unchanged_count": len(unchanged_runtime_requirements),
            "managed_secret_binding_unchanged_count": len(unchanged_secret_requirements),
        },
    }


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


class PreviewPrFeedbackEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    product: str
    context: str = ""
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
    instance: str = ""
    ingress: NpmplusIngressApplyRequest

    @field_validator("product", "context", mode="after")
    @classmethod
    def _validate_required_text(cls, value: str) -> str:
        normalized_value = value.strip()
        if not normalized_value:
            raise ValueError("NPMplus ingress apply requires non-empty product/context")
        return normalized_value

    @field_validator("instance", mode="after")
    @classmethod
    def _normalize_instance(cls, value: str) -> str:
        return value.strip()

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


class RouteBindingReconcileEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    mode: Literal["dry-run", "apply"] = "dry-run"
    product: str
    context: str
    instance: str
    expected_current: control_plane_route_binding_reconcile.RouteBindingExpectedCurrent
    source_label: str = "operator-reconcile"
    reason: str = ""
    confirmation: str = ""

    @model_validator(mode="after")
    def _validate_envelope(self) -> "RouteBindingReconcileEnvelope":
        if self.schema_version != 1:
            raise ValueError("Unsupported route binding reconcile schema version")
        self.product = self.product.strip()
        self.context = self.context.strip()
        self.instance = self.instance.strip()
        self.source_label = self.source_label.strip()
        self.reason = self.reason.strip()
        self.confirmation = self.confirmation.strip()
        if not self.product or not self.context or not self.instance:
            raise ValueError("Route binding reconcile requires product, context, and instance")
        if not self.source_label:
            raise ValueError("Route binding reconcile requires source_label")
        if self.mode == "apply":
            if not self.reason:
                raise ValueError("Route binding reconcile apply requires a reason")
            if self.confirmation != "APPLY LAUNCHPLANE ROUTE BINDING RECONCILE":
                raise ValueError("Route binding reconcile apply requires exact confirmation text")
        return self


class OdooTestingRouteBindingRefreshEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    mode: Literal["dry-run", "apply"] = "dry-run"
    reason: str = ""
    confirmation: str = ""

    @model_validator(mode="after")
    def _validate_envelope(self) -> "OdooTestingRouteBindingRefreshEnvelope":
        if self.schema_version != 1:
            raise ValueError("Unsupported Odoo testing route binding refresh schema version")
        self.reason = self.reason.strip()
        self.confirmation = self.confirmation.strip()
        if not self.reason:
            raise ValueError("Odoo testing route binding refresh requires a reason")
        if self.mode == "apply" and (
            self.confirmation != "APPLY ODOO TESTING ROUTE BINDING REFRESH"
        ):
            raise ValueError(
                "Odoo testing route binding refresh apply requires exact confirmation text"
            )
        if self.mode == "dry-run" and self.confirmation:
            raise ValueError("Odoo testing route binding refresh dry-run rejects confirmation")
        return self


class ExternalRouteBindingReconcileEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    mode: Literal["dry-run", "apply"] = "dry-run"
    product: str
    context: str
    instance: str
    expected_current: control_plane_route_binding_reconcile.RouteBindingExpectedCurrent
    desired_status: Literal["active", "disabled"] = "active"
    source_label: str = "operator-external-reconcile"
    reason: str = ""
    confirmation: str = ""

    @model_validator(mode="after")
    def _validate_envelope(self) -> "ExternalRouteBindingReconcileEnvelope":
        if self.schema_version != 1:
            raise ValueError("Unsupported external route binding reconcile schema version")
        self.product = self.product.strip()
        self.context = self.context.strip()
        self.instance = self.instance.strip()
        self.source_label = self.source_label.strip()
        self.reason = self.reason.strip()
        self.confirmation = self.confirmation.strip()
        if not self.product or not self.context or not self.instance:
            raise ValueError(
                "External route binding reconcile requires product, context, and instance"
            )
        if not self.source_label:
            raise ValueError("External route binding reconcile requires source_label")
        if self.mode == "apply":
            if not self.reason:
                raise ValueError("External route binding reconcile apply requires a reason")
            if self.confirmation != "APPLY EXTERNAL ROUTE BINDING RECONCILE":
                raise ValueError(
                    "External route binding reconcile apply requires exact confirmation text"
                )
        return self


class _RecordStoreFactory(Protocol):
    def __call__(self) -> object: ...


class _IngressProviderFactory(Protocol):
    def __call__(self) -> IngressProvider: ...


class _NpmplusIngressClientFactory(Protocol):
    def __call__(self) -> NpmplusIngressClient: ...


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


class _PreviewPrFeedbackWriteStore(Protocol):
    def write_preview_pr_feedback_record(
        self,
        record: PreviewPrFeedbackRecord,
    ) -> object: ...


class _PreviewPrFeedbackRemediationWriteStore(
    _PreviewPrFeedbackWriteStore,
    PreviewPrFeedbackRemediationStore,
    Protocol,
):
    def write_preview_pr_feedback_remediation_record(
        self,
        record: PreviewPrFeedbackRemediationRecord,
    ) -> object: ...

    def write_preview_pr_feedback_remediation_bundle(
        self,
        *,
        remediation_record: PreviewPrFeedbackRemediationRecord,
        feedback_record: PreviewPrFeedbackRecord,
        idempotency_record: LaunchplaneIdempotencyRecord,
    ) -> object: ...


class _PreviewLifecyclePlanApplyStore(Protocol):
    def list_preview_inventory_scan_records(
        self,
        *,
        context_name: str | None = None,
        limit: int | None = 50,
        offset: int = 0,
    ) -> tuple[PreviewInventoryScanRecord, ...]: ...

    def write_preview_lifecycle_plan_record(
        self,
        record: PreviewLifecyclePlanRecord,
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


class _RouteBindingReconcileStore(
    control_plane_route_binding_reconcile.RouteBindingReconcileStore, Protocol
): ...


class _ExternalRouteBindingReconcileStore(
    control_plane_route_binding_external_reconcile.ExternalRouteBindingReconcileStore,
    Protocol,
): ...


class _RouteBindingMutationStore(_RouteBindingReconcileStore, Protocol):
    def prepare_db_only_mutation(
        self,
        *,
        scope: str,
        route_path: str,
        idempotency_key: str,
        request_fingerprint: str,
    ) -> DbOnlyMutationPreflightResult: ...

    def reconcile_route_binding_record(
        self,
        *,
        expected_record: EnvironmentRouteBindingRecord | None,
        replacement_record: EnvironmentRouteBindingRecord,
        mutation: DbOnlyMutationRequest,
    ) -> RouteBindingReconcileWriteResult: ...


class _RouteBindingRefreshControllerReadStore(
    control_plane_route_binding_refresh_controller.RouteBindingRefreshControllerStore,
    Protocol,
): ...


class _RouteBindingRefreshControllerStore(
    _RouteBindingRefreshControllerReadStore,
    _RouteBindingMutationStore,
    Protocol,
):
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
    ) -> MutationReservationResult: ...

    def complete_mutation_reservation(
        self,
        *,
        completion: LaunchplaneIdempotencyRecord,
    ) -> MutationReservationCompletionResult: ...


class _ExternalRouteBindingMutationStore(_ExternalRouteBindingReconcileStore, Protocol):
    def prepare_db_only_mutation(
        self,
        *,
        scope: str,
        route_path: str,
        idempotency_key: str,
        request_fingerprint: str,
    ) -> DbOnlyMutationPreflightResult: ...

    def reconcile_route_binding_record(
        self,
        *,
        expected_record: EnvironmentRouteBindingRecord | None,
        replacement_record: EnvironmentRouteBindingRecord,
        mutation: DbOnlyMutationRequest,
    ) -> RouteBindingReconcileWriteResult: ...


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


class _EveryCodeWorkRequestWriteStore(Protocol):
    def write_every_code_work_request_record(
        self, record: EveryCodeWorkRequestRecord
    ) -> object: ...


class _EveryCodeWorkRequestClaimStore(Protocol):
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
    ) -> EveryCodeWorkRequestRecord | None: ...


class _EveryCodeWorkRequestHeartbeatStore(Protocol):
    def heartbeat_every_code_work_request_record(
        self,
        *,
        request_id: str,
        host: str,
        fencing_token: int,
        heartbeat_at: str,
        lease_expires_at: str,
    ) -> bool: ...


class _EveryCodeWorkRequestStaleStore(Protocol):
    def list_stale_every_code_work_request_records(
        self,
        *,
        as_of: str,
        limit: int = 50,
    ) -> tuple[EveryCodeWorkRequestRecord, ...]: ...

    def recover_stale_every_code_work_request_record(
        self,
        *,
        expected_record: EveryCodeWorkRequestRecord,
        recovered_at: str,
    ) -> EveryCodeWorkRequestRecord | None: ...


class _EveryCodeWorkRequestStatusStore(Protocol):
    def update_every_code_work_request_status_record(
        self,
        *,
        request_id: str,
        update: EveryCodeWorkRequestStatusUpdate,
    ) -> EveryCodeWorkRequestRecord: ...


class _EveryCodeWorkRequestRerunStore(Protocol):
    def read_every_code_work_request_record(
        self, request_id: str
    ) -> EveryCodeWorkRequestRecord: ...

    def compare_and_write_every_code_work_request_record(
        self,
        *,
        expected_record: EveryCodeWorkRequestRecord,
        record: EveryCodeWorkRequestRecord,
        idempotency_record: LaunchplaneIdempotencyRecord | None = None,
    ) -> Literal["updated", "changed", "missing"]: ...


class _AgentWriteIntentRecordReadStore(Protocol):
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


class _AgentWriteIntentRecordWriteStore(Protocol):
    def write_agent_write_intent_record(self, record: AgentWriteIntentRecord) -> object: ...


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


class _EveryCodePreviewGateWriteStore(Protocol):
    def write_every_code_preview_gate_record(
        self, record: EveryCodePreviewGateRecord
    ) -> object: ...


def require_product_profile_write_store(record_store: object) -> ProductProfileWriteStore:
    missing_methods = [
        method_name
        for method_name in (
            "read_product_profile_record",
            "write_product_profile_record",
        )
        if not callable(getattr(record_store, method_name, None))
    ]
    if missing_methods:
        raise TypeError(
            "Launchplane record store does not support product profile writes: "
            + ", ".join(missing_methods)
        )
    return cast(ProductProfileWriteStore, record_store)


def require_public_ingress_monitor_store(
    record_store: object, *, notify: bool
) -> PublicIngressMonitorStore:
    required_methods = [
        "list_product_profile_records",
        "list_route_binding_records",
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


def require_route_binding_reconcile_store(record_store: object) -> _RouteBindingReconcileStore:
    required_methods = (
        "read_provider_target_record",
        "read_dokploy_target_record",
        "read_dokploy_target_id_record",
        "read_route_binding_record",
        "list_edge_endpoint_records",
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
            "Launchplane record store does not support route binding reconciliation: "
            f"{missing_summary}"
        )
    return cast(_RouteBindingReconcileStore, record_store)


def require_external_route_binding_reconcile_store(
    record_store: object,
) -> _ExternalRouteBindingReconcileStore:
    required_methods = (
        "read_product_profile_record",
        "read_provider_target_record",
        "read_route_binding_record",
    )
    missing_methods = [
        method_name
        for method_name in required_methods
        if not callable(getattr(record_store, method_name, None))
    ]
    if missing_methods:
        missing_summary = ", ".join(missing_methods)
        raise TypeError(
            "Launchplane record store does not support external route binding reconciliation: "
            f"{missing_summary}"
        )
    return cast(_ExternalRouteBindingReconcileStore, record_store)


def require_route_binding_mutation_store(record_store: object) -> _RouteBindingMutationStore:
    route_binding_store = require_route_binding_reconcile_store(record_store)
    required_methods = ("prepare_db_only_mutation", "reconcile_route_binding_record")
    missing_methods = [
        method_name
        for method_name in required_methods
        if not callable(getattr(route_binding_store, method_name, None))
    ]
    if missing_methods:
        missing_summary = ", ".join(missing_methods)
        raise TypeError(
            "Launchplane record store does not support atomic route binding reconciliation: "
            f"{missing_summary}"
        )
    return cast(_RouteBindingMutationStore, route_binding_store)


def require_route_binding_refresh_controller_read_store(
    record_store: object,
) -> _RouteBindingRefreshControllerReadStore:
    route_binding_store = require_route_binding_reconcile_store(record_store)
    if not callable(getattr(route_binding_store, "list_product_profile_records", None)):
        raise TypeError(
            "Launchplane record store does not support Odoo testing route binding refresh "
            "target discovery: list_product_profile_records"
        )
    return cast(_RouteBindingRefreshControllerReadStore, route_binding_store)


def require_route_binding_refresh_controller_store(
    record_store: object,
) -> _RouteBindingRefreshControllerStore:
    route_binding_store = require_route_binding_refresh_controller_read_store(record_store)
    required_methods = (
        "prepare_db_only_mutation",
        "reserve_mutation",
        "complete_mutation_reservation",
        "reconcile_route_binding_record",
    )
    missing_methods = [
        method_name
        for method_name in required_methods
        if not callable(getattr(route_binding_store, method_name, None))
    ]
    if missing_methods:
        missing_summary = ", ".join(missing_methods)
        raise TypeError(
            "Launchplane record store does not support atomic Odoo testing route binding "
            f"refresh: {missing_summary}"
        )
    return cast(_RouteBindingRefreshControllerStore, route_binding_store)


def require_external_route_binding_mutation_store(
    record_store: object,
) -> _ExternalRouteBindingMutationStore:
    route_binding_store = require_external_route_binding_reconcile_store(record_store)
    required_methods = ("prepare_db_only_mutation", "reconcile_route_binding_record")
    missing_methods = [
        method_name
        for method_name in required_methods
        if not callable(getattr(route_binding_store, method_name, None))
    ]
    if missing_methods:
        missing_summary = ", ".join(missing_methods)
        raise TypeError(
            "Launchplane record store does not support atomic external route binding "
            f"reconciliation: {missing_summary}"
        )
    return cast(_ExternalRouteBindingMutationStore, route_binding_store)


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


def require_preview_lifecycle_plan_apply_store(
    record_store: object,
) -> _PreviewLifecyclePlanApplyStore:
    required_methods = (
        "list_preview_inventory_scan_records",
        "write_preview_lifecycle_plan_record",
    )
    missing_methods = [
        method_name
        for method_name in required_methods
        if not callable(getattr(record_store, method_name, None))
    ]
    if missing_methods:
        missing_summary = ", ".join(missing_methods)
        raise TypeError(
            "Launchplane record store does not support preview lifecycle plan applies: "
            f"{missing_summary}"
        )
    return cast(_PreviewLifecyclePlanApplyStore, record_store)


def require_preview_desired_state_write_store(
    record_store: object,
) -> PreviewDesiredStateWriteStore:
    write_record = getattr(record_store, "write_preview_desired_state_record", None)
    if not callable(write_record):
        raise TypeError(
            "Launchplane record store does not support preview desired-state writes: "
            "write_preview_desired_state_record"
        )
    return cast(PreviewDesiredStateWriteStore, record_store)


def require_preview_pr_feedback_write_store(
    record_store: object,
) -> _PreviewPrFeedbackWriteStore:
    write_record = getattr(record_store, "write_preview_pr_feedback_record", None)
    if not callable(write_record):
        raise TypeError(
            "Launchplane record store does not support preview PR feedback writes: "
            "write_preview_pr_feedback_record"
        )
    return cast(_PreviewPrFeedbackWriteStore, record_store)


def require_preview_pr_feedback_remediation_write_store(
    record_store: object,
) -> _PreviewPrFeedbackRemediationWriteStore:
    required_methods = (
        "read_product_profile_record",
        "list_preview_pr_feedback_remediation_records",
        "write_preview_pr_feedback_record",
        "write_preview_pr_feedback_remediation_record",
        "write_preview_pr_feedback_remediation_bundle",
    )
    missing_methods = [
        method_name
        for method_name in required_methods
        if not callable(getattr(record_store, method_name, None))
    ]
    if missing_methods:
        raise TypeError(
            "Launchplane record store does not support preview PR feedback remediation "
            f"writes: {', '.join(missing_methods)}"
        )
    return cast(_PreviewPrFeedbackRemediationWriteStore, record_store)


def supports_every_code_work_requests(record_store: object) -> bool:
    return hasattr(record_store, "list_every_code_work_request_records")


def allows_preview_pr_feedback_write(
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


def resolve_preview_pr_feedback_context(*, record_store: object, product: str, context: str) -> str:
    requested_context = context.strip()
    try:
        profile_store = require_product_profile_read_store(record_store)
        profile = profile_store.read_product_profile_record(product.strip())
    except TypeError as error:
        raise TypeError(
            "Preview PR feedback context derivation requires product profile reads: "
            "read_product_profile_record"
        ) from error
    except FileNotFoundError as error:
        raise FileNotFoundError(
            "Preview PR feedback context derivation requires an existing product profile."
        ) from error
    if not profile.preview.enabled or not profile.preview.context.strip():
        raise ValueError("Product profile does not define an enabled preview context.")
    profile_context = profile.preview.context.strip()
    if requested_context and requested_context != profile_context:
        raise ValueError("Preview PR feedback context does not match product profile.")
    return profile_context


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


def canonical_request_payload_for_idempotency(
    *, route_path: str, payload: dict[str, object]
) -> dict[str, object]:
    if route_path not in {
        "/v1/drivers/generic-web/preview-destroy",
        "/v1/drivers/verireel/preview-destroy",
    }:
        return payload
    canonical_payload = json.loads(json.dumps(payload))
    destroy_payload = canonical_payload.get("destroy")
    if isinstance(destroy_payload, dict):
        destroy_payload.pop("destroy_reason", None)
    return cast(dict[str, object], canonical_payload)


def idempotency_request_fingerprint(*, route_path: str, payload: dict[str, object]) -> str:
    if route_path in {
        _PRODUCT_CONFIG_APPLY_ROUTE,
        _PRODUCT_ENVIRONMENT_CONFIG_APPLY_ROUTE,
    }:
        return product_config_request_fingerprint(payload)
    return build_request_fingerprint(
        canonical_request_payload_for_idempotency(route_path=route_path, payload=payload)
    )


def canonical_product_config_request_payload(payload: dict[str, object]) -> dict[str, object]:
    request = ProductConfigApplyEnvelope.model_validate(payload)
    normalized_payload = control_plane_product_config.normalize_product_config_payload(
        request.product_config_payload()
    )
    return {
        **normalized_payload,
        "mode": request.mode,
        "source_label": request.source_label,
        "reason": request.reason,
        "confirmation": request.confirmation,
    }


def product_config_continuity_payload(payload: dict[str, object]) -> dict[str, object]:
    continuity_payload = canonical_product_config_request_payload(payload)
    continuity_payload.pop("mode", None)
    continuity_payload.pop("reason", None)
    continuity_payload.pop("confirmation", None)
    continuity_payload.pop("source_label", None)
    return continuity_payload


def product_config_request_fingerprint(payload: dict[str, object]) -> str:
    if not payload.get("secrets"):
        return build_request_fingerprint(payload)
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return control_plane_secrets.keyed_secret_payload_fingerprint(
        canonical,
        purpose="product-config-request",
    )


def product_config_dry_run_key(payload: dict[str, object]) -> str:
    return "product-config-dry-run:" + product_config_request_fingerprint(
        product_config_continuity_payload(payload)
    )


def launchplane_identity_actor(identity: LaunchplaneIdentity) -> str:
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


def product_retirement_identity(identity: LaunchplaneIdentity) -> ProductRetirementIdentity:
    return ProductRetirementIdentity(
        actor=launchplane_identity_actor(identity),
        identity_kind=type(identity).__name__,
        subject=str(getattr(identity, "subject", "") or ""),
        repository=str(getattr(identity, "repository", "") or ""),
        workflow_ref=str(
            getattr(identity, "workflow_ref", "") or getattr(identity, "job_workflow_ref", "") or ""
        ),
        environment=str(getattr(identity, "environment", "") or ""),
    )


def detached_application_retirement_identity(
    identity: LaunchplaneIdentity,
) -> DetachedApplicationRetirementIdentity:
    return DetachedApplicationRetirementIdentity(
        actor=launchplane_identity_actor(identity),
        identity_kind=type(identity).__name__,
        subject=str(getattr(identity, "subject", "") or ""),
        repository=str(getattr(identity, "repository", "") or ""),
        workflow_ref=str(getattr(identity, "workflow_ref", "") or ""),
        job_workflow_ref=str(getattr(identity, "job_workflow_ref", "") or ""),
        environment=str(getattr(identity, "environment", "") or ""),
    )


def detached_application_retirement_identity_allowed(identity: LaunchplaneIdentity) -> bool:
    if isinstance(identity, LocalAdminIdentity):
        return True
    if not isinstance(identity, GitHubActionsIdentity):
        return False
    return (
        identity.repository == "cbusillo/launchplane"
        and re.fullmatch(
            r"cbusillo/launchplane/\.github/workflows/"
            r"reusable-detached-application-retirement\.yml@[0-9a-f]{40}",
            identity.job_workflow_ref.strip(),
        )
        is not None
    )


def product_config_dry_run_exists(
    *,
    record_store: object,
    identity: LaunchplaneIdentity,
    request_payload: dict[str, object],
    route_path: str = _PRODUCT_CONFIG_APPLY_ROUTE,
) -> bool:
    idempotency_store = idempotency_capable_store(record_store)
    if idempotency_store is None:
        return False
    continuity_fingerprint = product_config_request_fingerprint(
        product_config_continuity_payload(request_payload)
    )
    stored_record = idempotency_store.read_idempotency_record(
        scope=idempotency_scope(identity),
        route_path=route_path,
        idempotency_key=product_config_dry_run_key(request_payload),
    )
    return stored_record is not None and stored_record.request_fingerprint == continuity_fingerprint


def product_config_dry_run_record_matches(
    *, record: LaunchplaneIdempotencyRecord | None, request_fingerprint_value: str
) -> bool:
    return record is not None and record.request_fingerprint == request_fingerprint_value


def store_product_config_dry_run_record(
    *,
    record_store: object,
    identity: LaunchplaneIdentity,
    request_payload: dict[str, object],
    trace_id: str,
    response: BaseModel,
    route_path: str = _PRODUCT_CONFIG_APPLY_ROUTE,
) -> None:
    idempotency_store = idempotency_capable_store(record_store)
    if idempotency_store is None:
        return
    dry_run_idempotency_key = product_config_dry_run_key(request_payload)
    dry_run_request_fingerprint = product_config_request_fingerprint(
        product_config_continuity_payload(request_payload)
    )
    stored_record = idempotency_store.read_idempotency_record(
        scope=idempotency_scope(identity),
        route_path=route_path,
        idempotency_key=dry_run_idempotency_key,
    )
    if product_config_dry_run_record_matches(
        record=stored_record,
        request_fingerprint_value=dry_run_request_fingerprint,
    ):
        return
    dry_run_trace_id = f"{trace_id}-product-config-dry-run"
    try:
        idempotency_store.write_idempotency_record(
            LaunchplaneIdempotencyRecord(
                record_id=build_launchplane_idempotency_record_id(
                    response_trace_id=dry_run_trace_id
                ),
                scope=idempotency_scope(identity),
                route_path=route_path,
                idempotency_key=dry_run_idempotency_key,
                request_fingerprint=dry_run_request_fingerprint,
                response_status_code=202,
                response_trace_id=dry_run_trace_id,
                recorded_at=utc_now_timestamp(),
                response_payload=response.model_dump(mode="json", exclude_none=True),
            )
        )
    except Exception as write_error:
        try:
            stored_record = idempotency_store.read_idempotency_record(
                scope=idempotency_scope(identity),
                route_path=route_path,
                idempotency_key=dry_run_idempotency_key,
            )
        except Exception as read_error:
            raise write_error from read_error
        if product_config_dry_run_record_matches(
            record=stored_record,
            request_fingerprint_value=dry_run_request_fingerprint,
        ):
            return
        raise


def product_promotion_dry_run_record(
    *,
    record_store: object,
    identity: LaunchplaneIdentity,
    status: ProductPromotionStatus,
    bump: Literal["patch", "minor", "major"],
) -> LaunchplaneIdempotencyRecord | None:
    idempotency_store = idempotency_capable_store(record_store)
    if idempotency_store is None:
        return None
    continuity_payload = product_promotion_continuity_payload(status=status, bump=bump)
    continuity_fingerprint = build_request_fingerprint(continuity_payload)
    stored_record = idempotency_store.read_idempotency_record(
        scope=idempotency_scope(identity),
        route_path=_PRODUCT_PROMOTION_DRY_RUN_MARKER_ROUTE,
        idempotency_key=product_promotion_dry_run_key(status=status, bump=bump),
    )
    if stored_record is None or stored_record.request_fingerprint != continuity_fingerprint:
        return None
    return stored_record


def store_product_promotion_dry_run_record(
    *,
    record_store: object,
    identity: LaunchplaneIdentity,
    status: ProductPromotionStatus,
    bump: Literal["patch", "minor", "major"],
    trace_id: str,
    response: BaseModel,
) -> None:
    idempotency_store = idempotency_capable_store(record_store)
    if idempotency_store is None:
        return
    if (
        product_promotion_dry_run_record(
            record_store=record_store,
            identity=identity,
            status=status,
            bump=bump,
        )
        is not None
    ):
        return
    continuity_payload = product_promotion_continuity_payload(status=status, bump=bump)
    continuity_fingerprint = build_request_fingerprint(continuity_payload)
    dry_run_key = product_promotion_dry_run_key(status=status, bump=bump)
    marker_trace_id = f"{trace_id}-product-promotion-dry-run"
    try:
        idempotency_store.write_idempotency_record(
            LaunchplaneIdempotencyRecord(
                record_id=build_launchplane_idempotency_record_id(
                    response_trace_id=marker_trace_id
                ),
                scope=idempotency_scope(identity),
                route_path=_PRODUCT_PROMOTION_DRY_RUN_MARKER_ROUTE,
                idempotency_key=dry_run_key,
                request_fingerprint=continuity_fingerprint,
                response_status_code=202,
                response_trace_id=trace_id,
                recorded_at=utc_now_timestamp(),
                response_payload=response.model_dump(mode="json", exclude_none=True),
            )
        )
    except Exception as write_error:
        try:
            stored_record = product_promotion_dry_run_record(
                record_store=record_store,
                identity=identity,
                status=status,
                bump=bump,
            )
        except Exception as read_error:
            raise write_error from read_error
        if stored_record is not None:
            return
        raise


def parse_utc_timestamp(value: str) -> datetime:
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _public_ingress_notification_policy_summary(
    policy: PublicIngressNotificationPolicyRecord,
) -> dict[str, object]:
    return {
        "policy_id": policy.policy_id,
        "product": policy.product,
        "context": policy.context,
        "instance": policy.instance,
        "status": policy.status,
        "reminder_interval_seconds": policy.reminder_interval_seconds,
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


_DEFAULT_OPENAPI_REF_TEMPLATE = "#/$defs/{model}"
_LAUNCHPLANE_ERROR_RESPONSE_REF = "#/components/schemas/LaunchplaneErrorResponse"


@cache
def _cached_openapi_model_schema(model: type[BaseModel], ref_template: str) -> dict[str, Any]:
    return model.model_json_schema(ref_template=ref_template)


def _openapi_model_schema(
    model: type[BaseModel],
    *,
    ref_template: str = _DEFAULT_OPENAPI_REF_TEMPLATE,
) -> dict[str, Any]:
    return deepcopy(_cached_openapi_model_schema(model, ref_template))


class _LaunchplaneFastAPI(FastAPI):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._launchplane_error_response_model_registered = False
        super().__init__(*args, **kwargs)

    def add_api_route(
        self,
        path: str,
        endpoint: Callable[..., Any],
        **kwargs: Any,
    ) -> None:
        if native_routes.is_native_fastapi_driver_route_path(path):
            route_metadata = native_routes.bind_native_fastapi_driver_handler(
                route_path=path,
                endpoint=endpoint,
                declared_methods=kwargs.get("methods"),
            )
            kwargs["methods"] = [route_metadata.method]
        responses = kwargs.get("responses")
        if path.startswith("/v1/") and path != "/v1/health":
            responses = {
                409: {"model": LaunchplaneErrorResponse},
                503: {"model": LaunchplaneErrorResponse},
                **(responses or {}),
            }
            kwargs["responses"] = responses
        registers_error_response_model = False
        if responses is not None:
            normalized_responses, registers_error_response_model = (
                self._deduplicate_error_response_models(
                    responses,
                    include_in_schema=bool(kwargs.get("include_in_schema", True)),
                    response_media_type=self._response_media_type(kwargs),
                )
            )
            kwargs["responses"] = normalized_responses
        super().add_api_route(path, endpoint, **kwargs)
        if registers_error_response_model:
            self._launchplane_error_response_model_registered = True

    def openapi(self) -> dict[str, Any]:
        schema = super().openapi()
        for path_item in schema.get("paths", {}).values():
            if not isinstance(path_item, dict):
                continue
            for operation in path_item.values():
                if not isinstance(operation, dict):
                    continue
                responses = operation.get("responses")
                if isinstance(responses, dict):
                    responses.pop("422", None)
        return schema

    def _response_media_type(self, kwargs: dict[str, Any]) -> str:
        response_class = kwargs.get("response_class", self.router.default_response_class)
        while isinstance(response_class, DefaultPlaceholder):
            response_class = response_class.value
        media_type = cast(type[Response], response_class).media_type
        return media_type or "application/json"

    def _deduplicate_error_response_models(
        self,
        responses: dict[int | str, dict[str, Any]],
        *,
        include_in_schema: bool,
        response_media_type: str,
    ) -> tuple[dict[int | str, dict[str, Any]], bool]:
        normalized_responses = {
            status_code: dict(response) for status_code, response in responses.items()
        }
        if not include_in_schema:
            return normalized_responses, False

        error_response_model_registered = self._launchplane_error_response_model_registered
        registers_error_response_model = False
        for normalized_response in normalized_responses.values():
            if normalized_response.get("model") is not LaunchplaneErrorResponse:
                continue
            if not error_response_model_registered:
                error_response_model_registered = True
                registers_error_response_model = True
                continue
            if set(normalized_response) != {"model"}:
                continue
            normalized_response.pop("model")
            normalized_response["content"] = {
                response_media_type: {"schema": {"$ref": _LAUNCHPLANE_ERROR_RESPONSE_REF}}
            }
        return normalized_responses, registers_error_response_model


class LaunchplaneAuthzPolicyRuntime:
    def __init__(
        self,
        policy: LaunchplaneAuthzPolicy,
        *,
        policy_sha256: str = "",
        source: str = "bootstrap",
        record_id: str = "",
        revision: int = 0,
    ) -> None:
        self._policy = policy
        self._policy_sha256 = policy_sha256 or authz_policy_sha256(policy)
        self._source = source.strip() or "bootstrap"
        self._record_id = record_id.strip()
        self._revision = revision

    @property
    def policy(self) -> LaunchplaneAuthzPolicy:
        return self._policy

    @property
    def policy_sha256(self) -> str:
        return self._policy_sha256

    @property
    def source(self) -> str:
        return self._source

    @property
    def record_id(self) -> str:
        return self._record_id

    @property
    def revision(self) -> int:
        return self._revision

    def allows(
        self,
        *,
        identity: LaunchplaneIdentity,
        action: str,
        product: str,
        context: str,
        target: AuthorizationTarget | None = None,
    ) -> bool:
        return self._policy.allows(
            identity=identity,
            action=action,
            product=product,
            context=context,
            target=target,
        )

    def policy_record(self, *, updated_at: str) -> LaunchplaneAuthzPolicyRecord:
        record_id = self._record_id or f"runtime-authz-policy-{self._policy_sha256[:12]}"
        return LaunchplaneAuthzPolicyRecord(
            record_id=record_id,
            revision=self._revision or 1,
            status="active",
            source=self._source,
            updated_at=updated_at,
            policy_sha256=self._policy_sha256,
            policy=self._policy,
        )

    def provenance(self) -> AuthzPolicyProvenance:
        return AuthzPolicyProvenance(
            source=self._source,
            record_id=self._record_id,
            revision=self._revision,
            policy_sha256=self._policy_sha256,
        )

    def update(
        self,
        policy: LaunchplaneAuthzPolicy,
        *,
        policy_sha256: str = "",
        source: str = "",
        record_id: str | None = None,
        revision: int | None = None,
    ) -> None:
        self._policy = policy
        self._policy_sha256 = policy_sha256 or authz_policy_sha256(policy)
        if source.strip():
            self._source = source.strip()
        if record_id is not None:
            self._record_id = record_id.strip()
        if revision is not None:
            self._revision = revision


class ResolvedLaunchplaneAuthzPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    policy: LaunchplaneAuthzPolicy
    policy_sha256: str
    source: str
    record_id: str = ""
    revision: int = Field(default=0, ge=0)


def optional_callable_attribute(instance: object, name: str) -> Callable[..., Any] | None:
    attribute = getattr(instance, name, None)
    return attribute if callable(attribute) else None


def string_value(value: Any) -> str:
    return str(value)


def resolve_launchplane_authz_policy(
    *,
    record_store: object,
    bootstrap_policy: LaunchplaneAuthzPolicy,
    policy_source: str,
    now_timestamp: str,
) -> ResolvedLaunchplaneAuthzPolicy:
    list_records = optional_callable_attribute(record_store, "list_authz_policy_records")
    if list_records is not None:
        records = list_records(status="active", limit=1)
        if records:
            record = records[0]
            return ResolvedLaunchplaneAuthzPolicy(
                policy=record.policy,
                policy_sha256=record.policy_sha256,
                source="db",
                record_id=record.record_id,
                revision=record.revision,
            )

    policy_sha256 = authz_policy_sha256(bootstrap_policy)
    seed_record = optional_callable_attribute(record_store, "seed_authz_policy_if_absent")
    if seed_record is not None:
        record = LaunchplaneAuthzPolicyRecord(
            record_id=build_authz_policy_record_id(
                revision=1,
                policy_sha256=policy_sha256,
            ),
            revision=1,
            status="active",
            source=policy_source,
            updated_at=now_timestamp,
            policy_sha256=policy_sha256,
            policy=bootstrap_policy,
        )
        seeded_record = seed_record(record)
        return ResolvedLaunchplaneAuthzPolicy(
            policy=seeded_record.policy,
            policy_sha256=seeded_record.policy_sha256,
            source=(
                "bootstrap_seeded_store" if seeded_record.record_id == record.record_id else "db"
            ),
            record_id=seeded_record.record_id,
            revision=seeded_record.revision,
        )

    return ResolvedLaunchplaneAuthzPolicy(
        policy=bootstrap_policy,
        policy_sha256=policy_sha256,
        source="bootstrap",
    )


def authz_policy_record_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


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
    github_oauth_client: GitHubOAuthLoginClient | None = None,
    oauth_login_state_store: OAuthLoginStateRepository | None = None,
    control_plane_root_path: FilePath | None = None,
    state_dir: FilePath | None = None,
    work_graph_planning_facts_provider: WorkGraphPlanningFactsProvider | None = None,
    work_graph_issue_inbox_provider: WorkGraphIssueInboxProvider | None = None,
    work_graph_issue_inbox_reconcile_provider: WorkGraphIssueInboxReconcileProvider | None = None,
    change_impact_repository_evidence_provider: (
        ChangeImpactRepositoryEvidenceProvider | None
    ) = None,
    every_code_discord_sender: Callable[[str, dict[str, object]], object] = post_discord_webhook,
    preview_pr_feedback_discord_sender: Callable[
        [str, dict[str, object]], object
    ] = post_discord_webhook,
    every_code_github_webhook_handler: EveryCodeGitHubWebhookHandler | None = None,
    manager_preview_approval_github_webhook_handler: (
        ManagerPreviewApprovalGitHubWebhookHandler | None
    ) = None,
    engineering_review_target_resolver: EngineeringReviewTargetResolver | None = None,
) -> FastAPI:
    resolved_control_plane_root = (
        control_plane_root_path or FilePath(__file__).resolve().parent.parent
    )
    resolved_state_dir = state_dir or resolved_control_plane_root / "state"
    resolved_change_impact_repository_evidence_provider = (
        change_impact_repository_evidence_provider
        or GitHubChangeImpactRepositoryEvidenceProvider(
            control_plane_root=resolved_control_plane_root,
            github_token=resolve_launchplane_github_token,
            github_api=github_api_request,
            token_context=_LAUNCHPLANE_SERVICE_CONTEXT,
        )
    )
    injected_github_api_request = github_api_request
    owner_acceptance_projection_service = OwnerAcceptanceProjectionService(
        repository_evidence_provider=resolved_change_impact_repository_evidence_provider,
        github_app_token=lambda repository, repository_id: mint_repository_installation_token(
            identity=resolve_advisory_github_app_identity(
                control_plane_root=resolved_control_plane_root
            ),
            repository=repository,
            repository_id=repository_id,
            api_request=injected_github_api_request,
        ),
        public_origin=(human_session_manager.public_origin if human_session_manager else None),
        api_request=injected_github_api_request,
    )
    resolved_engineering_review_target_resolver = (
        engineering_review_target_resolver
        if engineering_review_target_resolver is not None
        else lambda work_request: resolve_engineering_review_pull_request_target(
            work_request,
            github_token=os.environ.get(EVERY_CODE_GITHUB_TOKEN_ENV_KEY, ""),
        )
    )
    shared_record_store: object | None = (
        None
        if record_store_factory is not None
        else build_shared_record_store(database_url=database_url)
    )
    if authz_policy_runtime is not None:
        resolved_authz_policy_runtime = authz_policy_runtime
    elif shared_record_store is not None:
        resolved_authz_policy = resolve_launchplane_authz_policy(
            record_store=shared_record_store,
            bootstrap_policy=authz_policy,
            policy_source="bootstrap-policy",
            now_timestamp=authz_policy_record_timestamp(),
        )
        resolved_authz_policy_runtime = LaunchplaneAuthzPolicyRuntime(
            resolved_authz_policy.policy,
            policy_sha256=resolved_authz_policy.policy_sha256,
            source=resolved_authz_policy.source,
            record_id=resolved_authz_policy.record_id,
            revision=resolved_authz_policy.revision,
        )
    else:
        resolved_authz_policy_runtime = LaunchplaneAuthzPolicyRuntime(authz_policy)
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

    app = _LaunchplaneFastAPI(title="Launchplane API", version="0.1.0", lifespan=lifespan)
    app.add_middleware(BoundedRequestBodyMiddleware)
    enforce_human_policy_revalidation = False

    def read_active_authz_policy_records() -> tuple[LaunchplaneAuthzPolicyRecord, ...] | None:
        nonlocal enforce_human_policy_revalidation
        refresh_record_store = (
            record_store_factory() if record_store_factory is not None else shared_record_store
        )
        if (
            not isinstance(refresh_record_store, PostgresRecordStore)
            or refresh_record_store.database_dialect_name != "postgresql"
        ):
            return None
        enforce_human_policy_revalidation = True
        active_records = refresh_record_store.list_authz_policy_records(
            status="active",
            limit=2,
        )
        return active_records

    @app.middleware("http")
    async def refresh_authz_policy_runtime(
        request: Request,
        call_next: Callable[[Request], Any],
    ) -> Response:
        request_authz_policy_provenance = resolved_authz_policy_runtime.provenance()
        if request.url.path.startswith("/v1/") and request.url.path != "/v1/health":
            trace_id = next_trace_id()
            try:
                refresh_result = await run_in_threadpool(read_active_authz_policy_records)
            except (OSError, RuntimeError, SQLAlchemyError, TypeError, ValueError):
                logging.exception("Failed to refresh the active Launchplane authz policy.")
                return JSONResponse(
                    status_code=503,
                    content=LaunchplaneErrorResponse(
                        trace_id=trace_id,
                        error=LaunchplaneErrorDetail(
                            code="authz_policy_unavailable",
                            message="Launchplane active authz policy is unavailable.",
                        ),
                    ).model_dump(mode="json"),
                )
            if refresh_result is None:
                request.state.launchplane_authz_policy_provenance = (
                    resolved_authz_policy_runtime.provenance()
                )
                return cast(Response, await call_next(request))
            active_records = refresh_result
            if not active_records:
                return JSONResponse(
                    status_code=503,
                    content=LaunchplaneErrorResponse(
                        trace_id=trace_id,
                        error=LaunchplaneErrorDetail(
                            code="authz_policy_unavailable",
                            message="Launchplane active authz policy is unavailable.",
                        ),
                    ).model_dump(mode="json"),
                )
            if len(active_records) > 1:
                return JSONResponse(
                    status_code=409,
                    content=LaunchplaneErrorResponse(
                        trace_id=trace_id,
                        error=LaunchplaneErrorDetail(
                            code="active_authz_policy_ambiguous",
                            message="Multiple active Launchplane authz policy records were found.",
                        ),
                    ).model_dump(mode="json"),
                )
            active_record = active_records[0]
            request_authz_policy_provenance = AuthzPolicyProvenance.from_record(active_record)
            if (
                resolved_authz_policy_runtime.policy_sha256 != active_record.policy_sha256
                or resolved_authz_policy_runtime.source != "db"
                or resolved_authz_policy_runtime.record_id != active_record.record_id
                or resolved_authz_policy_runtime.revision != active_record.revision
            ):
                resolved_authz_policy_runtime.update(
                    active_record.policy,
                    policy_sha256=active_record.policy_sha256,
                    source="db",
                    record_id=active_record.record_id,
                    revision=active_record.revision,
                )
        request.state.launchplane_authz_policy_provenance = request_authz_policy_provenance
        return cast(Response, await call_next(request))

    @app.middleware("http")
    async def isolate_authz_evaluation_context(
        request: Request,
        call_next: Callable[[Request], Any],
    ) -> Response:
        clear_authz_evaluation()
        try:
            response = cast(Response, await call_next(request))
            if request.url.path == _AUTHZ_ACTIVATION_PREFLIGHT_ROUTE:
                response.headers["Cache-Control"] = "no-store"
            return response
        finally:
            clear_authz_evaluation()

    resolved_oauth_login_state_store = (
        oauth_login_state_store if oauth_login_state_store is not None else OAuthLoginStateStore()
    )

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
        if enforce_human_policy_revalidation:
            if not human_session_manager.authorization_claims_are_current(session):
                human_session_manager.revoke(session)
                return None
            current_role = human_session_manager.authorized_role(
                identity=session.identity,
                authz_policy=resolved_authz_policy_runtime.policy,
            )
            if current_role == "admin":
                resolved_role: Literal["read_only", "admin"] = "admin"
            elif current_role == "read_only":
                resolved_role = "read_only"
            else:
                return None
            if resolved_role != session.identity.role:
                session = replace(
                    session,
                    identity=replace(session.identity, role=resolved_role),
                )
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
        request.state.launchplane_human_session = session
        return session.identity

    def read_nonrenewing_human_session(
        *,
        cookie_header: str,
    ) -> LaunchplaneHumanSession | None:
        if human_session_manager is None:
            return None
        session = human_session_manager.read_cookie_without_renewal(cookie_header)
        if session is None:
            return None
        if not enforce_human_policy_revalidation:
            return session
        if not human_session_manager.authorization_claims_are_current(session):
            return None
        current_role = human_session_manager.authorized_role(
            identity=session.identity,
            authz_policy=resolved_authz_policy_runtime.policy,
        )
        if current_role not in {"admin", "read_only"}:
            return None
        resolved_role: Literal["read_only", "admin"] = current_role
        if resolved_role == session.identity.role:
            return session
        return replace(
            session,
            identity=replace(session.identity, role=resolved_role),
        )

    def read_authz_activation_preflight_session(
        response: Response,
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
        cookie: Annotated[str, Header(alias="Cookie")] = "",
    ) -> LaunchplaneHumanSession:
        no_store_headers = {"Cache-Control": "no-store"}
        response.headers.update(no_store_headers)
        if authorization is not None:
            raise _launchplane_http_error(
                status_code=403,
                trace_id=next_trace_id(),
                code="authorization_denied",
                message="Activation preflight requires a signed Launchplane session cookie.",
                headers=no_store_headers,
            )
        if human_session_manager is None:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=next_trace_id(),
                code="activation_preflight_unavailable",
                message="Activation preflight evidence is unavailable.",
                headers=no_store_headers,
            )
        try:
            session = human_session_manager.read_cookie_without_renewal(cookie)
            claims_are_current = (
                session is not None
                and human_session_manager.authorization_claims_are_current(session)
            )
        except Exception as error:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=next_trace_id(),
                code="activation_preflight_unavailable",
                message="Activation preflight evidence is unavailable.",
                headers=no_store_headers,
            ) from error
        if session is None or session.identity.github_id <= 0:
            raise _launchplane_http_error(
                status_code=401,
                trace_id=next_trace_id(),
                code="authentication_required",
                message="A signed Launchplane session cookie is required.",
                headers=no_store_headers,
            )
        if not claims_are_current:
            raise _launchplane_http_error(
                status_code=401,
                trace_id=next_trace_id(),
                code="authentication_required",
                message="A signed Launchplane session cookie is required.",
                headers=no_store_headers,
            )
        return session

    def human_identity_response(identity: GitHubHumanIdentity) -> GitHubHumanIdentityResponse:
        return GitHubHumanIdentityResponse(
            login=identity.login,
            github_id=identity.github_id,
            name=identity.name,
            email=identity.email,
            organizations=tuple(sorted(identity.organizations)),
            teams=tuple(sorted(identity.teams)),
            role=identity.role,
        )

    def read_auth_session(
        request: Request,
        response: Response,
        cookie: Annotated[str, Header(alias="Cookie")] = "",
    ) -> AuthSessionResponse | JSONResponse:
        trace_id = next_trace_id()
        session_result = read_human_session(cookie_header=cookie)
        if session_result is None:
            payload = AuthSessionRequiredResponse(
                trace_id=trace_id,
                error=LaunchplaneErrorDetail(
                    code="authentication_required",
                    message="Sign in with GitHub to access Launchplane.",
                ),
                configured=human_session_manager is not None,
            )
            return JSONResponse(
                status_code=401,
                content=payload.model_dump(mode="json"),
                headers={"Cache-Control": "no-store"},
            )
        session, was_renewed = session_result
        if was_renewed and human_session_manager is not None:
            session_cookie_header = human_session_manager.session_cookie_header(session)
            response.headers.append("Set-Cookie", session_cookie_header)
            request.state.launchplane_renewed_session_cookie = session_cookie_header
        if human_session_manager is None:
            raise RuntimeError("Launchplane human session manager is not initialized.")
        response.headers["Cache-Control"] = "no-store"
        return AuthSessionResponse(
            trace_id=trace_id,
            identity=human_identity_response(session.identity),
            csrf_token=human_session_manager.csrf_token(session),
        )

    def reject_github_oauth_not_configured(trace_id: str) -> JSONResponse:
        return JSONResponse(
            status_code=503,
            content=LaunchplaneErrorResponse(
                trace_id=trace_id,
                error=LaunchplaneErrorDetail(
                    code="auth_not_configured",
                    message="GitHub OAuth is not configured for Launchplane.",
                ),
            ).model_dump(mode="json"),
        )

    def reject_invalid_github_oauth_callback(trace_id: str, message: str) -> JSONResponse:
        return JSONResponse(
            status_code=400,
            content=LaunchplaneErrorResponse(
                trace_id=trace_id,
                error=LaunchplaneErrorDetail(
                    code="invalid_oauth_callback",
                    message=message,
                ),
            ).model_dump(mode="json"),
        )

    def login_github_oauth(return_to: str = "/") -> Response:
        trace_id = next_trace_id()
        if human_session_manager is None or github_oauth_client is None:
            return reject_github_oauth_not_configured(trace_id)
        state = secrets.token_urlsafe(32)
        code_verifier, code_challenge = build_pkce_verifier()
        resolved_oauth_login_state_store.put(
            state=state,
            code_verifier=code_verifier,
            return_to=safe_oauth_return_to(return_to),
        )
        return RedirectResponse(
            url=github_oauth_client.authorization_url(
                state=state,
                code_challenge=code_challenge,
            ),
            status_code=302,
            headers={"Cache-Control": "no-store"},
        )

    def complete_github_oauth_callback(
        code: str = "",
        state: str = "",
    ) -> Response:
        trace_id = next_trace_id()
        if human_session_manager is None or github_oauth_client is None:
            return reject_github_oauth_not_configured(trace_id)
        callback_code = code.strip()
        callback_state = state.strip()
        login_state = resolved_oauth_login_state_store.pop(callback_state)
        if not callback_code or login_state is None:
            return reject_invalid_github_oauth_callback(
                trace_id,
                "GitHub OAuth callback is missing a valid code or state.",
            )
        try:
            human_identity = github_oauth_client.fetch_identity(
                code=callback_code,
                code_verifier=login_state.code_verifier,
                authz_policy=resolved_authz_policy_runtime.policy,
            )
        except PermissionError:
            return JSONResponse(
                status_code=403,
                content=LaunchplaneErrorResponse(
                    trace_id=trace_id,
                    error=LaunchplaneErrorDetail(
                        code="authorization_denied",
                        message="GitHub identity is not authorized for Launchplane.",
                    ),
                ).model_dump(mode="json"),
            )
        except (OSError, RequestException, RuntimeError, ValueError):
            _LOGGER.exception("GitHub OAuth callback failed", extra={"trace_id": trace_id})
            return reject_invalid_github_oauth_callback(
                trace_id,
                "GitHub OAuth callback could not be completed.",
            )
        session = human_session_manager.issue(human_identity)
        return RedirectResponse(
            url=login_state.return_to,
            status_code=302,
            headers={"Set-Cookie": human_session_manager.session_cookie_header(session)},
        )

    def reject_browser_mutation() -> NoReturn:
        raise _launchplane_http_error(
            status_code=403,
            trace_id=next_trace_id(),
            code="browser_mutation_denied",
            message="Browser mutation request failed origin, fetch metadata, or CSRF validation.",
        )

    def consume_browser_mutation_request(
        *,
        request: Request,
        session: LaunchplaneHumanSession,
    ) -> LaunchplaneHumanSession:
        session_manager = human_session_manager
        if session_manager is None:
            reject_browser_mutation()
        try:
            csrf_token = validate_browser_mutation_request_headers(
                expected_origin=session_manager.public_origin,
                origin_values=tuple(request.headers.getlist("Origin")),
                sec_fetch_site_values=tuple(request.headers.getlist("Sec-Fetch-Site")),
                sec_fetch_mode_values=tuple(request.headers.getlist("Sec-Fetch-Mode")),
                sec_fetch_dest_values=tuple(request.headers.getlist("Sec-Fetch-Dest")),
                csrf_token_values=tuple(request.headers.getlist(BROWSER_CSRF_HEADER_NAME)),
            )
        except (PermissionError, ValueError):
            reject_browser_mutation()
        rotated_session = session_manager.consume_csrf_token(session, csrf_token)
        if rotated_session is None:
            reject_browser_mutation()
        request.state.launchplane_human_session = rotated_session
        return rotated_session

    def logout_auth_session(
        request: Request,
        response: Response,
        cookie: Annotated[str, Header(alias="Cookie")] = "",
    ) -> AuthLogoutResponse:
        trace_id = next_trace_id()
        if human_session_manager is not None:
            session_result = read_human_session(cookie_header=cookie)
            if session_result is not None:
                session, _was_renewed = session_result
                consume_browser_mutation_request(request=request, session=session)
            human_session_manager.delete_cookie_session(cookie)
            clear_cookie = human_session_manager.clear_cookie_header()
        else:
            clear_cookie = "launchplane_session=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax"
        response.headers.append("Set-Cookie", clear_cookie)
        return AuthLogoutResponse(trace_id=trace_id)

    def read_identity(
        request: Request,
        response: Response,
        authorization: Annotated[str, Header(alias="Authorization")] = "",
        cookie: Annotated[str, Header(alias="Cookie")] = "",
    ) -> LaunchplaneIdentity:
        bearer_identity = resolve_bearer_identity(authorization)
        if bearer_identity is not None:
            return bearer_identity
        human_identity = read_human_session_identity(
            cookie_header=cookie,
            request=request,
            response=response,
        )
        if human_identity is not None:
            return human_identity
        raise _authentication_required_error("Authorization header is required.")

    def resolve_bearer_identity(authorization: str) -> LaunchplaneIdentity | None:
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
        return None

    def read_bearer_identity(
        authorization: Annotated[str, Header(alias="Authorization")] = "",
    ) -> LaunchplaneIdentity:
        identity = resolve_bearer_identity(authorization)
        if identity is not None:
            return identity
        raise _authentication_required_error("Authorization header is required.")

    def read_browser_mutation_identity(
        request: Request,
        response: Response,
        authorization: Annotated[str, Header(alias="Authorization")] = "",
        cookie: Annotated[str, Header(alias="Cookie")] = "",
    ) -> LaunchplaneIdentity:
        identity = read_identity(
            request=request,
            response=response,
            authorization=authorization,
            cookie=cookie,
        )
        if not isinstance(identity, GitHubHumanIdentity):
            return identity
        session: object = getattr(request.state, "launchplane_human_session", None)
        if not isinstance(session, LaunchplaneHumanSession):
            reject_browser_mutation()
        consume_browser_mutation_request(request=request, session=session)
        return identity

    def require_github_human_identity(identity: LaunchplaneIdentity) -> GitHubHumanIdentity:
        if not isinstance(identity, GitHubHumanIdentity):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=next_trace_id(),
                code="authorization_denied",
                message="This route requires an authenticated GitHub human session.",
            )
        return identity

    def read_github_human_identity(
        identity: Annotated[LaunchplaneIdentity, Depends(read_identity)],
    ) -> GitHubHumanIdentity:
        return require_github_human_identity(identity)

    def read_github_human_browser_mutation_identity(
        identity: Annotated[LaunchplaneIdentity, Depends(read_browser_mutation_identity)],
    ) -> GitHubHumanIdentity:
        return require_github_human_identity(identity)

    def read_nonpersisting_sensitive_identity(
        request: Request,
        authorization: Annotated[str, Header(alias="Authorization")] = "",
        cookie: Annotated[str, Header(alias="Cookie")] = "",
    ) -> LaunchplaneIdentity:
        bearer_identity = resolve_bearer_identity(authorization)
        if bearer_identity is not None:
            return bearer_identity
        session = read_nonrenewing_human_session(cookie_header=cookie)
        if session is None or human_session_manager is None:
            raise _authentication_required_error("Authorization header is required.")
        try:
            csrf_token = validate_browser_mutation_request_headers(
                expected_origin=human_session_manager.public_origin,
                origin_values=tuple(request.headers.getlist("Origin")),
                sec_fetch_site_values=tuple(request.headers.getlist("Sec-Fetch-Site")),
                sec_fetch_mode_values=tuple(request.headers.getlist("Sec-Fetch-Mode")),
                sec_fetch_dest_values=tuple(request.headers.getlist("Sec-Fetch-Dest")),
                csrf_token_values=tuple(request.headers.getlist(BROWSER_CSRF_HEADER_NAME)),
            )
        except (PermissionError, ValueError):
            reject_browser_mutation()
        if not human_session_manager.csrf_token_is_valid(session, csrf_token):
            reject_browser_mutation()
        return session.identity

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
                message="Work graph rank requires GitHub Actions OIDC or a GitHub human session.",
            )
        try:
            return verifier.verify(bearer_token)
        except (InvalidTokenError, ValueError) as error:
            raise _authentication_required_error(str(error)) from error

    def read_browser_work_graph_rank_identity(
        request: Request,
        response: Response,
        authorization: Annotated[str, Header(alias="Authorization")] = "",
        cookie: Annotated[str, Header(alias="Cookie")] = "",
    ) -> GitHubActionsIdentity | GitHubHumanIdentity:
        identity = read_work_graph_rank_identity(
            request=request,
            response=response,
            authorization=authorization,
            cookie=cookie,
        )
        if not isinstance(identity, GitHubHumanIdentity):
            return identity
        session: object = getattr(request.state, "launchplane_human_session", None)
        if not isinstance(session, LaunchplaneHumanSession):
            reject_browser_mutation()
        consume_browser_mutation_request(request=request, session=session)
        return identity

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

    def read_engineering_review_worker_identity(
        authorization: Annotated[str, Header(alias="Authorization")] = "",
    ) -> EngineeringReviewWorkerIdentity:
        require_every_code_worker_write_token(authorization)
        assert bearer_identity_config is not None
        try:
            return EngineeringReviewWorkerIdentity(
                worker_runtime_id=bearer_identity_config.engineering_review_worker_runtime_id,
                worker_host=bearer_identity_config.engineering_review_worker_host,
            )
        except ValueError as error:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=next_trace_id(),
                code="engineering_review_worker_identity_unavailable",
                message="Engineering review worker identity is not configured.",
            ) from error

    async def handle_every_code_github_webhook(
        request: Request,
        x_github_event: Annotated[str, Header(alias="X-GitHub-Event")] = "",
        x_github_delivery: Annotated[str, Header(alias="X-GitHub-Delivery")] = "",
        x_hub_signature_256: Annotated[str, Header(alias="X-Hub-Signature-256")] = "",
        record_store: object = Depends(get_record_store),
    ) -> JSONResponse:
        trace_id = next_trace_id()
        if every_code_github_webhook_handler is None:
            raise _launchplane_http_error(
                status_code=404,
                trace_id=trace_id,
                code="not_found",
                message=f"No Launchplane route for {_EVERY_CODE_GITHUB_WEBHOOK_ROUTE}.",
            )
        status_code, payload = every_code_github_webhook_handler(
            await request.body(),
            x_github_event,
            x_github_delivery,
            x_hub_signature_256,
            record_store,
            resolved_control_plane_root,
            trace_id,
        )
        return JSONResponse(status_code=status_code, content=payload)

    async def handle_manager_preview_approval_github_webhook(
        request: Request,
        x_github_event: Annotated[str, Header(alias="X-GitHub-Event")] = "",
        x_github_delivery: Annotated[str, Header(alias="X-GitHub-Delivery")] = "",
        x_hub_signature_256: Annotated[str, Header(alias="X-Hub-Signature-256")] = "",
        record_store: object = Depends(get_record_store),
    ) -> JSONResponse:
        trace_id = next_trace_id()
        if manager_preview_approval_github_webhook_handler is None:
            raise _launchplane_http_error(
                status_code=404,
                trace_id=trace_id,
                code="not_found",
                message=f"No Launchplane route for {MANAGER_PREVIEW_APPROVAL_WEBHOOK_ROUTE}.",
            )
        status_code, payload = manager_preview_approval_github_webhook_handler(
            await request.body(),
            x_github_event,
            x_github_delivery,
            x_hub_signature_256,
            record_store,
            resolved_control_plane_root,
            trace_id,
        )
        return JSONResponse(status_code=status_code, content=payload)

    async def reconcile_manager_preview_approval(
        reconcile_request: ManagerPreviewApprovalReconcileEnvelope,
        identity: Annotated[LaunchplaneIdentity, Depends(read_browser_mutation_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
    ) -> dict[str, object]:
        trace_id = next_trace_id()
        list_profiles = optional_callable_attribute(record_store, "list_product_profile_records")
        if list_profiles is None:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="record_storage_unavailable",
                message="Manager preview approval reconciliation requires product profile storage.",
            )
        profiles = tuple(
            profile
            for profile in list_profiles()
            if profile.repository.strip().casefold() == reconcile_request.repository.casefold()
        )
        if len(profiles) != 1:
            raise _launchplane_http_error(
                status_code=404,
                trace_id=trace_id,
                code="not_found",
                message="Manager preview approval product profile was not found.",
            )
        profile = profiles[0]
        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action=MANAGER_PREVIEW_APPROVAL_READ_ACTION,
            product=profile.product,
            context=profile.preview.context,
            target=AuthorizationTarget(scope="context"),
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Caller cannot reconcile manager preview approval for this product.",
            )
        try:
            result = reconcile_manager_preview_approval_for_pr(
                repository=reconcile_request.repository,
                pr_number=reconcile_request.pr_number,
                record_store=cast(Any, record_store),
                control_plane_root=resolved_control_plane_root,
            )
        except (
            click.ClickException,
            FileNotFoundError,
            LookupError,
            TypeError,
            ValueError,
        ) as error:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="manager_preview_approval_unavailable",
                message="Manager preview approval reconciliation could not complete.",
            ) from error
        return {"status": "ok", "trace_id": trace_id, "result": result}

    def read_every_code_work_request_worker_write_identity(
        authorization: Annotated[str, Header(alias="Authorization")] = "",
    ) -> LaunchplaneIdentity | None:
        if every_code_worker_token_authorized(authorization):
            return None
        return read_write_identity(authorization=authorization)

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
                message="Terminal agent credentials can only read redacted Launchplane context.",
            )
        try:
            oidc_identity = verifier.verify(bearer_token)
        except (InvalidTokenError, ValueError) as error:
            raise _authentication_required_error(str(error)) from error
        if not isinstance(oidc_identity, GitHubActionsIdentity):
            raise _authentication_required_error("Mutation routes require GitHub Actions OIDC.")
        return oidc_identity

    read_route_dependencies = ReadRouteDependencies(
        read_identity=read_identity,
        get_record_store=get_record_store,
        next_trace_id=next_trace_id,
        authorization_allows=resolved_authz_policy_runtime.allows,
        http_error=_launchplane_http_error,
        error_response_model=LaunchplaneErrorResponse,
    )
    privileged_operation_route_dependencies = PrivilegedOperationRouteDependencies(
        common=read_route_dependencies,
        read_bearer_identity=read_bearer_identity,
        read_github_human_identity=read_github_human_identity,
        read_github_human_mutation_identity=(read_github_human_browser_mutation_identity),
        policy_reader=lambda: resolved_authz_policy_runtime.policy,
        policy_record_reader=lambda: read_active_authz_policy_record(get_record_store()),
    )
    product_owner_write_route_dependencies = ProductOwnerWriteRouteDependencies(
        read_write_identity=read_write_identity,
        get_record_store=get_record_store,
        next_trace_id=next_trace_id,
        authorization_allows=resolved_authz_policy_runtime.allows,
        http_error=_launchplane_http_error,
        error_response_model=LaunchplaneErrorResponse,
    )
    change_impact_write_route_dependencies = ChangeImpactWriteRouteDependencies(
        read_write_identity=read_write_identity,
        get_record_store=get_record_store,
        next_trace_id=next_trace_id,
        authorization_allows=resolved_authz_policy_runtime.allows,
        http_error=_launchplane_http_error,
        error_response_model=LaunchplaneErrorResponse,
    )
    engineering_review_write_route_dependencies = EngineeringReviewWriteRouteDependencies(
        read_write_identity=read_write_identity,
        read_worker_identity=read_engineering_review_worker_identity,
        get_record_store=get_record_store,
        next_trace_id=next_trace_id,
        authorization_allows=resolved_authz_policy_runtime.allows,
        http_error=_launchplane_http_error,
        error_response_model=LaunchplaneErrorResponse,
        target_resolver=resolved_engineering_review_target_resolver,
        repository_evidence_provider=resolved_change_impact_repository_evidence_provider,
    )
    engineering_review_decision_route_dependencies = EngineeringReviewDecisionRouteDependencies(
        read_write_identity=read_write_identity,
        get_record_store=get_record_store,
        next_trace_id=next_trace_id,
        authorization_allows=resolved_authz_policy_runtime.allows,
        http_error=_launchplane_http_error,
        error_response_model=LaunchplaneErrorResponse,
        repository_evidence_provider=resolved_change_impact_repository_evidence_provider,
        github_app_token=lambda repository, repository_id: mint_repository_installation_token(
            identity=resolve_advisory_github_app_identity(
                control_plane_root=resolved_control_plane_root
            ),
            repository=repository,
            repository_id=repository_id,
            api_request=injected_github_api_request,
        ),
        github_api=github_api_request,
    )
    evidence_write_route_dependencies = EvidenceWriteRouteDependencies(
        read_write_identity=read_write_identity,
        get_record_store=get_record_store,
        next_trace_id=next_trace_id,
        authorization_allows=resolved_authz_policy_runtime.allows,
        http_error=_launchplane_http_error,
        error_response_model=LaunchplaneErrorResponse,
        control_plane_root=resolved_control_plane_root,
    )
    product_read_route_dependencies = ProductReadRouteDependencies(
        common=read_route_dependencies,
        read_product_profile_list_identity=read_product_profile_list_identity,
        work_graph_planning_facts_provider=work_graph_planning_facts_provider,
        workflow_credentials_ready=lambda context: bool(
            resolve_launchplane_github_token(
                control_plane_root=resolved_control_plane_root,
                context_name=context,
            )
        ),
        control_plane_root=resolved_control_plane_root,
        github_token=resolve_launchplane_github_token,
    )
    driver_read_route_dependencies = DriverReadRouteDependencies(
        common=read_route_dependencies,
        control_plane_root=resolved_control_plane_root,
        database_url=database_url,
    )

    def work_graph_product_action_allowed(*, identity: LaunchplaneIdentity) -> ActionAllowed:
        def action_allowed(
            requested_action: str,
            requested_product: str,
            requested_context: str,
            requested_instances: tuple[str, ...],
        ) -> bool:
            return resolved_authz_policy_runtime.policy.allows(
                identity=identity,
                action=requested_action,
                product=requested_product,
                context=requested_context,
                target=(
                    AuthorizationTarget(scope="instance", instances=requested_instances)
                    if requested_instances
                    else AuthorizationTarget(scope="context")
                ),
            )

        return action_allowed

    work_graph_read_route_dependencies = WorkGraphReadRouteDependencies(
        common=read_route_dependencies,
        action_allowed_for_identity=work_graph_product_action_allowed,
        planning_facts_provider=work_graph_planning_facts_provider,
        issue_inbox_provider=work_graph_issue_inbox_provider,
    )

    def require_launchplane_service_read_authorization(
        *,
        identity: LaunchplaneIdentity,
        trace_id: str,
    ) -> None:
        if resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action="launchplane_service.read",
            product="launchplane",
            context=_LAUNCHPLANE_SERVICE_CONTEXT,
            target=AuthorizationTarget(scope="context"),
        ):
            return
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

    def require_launchplane_service_verireel_reconcile_authorization(
        *, identity: LaunchplaneIdentity, trace_id: str
    ) -> None:
        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action="launchplane_service.reconcile_verireel_workers",
            product="launchplane",
            context=_LAUNCHPLANE_SERVICE_CONTEXT,
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Workflow cannot reconcile Launchplane VeriReel workers.",
            )

    def read_launchplane_runtime(
        identity: Annotated[LaunchplaneIdentity, Depends(read_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
    ) -> LaunchplaneRuntimeResponse:
        trace_id = next_trace_id()
        require_launchplane_service_read_authorization(
            identity=identity,
            trace_id=trace_id,
        )
        schema_revision_reader = optional_callable_attribute(record_store, "schema_revision")
        database_schema_revision = (
            str(schema_revision_reader()).strip() if schema_revision_reader is not None else ""
        )
        runtime = LaunchplaneRuntimeStatus.model_validate(
            control_plane_service_status.launchplane_runtime_payload(
                storage_backend=storage_backend_name(record_store),
                database_schema_revision=database_schema_revision,
                authz_policy_schema_version=resolved_authz_policy_runtime.policy.schema_version,
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
        reconciled_restore_ids = tuple(reconcile_result.reconciled_restore_ids)
        reconciled_replacement_ids = tuple(reconcile_result.reconciled_replacement_ids)
        return OdooStableOperationWorkerReconcileResponse(
            trace_id=trace_id,
            reconcile_result=OdooStableOperationWorkerReconcileResultResponse(
                reconciled_bootstrap_ids=reconciled_bootstrap_ids,
                reconciled_restore_ids=reconciled_restore_ids,
                reconciled_replacement_ids=reconciled_replacement_ids,
                reconciled_count=(
                    len(reconciled_bootstrap_ids)
                    + len(reconciled_restore_ids)
                    + len(reconciled_replacement_ids)
                ),
            ),
        )

    def read_verireel_prod_backup_gate_operation_worker_status(
        identity: Annotated[LaunchplaneIdentity, Depends(read_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
        recent_terminal_limit: Annotated[str, Query()] = "10",
    ) -> VeriReelProdBackupGateOperationWorkerStatusResponse:
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
            worker_status = VeriReelProdBackupGateOperationWorkerStatusResponseModel.model_validate(
                control_plane_service_status.verireel_prod_backup_gate_operation_worker_status_payload(
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
        return VeriReelProdBackupGateOperationWorkerStatusResponse(
            trace_id=trace_id,
            worker_status=worker_status,
        )

    def reconcile_verireel_prod_backup_gate_operation_workers(
        identity: Annotated[LaunchplaneIdentity, Depends(read_write_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
        max_attempts: Annotated[str, Query()] = str(
            DEFAULT_VERIREEL_BACKUP_GATE_WORKER_MAX_ATTEMPTS
        ),
    ) -> VeriReelProdBackupGateOperationWorkerReconcileResponse:
        trace_id = next_trace_id()
        require_launchplane_service_verireel_reconcile_authorization(
            identity=identity,
            trace_id=trace_id,
        )
        try:
            parsed_max_attempts = control_plane_service_status.query_int_value(
                max_attempts,
                "max_attempts",
                default=DEFAULT_VERIREEL_BACKUP_GATE_WORKER_MAX_ATTEMPTS,
                minimum=1,
                maximum=100,
            )
            assert parsed_max_attempts is not None
            reconcile_result = reconcile_stale_verireel_prod_backup_gate_operation_records(
                record_store=control_plane_service_status.require_verireel_prod_backup_gate_operation_worker_store(
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
        reconciled_operation_ids = tuple(reconcile_result.reconciled_operation_ids)
        return VeriReelProdBackupGateOperationWorkerReconcileResponse(
            trace_id=trace_id,
            reconcile_result=VeriReelProdBackupGateOperationWorkerReconcileResultResponse(
                reconciled_operation_ids=reconciled_operation_ids,
                reconciled_count=len(reconciled_operation_ids),
            ),
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

    def require_every_code_work_request_write_store(
        record_store: object,
    ) -> _EveryCodeWorkRequestWriteStore:
        require_every_code_read_methods(
            record_store,
            required_methods=("write_every_code_work_request_record",),
            capability="Every Code work request writes",
        )
        return cast(_EveryCodeWorkRequestWriteStore, record_store)

    def require_every_code_work_request_claim_store(
        record_store: object,
    ) -> _EveryCodeWorkRequestClaimStore:
        require_every_code_read_methods(
            record_store,
            required_methods=("claim_every_code_work_request_record",),
            capability="Every Code work request claim writes",
        )
        return cast(_EveryCodeWorkRequestClaimStore, record_store)

    def require_every_code_work_request_heartbeat_store(
        record_store: object,
    ) -> _EveryCodeWorkRequestHeartbeatStore:
        require_every_code_read_methods(
            record_store,
            required_methods=("heartbeat_every_code_work_request_record",),
            capability="Every Code work request heartbeat writes",
        )
        return cast(_EveryCodeWorkRequestHeartbeatStore, record_store)

    def require_every_code_work_request_stale_store(
        record_store: object,
    ) -> _EveryCodeWorkRequestStaleStore:
        require_every_code_read_methods(
            record_store,
            required_methods=(
                "list_stale_every_code_work_request_records",
                "recover_stale_every_code_work_request_record",
            ),
            capability="Every Code stale work request recovery",
        )
        return cast(_EveryCodeWorkRequestStaleStore, record_store)

    def require_every_code_work_request_status_store(
        record_store: object,
    ) -> _EveryCodeWorkRequestStatusStore:
        require_every_code_read_methods(
            record_store,
            required_methods=("update_every_code_work_request_status_record",),
            capability="Every Code work request status writes",
        )
        return cast(_EveryCodeWorkRequestStatusStore, record_store)

    def require_every_code_work_request_rerun_store(
        record_store: object,
    ) -> _EveryCodeWorkRequestRerunStore:
        require_every_code_read_methods(
            record_store,
            required_methods=(
                "read_every_code_work_request_record",
                "compare_and_write_every_code_work_request_record",
            ),
            capability="Every Code work request rerun writes",
        )
        return cast(_EveryCodeWorkRequestRerunStore, record_store)

    def require_agent_write_intent_read_store(
        record_store: object,
    ) -> _AgentWriteIntentRecordReadStore:
        require_every_code_read_methods(
            record_store,
            required_methods=(
                "read_agent_write_intent_record",
                "list_agent_write_intent_records",
            ),
            capability="agent write-intent evidence reads",
        )
        return cast(_AgentWriteIntentRecordReadStore, record_store)

    def require_agent_write_intent_write_store(
        record_store: object,
    ) -> _AgentWriteIntentRecordWriteStore:
        require_every_code_read_methods(
            record_store,
            required_methods=("write_agent_write_intent_record",),
            capability="agent write-intent records",
        )
        return cast(_AgentWriteIntentRecordWriteStore, record_store)

    def agent_write_intent_secret_evidence(
        *, record_store: object, request: AgentWriteIntentRequest
    ) -> AgentWriteIntentSecretEvidence:
        if not request.secret_bindings:
            return secret_evidence_for_agent_write_intent(request=request, evaluation=None)
        if request.destination is None:
            return secret_evidence_for_agent_write_intent(
                request=request, evaluation=None, unavailable=True
            )
        try:
            policy_record = latest_active_runtime_key_safety_policy(record_store)  # type: ignore[arg-type]
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

    def reject_agent_write_intent(
        *, trace_id: str, code: str, message: str, record_id: str = ""
    ) -> HTTPException:
        detail: dict[str, object] = {"trace_id": trace_id, "code": code, "message": message}
        if record_id:
            detail["records"] = {"agent_write_intent_record_id": record_id}
        return LaunchplaneHTTPException(
            status_code=404 if code == "agent_write_intent_not_found" else 409,
            detail=detail,
        )

    def validate_every_code_rerun_write_intent(
        *,
        record_store: object,
        rerun_request: EveryCodeWorkRequestRerunEnvelope,
        idempotency_key: str,
        now: datetime,
        trace_id: str,
    ) -> AgentWriteIntentRecord | None:
        record_id = rerun_request.agent_write_intent_record_id.strip()
        if not record_id:
            return None
        try:
            record = require_agent_write_intent_read_store(
                record_store
            ).read_agent_write_intent_record(record_id)
        except FileNotFoundError as error:
            raise reject_agent_write_intent(
                trace_id=trace_id,
                code="agent_write_intent_not_found",
                message="Agent write-intent evidence record was not found.",
                record_id=record_id,
            ) from error
        if (
            record.evaluation.status != "allowed"
            or record.evaluation.intent != "every_code_rerun"
            or record.evaluation.mode != "apply"
            or not record.evaluation.safe_to_execute
        ):
            raise reject_agent_write_intent(
                trace_id=trace_id,
                code="agent_write_intent_not_executable",
                message=(
                    "Every Code rerun requires an allowed apply-mode "
                    "every_code_rerun intent record."
                ),
                record_id=record.record_id,
            )
        if (
            record.evaluation.product != "launchplane"
            or record.evaluation.context != _LAUNCHPLANE_SERVICE_CONTEXT
        ):
            raise reject_agent_write_intent(
                trace_id=trace_id,
                code="agent_write_intent_scope_mismatch",
                message=(
                    "Agent write-intent evidence does not match the Every Code "
                    "rerun product/context."
                ),
                record_id=record.record_id,
            )
        if record.evaluation.authz_action != "every_code_work_request.rerun":
            raise reject_agent_write_intent(
                trace_id=trace_id,
                code="agent_write_intent_action_mismatch",
                message="Agent write-intent evidence was evaluated for a different route action.",
                record_id=record.record_id,
            )
        if rerun_request.source_url and rerun_request.source_url != record.request.source_url:
            raise reject_agent_write_intent(
                trace_id=trace_id,
                code="agent_write_intent_source_mismatch",
                message="Every Code rerun source_url does not match the write-intent source_url.",
                record_id=record.record_id,
            )
        if idempotency_key and record.idempotency_key and idempotency_key != record.idempotency_key:
            raise reject_agent_write_intent(
                trace_id=trace_id,
                code="agent_write_intent_idempotency_mismatch",
                message=(
                    "Every Code rerun idempotency key does not match the write-intent evidence."
                ),
                record_id=record.record_id,
            )
        try:
            recorded_at = parse_utc_timestamp(record.recorded_at)
        except ValueError as error:
            raise reject_agent_write_intent(
                trace_id=trace_id,
                code="agent_write_intent_stale",
                message="Agent write-intent evidence timestamp is invalid or stale.",
                record_id=record.record_id,
            ) from error
        if recorded_at > now or now - recorded_at > _AGENT_WRITE_INTENT_MAX_AGE:
            raise reject_agent_write_intent(
                trace_id=trace_id,
                code="agent_write_intent_stale",
                message="Agent write-intent evidence is too old for Every Code rerun execution.",
                record_id=record.record_id,
            )
        return record

    def matching_every_code_rerun_intent_record(
        *, record_store: object, source_url: str, now: datetime
    ) -> AgentWriteIntentRecord | None:
        for record in require_agent_write_intent_read_store(
            record_store
        ).list_agent_write_intent_records(
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
                recorded_at = parse_utc_timestamp(record.recorded_at)
            except ValueError:
                continue
            if recorded_at <= now and now - recorded_at <= _AGENT_WRITE_INTENT_MAX_AGE:
                return record
        return None

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

    def require_every_code_preview_gate_write_store(
        record_store: object,
    ) -> _EveryCodePreviewGateWriteStore:
        require_every_code_read_methods(
            record_store,
            required_methods=("write_every_code_preview_gate_record",),
            capability="Every Code preview gate writes",
        )
        return cast(_EveryCodePreviewGateWriteStore, record_store)

    def rank_work_graph_snapshot(
        payload: WorkGraphRankEnvelope,
        identity: Annotated[
            GitHubActionsIdentity | GitHubHumanIdentity,
            Depends(read_browser_work_graph_rank_identity),
        ],
    ) -> WorkGraphRankResponse:
        trace_id = next_trace_id()
        require_work_graph_rank_authorization(
            identity=identity,
            trace_id=trace_id,
            message="Workflow cannot rank the Launchplane work graph.",
        )
        _summary, driver_result = build_work_graph_rank_result(payload)
        return WorkGraphRankResponse(
            trace_id=trace_id,
            records={},
            result=WorkGraphRankResult.model_validate(driver_result),
        )

    def reconcile_work_graph_issue_inbox(
        payload: GitHubIssueInboxReconcileRequest,
        identity: Annotated[LaunchplaneIdentity, Depends(read_write_identity)],
    ) -> WorkGraphIssueInboxReconcileResponse:
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
        return WorkGraphIssueInboxReconcileResponse(
            trace_id=trace_id,
            records={},
            result=WorkGraphIssueInboxReconcileResponseResult(reconcile=reconcile_result),
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

    def merge_train_github_stale_state_response(
        *, trace_id: str, error: MergeTrainGitHubStaleHeadError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=409,
            content={
                "status": "rejected",
                "trace_id": trace_id,
                "error": {
                    "code": "merge_train_github_stale_state",
                    "message": "Merge train GitHub state changed; retry after rereading upstream state.",
                },
                "details": {"github_status_code": error.status_code},
            },
        )

    def merge_train_github_request_failed_response(
        *, trace_id: str, error: MergeTrainGitHubError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=502,
            content={
                "status": "rejected",
                "trace_id": trace_id,
                "error": {
                    "code": "github_request_failed",
                    "message": "GitHub merge train request failed; retry after upstream recovers.",
                },
                "details": {
                    "github_status_code": error.status_code,
                },
            },
        )

    async def write_merge_train_batch_candidate_run_once(
        request: Request,
        identity: Annotated[LaunchplaneIdentity, Depends(read_write_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> AcceptedEvidenceResponse | JSONResponse:
        trace_id = next_trace_id()
        try:
            raw_payload = await request.json()
        except ValueError as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request payload failed validation.",
            ) from error
        if not isinstance(raw_payload, dict):
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request payload failed validation.",
            )
        try:
            batch_request = MergeTrainBatchCandidateRunOnceEnvelope.model_validate(raw_payload)
        except ValidationError as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request payload failed validation.",
            ) from error

        normalized_idempotency_key = idempotency_key.strip()
        payload_fingerprint = build_request_fingerprint(raw_payload)
        if normalized_idempotency_key:
            (
                normalized_idempotency_key,
                payload_fingerprint,
                replay_response,
            ) = await replay_apply_idempotency(
                request=request,
                record_store=record_store,
                identity=identity,
                route_path=_MERGE_TRAIN_BATCH_CANDIDATE_RUN_ONCE_ROUTE,
                idempotency_key=normalized_idempotency_key,
                trace_id=trace_id,
                check_replay=True,
            )
            if replay_response is not None:
                return replay_response

        try:
            policy_record = resolve_merge_train_policy_record(record_store)
        except MergeTrainPolicyStoreMissingError as error:
            raise merge_train_policy_not_configured_error(trace_id=trace_id, error=error) from error
        try:
            repository_policy = policy_record.policy.find_repository_policy(
                repository=batch_request.repository,
                base_branch=batch_request.base_branch,
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
                message="Workflow cannot run the requested merge train policy.",
            )
        token_env = repository_policy.github_token.env_var
        if not token_env:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="github_token_not_configured",
                message="Merge train policy does not define a GitHub token environment variable.",
            )
        token = os.environ.get(token_env, "").strip()
        if not token:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="github_token_not_configured",
                message="Configured merge train GitHub token is not available.",
            )
        try:
            batch_store = require_merge_train_batch_candidate_record_store(record_store)
            stack_collapse_store = require_merge_train_stack_collapse_plan_record_store(
                record_store
            )
            controller_state_store = require_merge_train_controller_state_record_store(record_store)
        except TypeError as error:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message="Merge train batch candidate storage requires database-backed records.",
            ) from error
        try:
            with merge_train_controller_mutation_fence(
                record_store=controller_state_store,
                repository=batch_request.repository,
                base_branch=batch_request.base_branch,
                policy_key=repository_policy.policy_key,
                policy_sha256=policy_record.policy_sha256,
                trace_id=trace_id,
                active_action="batch_candidate_run_once",
                active_phase=batch_request.mode,
                active_record_id=batch_request.candidate_record_id,
            ) as lease:

                def checkpoint_candidate_mutation(
                    phase: str, pull_request_number: int | None
                ) -> None:
                    lease.checkpoint(
                        active_action="batch_candidate_run_once",
                        active_phase=phase.split(":", 1)[0],
                        active_record_id=batch_request.candidate_record_id,
                        active_pull_request_number=pull_request_number,
                        step_payload={"mode": batch_request.mode},
                    )

                batch_result = execute_merge_train_batch_candidate_run_once(
                    request=batch_request,
                    policy=policy_record.policy,
                    policy_sha256=policy_record.policy_sha256,
                    token=token,
                    trace_id=trace_id,
                    recorded_at=lease.record.updated_at,
                    batch_store=batch_store,
                    stack_collapse_store=stack_collapse_store,
                    mutation_checkpoint=checkpoint_candidate_mutation,
                )
                response = accepted_evidence_response(
                    trace_id=trace_id,
                    records=batch_result.records,
                    result=batch_result.accepted_result,
                )
                store_apply_idempotency(
                    record_store=record_store,
                    identity=identity,
                    route_path=_MERGE_TRAIN_BATCH_CANDIDATE_RUN_ONCE_ROUTE,
                    idempotency_key=normalized_idempotency_key,
                    request_fingerprint_value=payload_fingerprint,
                    trace_id=trace_id,
                    response=response,
                )
        except MergeTrainGitHubStaleHeadError as error:
            return merge_train_github_stale_state_response(trace_id=trace_id, error=error)
        except MergeTrainGitHubError as error:
            return merge_train_github_request_failed_response(trace_id=trace_id, error=error)
        except (
            MergeTrainControllerLeaseHeldError,
            MergeTrainControllerLeaseLostError,
            MergeTrainControllerReconciliationRequiredError,
            MergeTrainControllerAdoptionRejectedError,
        ) as error:
            raise merge_train_controller_fence_http_error(trace_id=trace_id, error=error) from error
        except MergeTrainBatchCandidateRecordNotFoundError as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request could not be completed.",
            ) from error
        return response

    async def write_merge_train_controller_run_once(
        request: Request,
        identity: Annotated[LaunchplaneIdentity, Depends(read_write_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> AcceptedEvidenceResponse | JSONResponse:
        trace_id = next_trace_id()
        try:
            raw_payload = await request.json()
        except ValueError as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="merge_train_controller_invalid_state",
                message="Request payload failed validation.",
            ) from error
        if not isinstance(raw_payload, dict):
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="merge_train_controller_invalid_state",
                message="Request payload failed validation.",
            )
        try:
            controller_request = MergeTrainControllerRunOnceEnvelope.model_validate(raw_payload)
        except ValidationError as error:
            message = str(error).strip() or "Request payload failed validation."
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="merge_train_controller_invalid_state",
                message=message,
            ) from error

        normalized_idempotency_key = idempotency_key.strip()
        payload_fingerprint = build_request_fingerprint(raw_payload)
        if normalized_idempotency_key:
            (
                normalized_idempotency_key,
                payload_fingerprint,
                replay_response,
            ) = await replay_apply_idempotency(
                request=request,
                record_store=record_store,
                identity=identity,
                route_path=_MERGE_TRAIN_CONTROLLER_RUN_ONCE_ROUTE,
                idempotency_key=normalized_idempotency_key,
                trace_id=trace_id,
                check_replay=True,
            )
            if replay_response is not None:
                return replay_response

        try:
            policy_record = resolve_merge_train_policy_record(record_store)
        except MergeTrainPolicyStoreMissingError as error:
            raise merge_train_policy_not_configured_error(trace_id=trace_id, error=error) from error
        try:
            repository_policy = policy_record.policy.find_repository_policy(
                repository=controller_request.repository,
                base_branch=controller_request.base_branch,
            )
        except ValueError as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="merge_train_controller_invalid_state",
                message=str(error).strip()
                or "Merge train controller request could not be completed.",
            ) from error
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
                message="Workflow cannot run the requested merge train policy.",
            )
        token_env = repository_policy.github_token.env_var
        if not token_env:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="github_token_not_configured",
                message="Merge train policy does not define a GitHub token environment variable.",
            )
        token = os.environ.get(token_env, "").strip()
        if not token:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="github_token_not_configured",
                message="Configured merge train GitHub token is not available.",
            )
        try:
            candidate_store = require_merge_train_batch_candidate_record_store(record_store)
            landing_store = require_merge_train_batch_landing_plan_record_store(record_store)
            stack_collapse_store = require_merge_train_stack_collapse_plan_record_store(
                record_store
            )
            controller_state_store = require_merge_train_controller_state_record_store(record_store)
            admission_store = require_merge_admission_record_store(record_store)
        except TypeError as error:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message="Merge train controller storage requires database-backed records.",
            ) from error
        try:

            def store_controller_idempotency_before_release(
                result: MergeTrainControllerRunOnceResult,
            ) -> None:
                idempotent_response = accepted_evidence_response(
                    trace_id=trace_id,
                    records=result.records,
                    result=result.accepted_result,
                )
                store_apply_idempotency(
                    record_store=record_store,
                    identity=identity,
                    route_path=_MERGE_TRAIN_CONTROLLER_RUN_ONCE_ROUTE,
                    idempotency_key=normalized_idempotency_key,
                    request_fingerprint_value=payload_fingerprint,
                    trace_id=trace_id,
                    response=idempotent_response,
                )

            admission_evaluator = LiveMergeAdmissionEvaluator(
                store=record_store,
                repository_evidence_provider=resolved_change_impact_repository_evidence_provider,
                technical_check_client=TenantAdmissionControllerGitHubClient(
                    transport=UrllibMergeTrainGitHubTransport(
                        token=token,
                        api_base_url=controller_request.github_api_base_url,
                    )
                ),
            )
            controller_result = execute_merge_train_controller_run_once(
                request=controller_request,
                policy=policy_record.policy,
                policy_sha256=policy_record.policy_sha256,
                repository_policy=repository_policy,
                token=token,
                trace_id=trace_id,
                recorded_at=utc_now_timestamp(),
                candidate_store=candidate_store,
                landing_store=landing_store,
                stack_collapse_store=stack_collapse_store,
                controller_state_store=controller_state_store,
                admission_store=admission_store,
                admission_evaluator=admission_evaluator,
                before_release=store_controller_idempotency_before_release,
            )
        except MergeTrainGitHubStaleHeadError as error:
            return merge_train_github_stale_state_response(trace_id=trace_id, error=error)
        except MergeTrainGitHubError as error:
            return merge_train_github_request_failed_response(trace_id=trace_id, error=error)
        except MergeAdmissionDeniedError as error:
            raise _launchplane_http_error(
                status_code=409,
                trace_id=trace_id,
                code="merge_train_landing_not_admitted",
                message=str(error),
            ) from error
        except MergeAdmissionReconciliationRequiredError as error:
            raise _launchplane_http_error(
                status_code=409,
                trace_id=trace_id,
                code="merge_train_landing_reconcile_required",
                message=str(error),
            ) from error
        except (
            MergeTrainControllerLeaseHeldError,
            MergeTrainControllerLeaseLostError,
            MergeTrainControllerReconciliationRequiredError,
            MergeTrainControllerAdoptionRejectedError,
        ) as error:
            raise merge_train_controller_fence_http_error(trace_id=trace_id, error=error) from error
        except (MergeTrainControllerRequestError, ValueError, click.ClickException) as error:
            message = str(error).strip() or "Merge train controller request could not be completed."
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="merge_train_controller_invalid_state",
                message=message,
            ) from error
        response = accepted_evidence_response(
            trace_id=trace_id,
            records=controller_result.records,
            result=controller_result.accepted_result,
        )
        return response

    def driver_route_dependency_not_found_response(
        *, trace_id: str, route_path: str
    ) -> JSONResponse:
        return JSONResponse(
            status_code=503,
            content={
                "status": "rejected",
                "trace_id": trace_id,
                "error": {
                    "code": "driver_route_dependency_not_found",
                    "message": (
                        "Driver route is registered, but required product or runtime"
                        " records were not found."
                    ),
                },
                "details": {"route_path": route_path},
            },
        )

    async def write_odoo_artifact_publish(
        request: Request,
        identity: Annotated[LaunchplaneIdentity, Depends(read_write_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> AcceptedEvidenceResponse | JSONResponse:
        trace_id = next_trace_id()
        try:
            raw_payload = await request.json()
        except ValueError as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request payload failed validation.",
            ) from error
        if not isinstance(raw_payload, dict):
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request payload failed validation.",
            )
        try:
            publish_request = OdooArtifactPublishEnvelope.model_validate(raw_payload)
        except ValidationError as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request payload failed validation.",
            ) from error
        try:
            product_profile = resolve_odoo_artifact_publish_product_route(
                record_store=record_store,
                product=publish_request.product,
                context=publish_request.publish.context,
                instance=publish_request.publish.instance,
            )
            validate_odoo_artifact_publish_product_evidence(
                product_profile=product_profile,
                request=publish_request,
            )
        except OdooArtifactPublishRouteDependencyError:
            return driver_route_dependency_not_found_response(
                trace_id=trace_id,
                route_path=_ODOO_ARTIFACT_PUBLISH_ROUTE,
            )
        except OdooArtifactPublishProductMismatchError as error:
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="product_driver_mismatch",
                message="Product is not configured for the requested driver route.",
            ) from error
        except ValueError as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request could not be completed.",
            ) from error

        authorization_product = product_profile.product
        if not native_routes.native_driver_route_authorization_allows(
            endpoint=write_odoo_artifact_publish,
            authorization_allows=resolved_authz_policy_runtime.policy.allows,
            identity=identity,
            product=authorization_product,
            context=publish_request.publish.context,
            instances=(publish_request.publish.instance,),
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message=(
                    "Workflow cannot write Odoo artifact publish evidence for the requested"
                    " product/context."
                ),
            )

        (
            normalized_idempotency_key,
            payload_fingerprint,
            replay_response,
        ) = await replay_apply_idempotency(
            request=request,
            record_store=record_store,
            identity=identity,
            route_path=_ODOO_ARTIFACT_PUBLISH_ROUTE,
            idempotency_key=idempotency_key,
            trace_id=trace_id,
            check_replay=bool(idempotency_key.strip()),
        )
        if replay_response is not None:
            return replay_response

        try:
            records, driver_result = ingest_odoo_artifact_publish_evidence_result(
                record_store=record_store,
                request=publish_request,
            )
        except FileNotFoundError as error:
            raise _launchplane_http_error(
                status_code=404,
                trace_id=trace_id,
                code="not_found",
                message=f"No Launchplane route for {_ODOO_ARTIFACT_PUBLISH_ROUTE}.",
            ) from error
        except (ValueError, click.ClickException) as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request could not be completed.",
            ) from error

        response = accepted_evidence_response(
            trace_id=trace_id,
            records={key: string_value(value) for key, value in records.items()},
            result=driver_result,
        )
        if should_store_odoo_artifact_publish_idempotency(driver_result):
            store_apply_idempotency(
                record_store=record_store,
                identity=identity,
                route_path=_ODOO_ARTIFACT_PUBLISH_ROUTE,
                idempotency_key=normalized_idempotency_key,
                request_fingerprint_value=payload_fingerprint,
                trace_id=trace_id,
                response=response,
            )
        return response

    async def write_odoo_artifact_publish_inputs(
        request: Request,
        identity: Annotated[LaunchplaneIdentity, Depends(read_write_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> AcceptedEvidenceResponse | JSONResponse:
        trace_id = next_trace_id()
        try:
            raw_payload = await request.json()
        except ValueError as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request payload failed validation.",
            ) from error
        if not isinstance(raw_payload, dict):
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request payload failed validation.",
            )
        try:
            inputs_request = OdooArtifactPublishInputsEnvelope.model_validate(raw_payload)
        except ValidationError as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request payload failed validation.",
            ) from error
        try:
            product_profile = resolve_odoo_artifact_publish_inputs_profile(
                record_store=record_store,
                request=inputs_request,
            )
        except OdooArtifactPublishInputsRouteDependencyError:
            return driver_route_dependency_not_found_response(
                trace_id=trace_id,
                route_path=_ODOO_ARTIFACT_PUBLISH_INPUTS_ROUTE,
            )
        except OdooArtifactPublishInputsProductMismatchError as error:
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="product_driver_mismatch",
                message="Product is not configured for the requested driver route.",
            ) from error
        except ValueError as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request could not be completed.",
            ) from error

        if not native_routes.native_driver_route_authorization_allows(
            endpoint=write_odoo_artifact_publish_inputs,
            authorization_allows=resolved_authz_policy_runtime.policy.allows,
            identity=identity,
            product=inputs_request.product,
            context=inputs_request.inputs.context,
            instances=(inputs_request.inputs.instance,),
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message=(
                    "Workflow cannot read Odoo artifact publish inputs for the requested"
                    " product/context."
                ),
            )

        (
            normalized_idempotency_key,
            payload_fingerprint,
            replay_response,
        ) = await replay_apply_idempotency(
            request=request,
            record_store=record_store,
            identity=identity,
            route_path=_ODOO_ARTIFACT_PUBLISH_INPUTS_ROUTE,
            idempotency_key=idempotency_key,
            trace_id=trace_id,
            check_replay=bool(idempotency_key.strip()),
        )
        if replay_response is not None:
            return replay_response

        try:
            driver_result = build_odoo_artifact_publish_inputs_result(
                control_plane_root=resolved_control_plane_root,
                request=inputs_request,
                product_profile=product_profile,
            )
        except OdooArtifactPublishInputsRouteDependencyError:
            return driver_route_dependency_not_found_response(
                trace_id=trace_id,
                route_path=_ODOO_ARTIFACT_PUBLISH_INPUTS_ROUTE,
            )
        except FileNotFoundError as error:
            raise _launchplane_http_error(
                status_code=404,
                trace_id=trace_id,
                code="not_found",
                message=f"No Launchplane route for {_ODOO_ARTIFACT_PUBLISH_INPUTS_ROUTE}.",
            ) from error
        except (ValueError, click.ClickException) as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request could not be completed.",
            ) from error

        response = accepted_evidence_response(
            trace_id=trace_id,
            records={},
            result=driver_result,
        )
        store_apply_idempotency(
            record_store=record_store,
            identity=identity,
            route_path=_ODOO_ARTIFACT_PUBLISH_INPUTS_ROUTE,
            idempotency_key=normalized_idempotency_key,
            request_fingerprint_value=payload_fingerprint,
            trace_id=trace_id,
            response=response,
        )
        return response

    async def write_odoo_preview_apply_inputs(
        request: Request,
        identity: Annotated[LaunchplaneIdentity, Depends(read_write_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> AcceptedEvidenceResponse | JSONResponse:
        trace_id = next_trace_id()
        try:
            raw_payload = await request.json()
        except ValueError as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request payload failed validation.",
            ) from error
        if not isinstance(raw_payload, dict):
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request payload failed validation.",
            )
        try:
            inputs_request = OdooPreviewApplyInputsEnvelope.model_validate(raw_payload)
        except ValidationError as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request payload failed validation.",
            ) from error
        try:
            product_profile = resolve_odoo_preview_apply_profile(
                record_store=record_store,
                product=inputs_request.product,
            )
        except OdooPreviewApplyRouteDependencyError:
            return driver_route_dependency_not_found_response(
                trace_id=trace_id,
                route_path=_ODOO_PREVIEW_APPLY_INPUTS_ROUTE,
            )
        except OdooPreviewApplyProductMismatchError as error:
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="product_driver_mismatch",
                message="Product is not configured for the requested driver route.",
            ) from error
        except ValueError as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request could not be completed.",
            ) from error

        if not native_routes.native_driver_route_authorization_allows(
            endpoint=write_odoo_preview_apply_inputs,
            authorization_allows=resolved_authz_policy_runtime.policy.allows,
            identity=identity,
            product=product_profile.product,
            context=product_profile.preview.context,
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message=(
                    "Workflow cannot read Odoo preview apply inputs for the requested"
                    " product/context."
                ),
            )

        normalized_idempotency_key = idempotency_key.strip()
        if not normalized_idempotency_key:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="idempotency_key_required",
                message="Odoo preview apply inputs require an Idempotency-Key header.",
            )
        if idempotency_capable_store(record_store) is None:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="plan_storage_required",
                message="Odoo preview plan issuance requires durable idempotency storage.",
            )
        payload_fingerprint = idempotency_request_fingerprint(
            route_path=_ODOO_PREVIEW_APPLY_INPUTS_ROUTE,
            payload=raw_payload,
        )
        plan_id = build_odoo_preview_plan_id(
            scope=idempotency_scope(identity),
            idempotency_key=normalized_idempotency_key,
        )
        replay_response = replay_stored_apply_idempotency(
            record_store=record_store,
            identity=identity,
            route_path=_ODOO_PREVIEW_APPLY_INPUTS_ROUTE,
            idempotency_key=plan_id,
            request_fingerprint_value=payload_fingerprint,
            trace_id=trace_id,
        )
        if replay_response is not None:
            return replay_response

        try:
            driver_result = build_odoo_preview_apply_inputs_result(
                control_plane_root=resolved_control_plane_root,
                record_store=record_store,
                profile=product_profile,
                request=inputs_request.inputs,
                database_url=getattr(record_store, "database_url", None),
            )
        except FileNotFoundError as error:
            raise _launchplane_http_error(
                status_code=404,
                trace_id=trace_id,
                code="not_found",
                message=f"No Launchplane route for {_ODOO_PREVIEW_APPLY_INPUTS_ROUTE}.",
            ) from error
        except (ValueError, click.ClickException) as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request could not be completed.",
            ) from error

        issued_plan = issue_odoo_preview_apply_plan(
            result=driver_result,
            plan_id=plan_id,
        )
        response = accepted_evidence_response(
            trace_id=trace_id,
            records={},
            result=issued_plan.model_dump(mode="json"),
        )
        if issued_plan.status == "ready":
            try:
                store_apply_idempotency(
                    record_store=record_store,
                    identity=identity,
                    route_path=_ODOO_PREVIEW_APPLY_INPUTS_ROUTE,
                    idempotency_key=plan_id,
                    request_fingerprint_value=payload_fingerprint,
                    trace_id=trace_id,
                    response=response,
                )
            except Exception as write_error:
                replay_response = replay_stored_apply_idempotency(
                    record_store=record_store,
                    identity=identity,
                    route_path=_ODOO_PREVIEW_APPLY_INPUTS_ROUTE,
                    idempotency_key=plan_id,
                    request_fingerprint_value=payload_fingerprint,
                    trace_id=trace_id,
                )
                if replay_response is not None:
                    return replay_response
                raise write_error
        return response

    async def write_odoo_preview_apply(
        request: Request,
        identity: Annotated[LaunchplaneIdentity, Depends(read_write_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> AcceptedEvidenceResponse | JSONResponse:
        trace_id = next_trace_id()
        try:
            raw_payload = await request.json()
        except ValueError as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request payload failed validation.",
            ) from error
        if not isinstance(raw_payload, dict):
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request payload failed validation.",
            )
        try:
            apply_request = OdooPreviewApplyEnvelope.model_validate(raw_payload)
        except ValidationError as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request payload failed validation.",
            ) from error
        try:
            product_profile = resolve_odoo_preview_apply_profile(
                record_store=record_store,
                product=apply_request.product,
            )
        except OdooPreviewApplyRouteDependencyError:
            return driver_route_dependency_not_found_response(
                trace_id=trace_id,
                route_path=_ODOO_PREVIEW_APPLY_ROUTE,
            )
        except OdooPreviewApplyProductMismatchError as error:
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="product_driver_mismatch",
                message="Product is not configured for the requested driver route.",
            ) from error
        except ValueError as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request could not be completed.",
            ) from error

        if not native_routes.native_driver_route_authorization_allows(
            endpoint=write_odoo_preview_apply,
            authorization_allows=resolved_authz_policy_runtime.policy.allows,
            identity=identity,
            product=product_profile.product,
            context=product_profile.preview.context,
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message=(
                    "Workflow cannot apply Odoo preview provider state for the requested"
                    " product/context."
                ),
            )

        normalized_idempotency_key = idempotency_key.strip()
        if not normalized_idempotency_key:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="idempotency_key_required",
                message="Odoo preview apply requests require an Idempotency-Key header.",
            )
        idempotency_store = idempotency_capable_store(record_store)
        if idempotency_store is None:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="plan_storage_required",
                message="Odoo preview apply requires durable plan storage.",
            )
        stored_plan_record = idempotency_store.read_idempotency_record(
            scope=idempotency_scope(identity),
            route_path=_ODOO_PREVIEW_APPLY_INPUTS_ROUTE,
            idempotency_key=normalized_idempotency_key,
        )
        if stored_plan_record is None or stored_plan_record.state != "completed":
            raise _launchplane_http_error(
                status_code=409,
                trace_id=trace_id,
                code="odoo_preview_plan_not_issued",
                message="Odoo preview apply requires a matching service-issued plan.",
            )
        stored_plan_payload = stored_plan_record.response_payload.get("result")
        try:
            issued_plan = OdooPreviewApplyInputsResult.model_validate(stored_plan_payload)
            service_apply_request = validate_odoo_preview_issued_plan(
                plan_id=normalized_idempotency_key,
                issued_plan=issued_plan,
                request=apply_request,
            )
            validate_odoo_preview_profile_authority(
                profile=product_profile,
                issued_plan=issued_plan,
            )
        except ValidationError as error:
            raise _launchplane_http_error(
                status_code=409,
                trace_id=trace_id,
                code="odoo_preview_plan_not_issued",
                message="Stored Odoo preview plan evidence is invalid.",
            ) from error
        except OdooPreviewPlanProvenanceError as error:
            raise _launchplane_http_error(
                status_code=409,
                trace_id=trace_id,
                code=error.code,
                message=str(error),
            ) from error
        payload_fingerprint = idempotency_request_fingerprint(
            route_path=_ODOO_PREVIEW_APPLY_ROUTE,
            payload=raw_payload,
        )
        adapter = _OdooPreviewProviderMutationAdapter(
            control_plane_root=resolved_control_plane_root,
            record_store=record_store,
            profile=product_profile,
            apply_request=service_apply_request,
            issued_plan=issued_plan,
            database_url=getattr(record_store, "database_url", None),
            trace_id=trace_id,
            deployment_record_id=build_launchplane_mutation_reservation_id(
                scope=idempotency_scope(identity),
                route_path=_ODOO_PREVIEW_APPLY_ROUTE,
                idempotency_key=normalized_idempotency_key,
            ),
        )
        target_supersession = None
        if service_apply_request.apply.dry_run_plan.operation == "destroy":
            target_supersession = ProviderTargetSupersession(
                response_status_code=409,
                response_payload=_provider_operation_response_payload(
                    trace_id=trace_id,
                    records={},
                    result={
                        "status": "fail",
                        "error_message": (
                            "The earlier Odoo preview apply was superseded by an "
                            "authoritative destroy after its recovery lease expired."
                        ),
                    },
                ),
                minimum_expired_seconds=ODOO_PREVIEW_DESTROY_SUPERSESSION_GRACE_SECONDS,
                quiescence_check=lambda _reservation: (
                    odoo_preview_destroy_supersession_is_quiescent(
                        control_plane_root_path=resolved_control_plane_root,
                        request=service_apply_request,
                        database_url=getattr(record_store, "database_url", None),
                    )
                ),
            )
        try:
            response = await run_provider_mutation(
                record_store=record_store,
                identity=identity,
                route_path=_ODOO_PREVIEW_APPLY_ROUTE,
                idempotency_key=normalized_idempotency_key,
                request_fingerprint=payload_fingerprint,
                trace_id=trace_id,
                adapter=adapter,
                in_progress_message=(
                    "A matching Odoo preview apply is already running. "
                    "Retry with the same Idempotency-Key."
                ),
                reconcile_message="The Odoo preview apply requires reconciliation before retry.",
                target_supersession=target_supersession,
            )
            driver_result = response.result or {}
            if string_value(driver_result.get("status") or "").strip() != "pass":
                return response
            validate_odoo_preview_lifecycle_response_current(
                record_store=record_store,
                profile=product_profile,
                issued_plan=issued_plan,
                records=response.records,
            )
            pr_number = issued_plan.plan_request.pr_number
            reconcile_manager_preview_approval_for_pr_best_effort(
                repository=product_profile.repository,
                pr_number=pr_number,
                record_store=record_store,
                control_plane_root=resolved_control_plane_root,
            )
            return response
        except OdooPreviewPlanProvenanceError as error:
            raise _launchplane_http_error(
                status_code=409,
                trace_id=trace_id,
                code=error.code,
                message=str(error),
            ) from error
        except OdooPreviewApplyConfigError as error:
            return JSONResponse(
                status_code=400,
                content={
                    "status": "rejected",
                    "trace_id": trace_id,
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
        except FileNotFoundError as error:
            raise _launchplane_http_error(
                status_code=404,
                trace_id=trace_id,
                code="not_found",
                message=f"No Launchplane route for {_ODOO_PREVIEW_APPLY_ROUTE}.",
            ) from error
        except (ValueError, click.ClickException) as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request could not be completed.",
            ) from error

    async def write_odoo_post_deploy(
        request: Request,
        identity: Annotated[LaunchplaneIdentity, Depends(read_write_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> AcceptedEvidenceResponse | JSONResponse:
        trace_id = next_trace_id()
        try:
            raw_payload = await request.json()
        except ValueError as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request payload failed validation.",
            ) from error
        if not isinstance(raw_payload, dict):
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request payload failed validation.",
            )
        try:
            post_deploy_request = OdooPostDeployEnvelope.model_validate(raw_payload)
        except ValidationError as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request payload failed validation.",
            ) from error
        try:
            product_profile = resolve_odoo_post_deploy_product_route(
                record_store=record_store,
                product=post_deploy_request.product,
                context=post_deploy_request.post_deploy.context,
                instance=post_deploy_request.post_deploy.instance,
            )
        except OdooPostDeployRouteDependencyError:
            return driver_route_dependency_not_found_response(
                trace_id=trace_id,
                route_path=_ODOO_POST_DEPLOY_ROUTE,
            )
        except OdooPostDeployProductMismatchError as error:
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="product_driver_mismatch",
                message="Product is not configured for the requested driver route.",
            ) from error
        except ValueError as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request could not be completed.",
            ) from error

        authorization_product = product_profile.product
        if not native_routes.native_driver_route_authorization_allows(
            endpoint=write_odoo_post_deploy,
            authorization_allows=resolved_authz_policy_runtime.policy.allows,
            identity=identity,
            product=authorization_product,
            context=post_deploy_request.post_deploy.context,
            instances=(post_deploy_request.post_deploy.instance,),
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message=(
                    "Workflow cannot execute the Odoo post-deploy driver for the requested"
                    " product/context."
                ),
            )

        (
            normalized_idempotency_key,
            payload_fingerprint,
            replay_response,
        ) = await replay_apply_idempotency(
            request=request,
            record_store=record_store,
            identity=identity,
            route_path=_ODOO_POST_DEPLOY_ROUTE,
            idempotency_key=idempotency_key,
            trace_id=trace_id,
            check_replay=bool(idempotency_key.strip()),
        )
        if replay_response is not None:
            return replay_response

        try:
            records, driver_result = execute_odoo_post_deploy_result(
                control_plane_root=resolved_control_plane_root,
                record_store=record_store,
                request=post_deploy_request,
            )
        except FileNotFoundError as error:
            raise _launchplane_http_error(
                status_code=404,
                trace_id=trace_id,
                code="not_found",
                message=f"No Launchplane route for {_ODOO_POST_DEPLOY_ROUTE}.",
            ) from error
        except (ValueError, click.ClickException) as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request could not be completed.",
            ) from error

        response = accepted_evidence_response(
            trace_id=trace_id,
            records={key: string_value(value) for key, value in records.items()},
            result=driver_result,
        )
        store_apply_idempotency(
            record_store=record_store,
            identity=identity,
            route_path=_ODOO_POST_DEPLOY_ROUTE,
            idempotency_key=normalized_idempotency_key,
            request_fingerprint_value=payload_fingerprint,
            trace_id=trace_id,
            response=response,
        )
        return response

    async def write_odoo_app_maintenance(
        request: Request,
        identity: Annotated[LaunchplaneIdentity, Depends(read_write_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> AcceptedEvidenceResponse | JSONResponse:
        trace_id = next_trace_id()
        try:
            raw_payload = await request.json()
        except ValueError as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request payload failed validation.",
            ) from error
        if not isinstance(raw_payload, dict):
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request payload failed validation.",
            )
        try:
            maintenance_request = OdooAppMaintenanceEnvelope.model_validate(raw_payload)
        except ValidationError as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request payload failed validation.",
            ) from error
        try:
            product_profile = resolve_odoo_app_maintenance_product_route(
                record_store=record_store,
                product=maintenance_request.product,
                context=maintenance_request.maintenance.context,
                instance=maintenance_request.maintenance.instance,
            )
        except OdooAppMaintenanceRouteDependencyError:
            return driver_route_dependency_not_found_response(
                trace_id=trace_id,
                route_path=_ODOO_APP_MAINTENANCE_ROUTE,
            )
        except OdooAppMaintenanceProductMismatchError as error:
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="product_driver_mismatch",
                message="Product is not configured for the requested driver route.",
            ) from error
        except ValueError as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request could not be completed.",
            ) from error

        authorization_product = product_profile.product
        if not native_routes.native_driver_route_authorization_allows(
            endpoint=write_odoo_app_maintenance,
            authorization_allows=resolved_authz_policy_runtime.policy.allows,
            identity=identity,
            product=authorization_product,
            context=maintenance_request.maintenance.context,
            instances=(maintenance_request.maintenance.instance,),
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message=(
                    "Workflow cannot execute the Odoo app maintenance driver"
                    " for the requested product/context."
                ),
            )

        (
            normalized_idempotency_key,
            payload_fingerprint,
            replay_response,
        ) = await replay_apply_idempotency(
            request=request,
            record_store=record_store,
            identity=identity,
            route_path=_ODOO_APP_MAINTENANCE_ROUTE,
            idempotency_key=idempotency_key,
            trace_id=trace_id,
            check_replay=bool(idempotency_key.strip()),
        )
        if replay_response is not None:
            return replay_response

        try:
            records, driver_result = execute_odoo_app_maintenance_result(
                control_plane_root=resolved_control_plane_root,
                record_store=record_store,
                request=maintenance_request,
            )
        except FileNotFoundError as error:
            raise _launchplane_http_error(
                status_code=404,
                trace_id=trace_id,
                code="not_found",
                message=f"No Launchplane route for {_ODOO_APP_MAINTENANCE_ROUTE}.",
            ) from error
        except (ValueError, click.ClickException) as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request could not be completed.",
            ) from error

        response = accepted_evidence_response(
            trace_id=trace_id,
            records={key: string_value(value) for key, value in records.items()},
            result=driver_result,
        )
        if should_store_odoo_app_maintenance_idempotency(driver_result):
            store_apply_idempotency(
                record_store=record_store,
                identity=identity,
                route_path=_ODOO_APP_MAINTENANCE_ROUTE,
                idempotency_key=normalized_idempotency_key,
                request_fingerprint_value=payload_fingerprint,
                trace_id=trace_id,
                response=response,
            )
        return response

    async def write_odoo_config_parameter_override(
        request: Request,
        identity: Annotated[LaunchplaneIdentity, Depends(read_write_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> AcceptedEvidenceResponse | JSONResponse:
        trace_id = next_trace_id()
        try:
            raw_payload = await request.json()
        except ValueError as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request payload failed validation.",
            ) from error
        if not isinstance(raw_payload, dict):
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request payload failed validation.",
            )
        try:
            override_request = OdooConfigParameterOverrideEnvelope.model_validate(raw_payload)
        except ValidationError as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request payload failed validation.",
            ) from error
        try:
            product_profile = resolve_odoo_post_deploy_product_route(
                record_store=record_store,
                product=override_request.product,
                context=override_request.override.context,
                instance=override_request.override.instance,
            )
        except OdooPostDeployRouteDependencyError:
            return driver_route_dependency_not_found_response(
                trace_id=trace_id,
                route_path=_ODOO_CONFIG_PARAMETER_OVERRIDE_ROUTE,
            )
        except OdooPostDeployProductMismatchError as error:
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="product_driver_mismatch",
                message="Product is not configured for the requested driver route.",
            ) from error
        except ValueError as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request could not be completed.",
            ) from error

        authorization_product = product_profile.product
        if not native_routes.native_driver_route_authorization_allows(
            endpoint=write_odoo_config_parameter_override,
            authorization_allows=resolved_authz_policy_runtime.policy.allows,
            identity=identity,
            product=authorization_product,
            context=override_request.override.context,
            instances=(override_request.override.instance,),
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message=(
                    "Workflow cannot write Odoo config-parameter overrides for the requested"
                    " product/context."
                ),
            )

        (
            normalized_idempotency_key,
            payload_fingerprint,
            replay_response,
        ) = await replay_apply_idempotency(
            request=request,
            record_store=record_store,
            identity=identity,
            route_path=_ODOO_CONFIG_PARAMETER_OVERRIDE_ROUTE,
            idempotency_key=idempotency_key,
            trace_id=trace_id,
            check_replay=bool(idempotency_key.strip()),
        )
        if replay_response is not None:
            return replay_response

        try:
            driver_result = write_odoo_config_parameter_override_result(
                record_store=cast(OdooInstanceOverrideStore, record_store),
                request=override_request,
            )
        except FileNotFoundError as error:
            raise _launchplane_http_error(
                status_code=404,
                trace_id=trace_id,
                code="not_found",
                message=f"No Launchplane route for {_ODOO_CONFIG_PARAMETER_OVERRIDE_ROUTE}.",
            ) from error
        except (ValueError, click.ClickException) as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request could not be completed.",
            ) from error

        response = accepted_evidence_response(
            trace_id=trace_id,
            records={},
            result=driver_result,
        )
        store_apply_idempotency(
            record_store=record_store,
            identity=identity,
            route_path=_ODOO_CONFIG_PARAMETER_OVERRIDE_ROUTE,
            idempotency_key=normalized_idempotency_key,
            request_fingerprint_value=payload_fingerprint,
            trace_id=trace_id,
            response=response,
        )
        return response

    async def write_odoo_website_bootstrap_override(
        request: Request,
        identity: Annotated[LaunchplaneIdentity, Depends(read_write_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> AcceptedEvidenceResponse | JSONResponse:
        trace_id = next_trace_id()
        try:
            raw_payload = await request.json()
        except ValueError as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request payload failed validation.",
            ) from error
        if not isinstance(raw_payload, dict):
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request payload failed validation.",
            )
        try:
            override_request = OdooWebsiteBootstrapOverrideEnvelope.model_validate(raw_payload)
        except ValidationError as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request payload failed validation.",
            ) from error
        try:
            product_profile = resolve_odoo_post_deploy_product_route(
                record_store=record_store,
                product=override_request.product,
                context=override_request.override.context,
                instance=override_request.override.instance,
            )
        except OdooPostDeployRouteDependencyError:
            return driver_route_dependency_not_found_response(
                trace_id=trace_id,
                route_path=_ODOO_WEBSITE_BOOTSTRAP_OVERRIDE_ROUTE,
            )
        except OdooPostDeployProductMismatchError as error:
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="product_driver_mismatch",
                message="Product is not configured for the requested driver route.",
            ) from error
        except ValueError as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request could not be completed.",
            ) from error

        authorization_product = product_profile.product
        if not native_routes.native_driver_route_authorization_allows(
            endpoint=write_odoo_website_bootstrap_override,
            authorization_allows=resolved_authz_policy_runtime.policy.allows,
            identity=identity,
            product=authorization_product,
            context=override_request.override.context,
            instances=(override_request.override.instance,),
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message=(
                    "Workflow cannot write Odoo website-bootstrap overrides for the requested"
                    " product/context."
                ),
            )

        (
            normalized_idempotency_key,
            payload_fingerprint,
            replay_response,
        ) = await replay_apply_idempotency(
            request=request,
            record_store=record_store,
            identity=identity,
            route_path=_ODOO_WEBSITE_BOOTSTRAP_OVERRIDE_ROUTE,
            idempotency_key=idempotency_key,
            trace_id=trace_id,
            check_replay=bool(idempotency_key.strip()),
        )
        if replay_response is not None:
            return replay_response

        try:
            driver_result = write_odoo_website_bootstrap_override_result(
                record_store=cast(OdooInstanceOverrideStore, record_store),
                request=override_request,
            )
        except FileNotFoundError as error:
            raise _launchplane_http_error(
                status_code=404,
                trace_id=trace_id,
                code="not_found",
                message=f"No Launchplane route for {_ODOO_WEBSITE_BOOTSTRAP_OVERRIDE_ROUTE}.",
            ) from error
        except (ValueError, click.ClickException) as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request could not be completed.",
            ) from error

        response = accepted_evidence_response(
            trace_id=trace_id,
            records={},
            result=driver_result,
        )
        store_apply_idempotency(
            record_store=record_store,
            identity=identity,
            route_path=_ODOO_WEBSITE_BOOTSTRAP_OVERRIDE_ROUTE,
            idempotency_key=normalized_idempotency_key,
            request_fingerprint_value=payload_fingerprint,
            trace_id=trace_id,
            response=response,
        )
        return response

    async def write_odoo_prod_backup_gate(
        request: Request,
        identity: Annotated[LaunchplaneIdentity, Depends(read_write_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> AcceptedEvidenceResponse | JSONResponse:
        trace_id = next_trace_id()
        try:
            raw_payload = await request.json()
        except ValueError as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request payload failed validation.",
            ) from error
        if not isinstance(raw_payload, dict):
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request payload failed validation.",
            )
        try:
            backup_gate_request = OdooProdBackupGateEnvelope.model_validate(raw_payload)
        except ValidationError as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request payload failed validation.",
            ) from error
        try:
            product_profile = resolve_odoo_prod_backup_gate_product_route(
                record_store=record_store,
                product=backup_gate_request.product,
                context=backup_gate_request.backup_gate.context,
                instance=backup_gate_request.backup_gate.instance,
            )
        except OdooProdBackupGateRouteDependencyError:
            return driver_route_dependency_not_found_response(
                trace_id=trace_id,
                route_path=_ODOO_PROD_BACKUP_GATE_ROUTE,
            )
        except OdooProdBackupGateProductMismatchError as error:
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="product_driver_mismatch",
                message="Product is not configured for the requested driver route.",
            ) from error
        except ValueError as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request could not be completed.",
            ) from error

        authorization_product = product_profile.product
        if not native_routes.native_driver_route_authorization_allows(
            endpoint=write_odoo_prod_backup_gate,
            authorization_allows=resolved_authz_policy_runtime.policy.allows,
            identity=identity,
            product=authorization_product,
            context=backup_gate_request.backup_gate.context,
            instances=(backup_gate_request.backup_gate.instance,),
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message=(
                    "Workflow cannot execute the Odoo prod backup-gate driver"
                    " for the requested product/context."
                ),
            )

        (
            normalized_idempotency_key,
            payload_fingerprint,
            replay_response,
        ) = await replay_apply_idempotency(
            request=request,
            record_store=record_store,
            identity=identity,
            route_path=_ODOO_PROD_BACKUP_GATE_ROUTE,
            idempotency_key=idempotency_key,
            trace_id=trace_id,
            check_replay=bool(idempotency_key.strip()),
        )
        if replay_response is not None:
            return replay_response

        try:
            records, driver_result = execute_odoo_prod_backup_gate_result(
                control_plane_root=resolved_control_plane_root,
                record_store=record_store,
                request=backup_gate_request,
            )
        except FileNotFoundError as error:
            raise _launchplane_http_error(
                status_code=404,
                trace_id=trace_id,
                code="not_found",
                message=f"No Launchplane route for {_ODOO_PROD_BACKUP_GATE_ROUTE}.",
            ) from error
        except (ValueError, click.ClickException) as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request could not be completed.",
            ) from error

        response = accepted_evidence_response(
            trace_id=trace_id,
            records={key: string_value(value) for key, value in records.items()},
            result=driver_result,
        )
        if should_store_odoo_prod_backup_gate_idempotency(driver_result):
            store_apply_idempotency(
                record_store=record_store,
                identity=identity,
                route_path=_ODOO_PROD_BACKUP_GATE_ROUTE,
                idempotency_key=normalized_idempotency_key,
                request_fingerprint_value=payload_fingerprint,
                trace_id=trace_id,
                response=response,
            )
        return response

    async def write_odoo_prod_backup_verification(
        request: Request,
        identity: Annotated[LaunchplaneIdentity, Depends(read_write_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> AcceptedEvidenceResponse | JSONResponse:
        trace_id = next_trace_id()
        try:
            raw_payload = await request.json()
        except ValueError as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request payload failed validation.",
            ) from error
        if not isinstance(raw_payload, dict):
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request payload failed validation.",
            )
        try:
            verification_request = OdooProdBackupVerificationEnvelope.model_validate(raw_payload)
        except ValidationError as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request payload failed validation.",
            ) from error
        try:
            product_profile = resolve_odoo_prod_backup_gate_product_route(
                record_store=record_store,
                product=verification_request.product,
                context=verification_request.backup_verification.context,
                instance=verification_request.backup_verification.instance,
            )
        except OdooProdBackupGateRouteDependencyError:
            return driver_route_dependency_not_found_response(
                trace_id=trace_id,
                route_path=_ODOO_PROD_BACKUP_VERIFICATION_ROUTE,
            )
        except OdooProdBackupGateProductMismatchError as error:
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="product_driver_mismatch",
                message="Product is not configured for the requested driver route.",
            ) from error
        except ValueError as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request could not be completed.",
            ) from error

        authorization_product = product_profile.product
        if not native_routes.native_driver_route_authorization_allows(
            endpoint=write_odoo_prod_backup_verification,
            authorization_allows=resolved_authz_policy_runtime.policy.allows,
            identity=identity,
            product=authorization_product,
            context=verification_request.backup_verification.context,
            instances=(verification_request.backup_verification.instance,),
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message=(
                    "Workflow cannot execute Odoo prod backup verification"
                    " for the requested product/context/instance."
                ),
            )

        (
            normalized_idempotency_key,
            payload_fingerprint,
            replay_response,
        ) = await replay_apply_idempotency(
            request=request,
            record_store=record_store,
            identity=identity,
            route_path=_ODOO_PROD_BACKUP_VERIFICATION_ROUTE,
            idempotency_key=idempotency_key,
            trace_id=trace_id,
            check_replay=bool(idempotency_key.strip()),
        )
        if replay_response is not None:
            return replay_response

        try:
            records, driver_result = execute_odoo_prod_backup_verification_result(
                control_plane_root=resolved_control_plane_root,
                record_store=record_store,
                request=verification_request,
            )
        except FileNotFoundError as error:
            raise _launchplane_http_error(
                status_code=404,
                trace_id=trace_id,
                code="not_found",
                message=f"No Launchplane route for {_ODOO_PROD_BACKUP_VERIFICATION_ROUTE}.",
            ) from error
        except (ValueError, click.ClickException) as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request could not be completed.",
            ) from error

        response = accepted_evidence_response(
            trace_id=trace_id,
            records={key: string_value(value) for key, value in records.items()},
            result=driver_result,
        )
        if should_store_odoo_prod_backup_verification_idempotency(driver_result):
            store_apply_idempotency(
                record_store=record_store,
                identity=identity,
                route_path=_ODOO_PROD_BACKUP_VERIFICATION_ROUTE,
                idempotency_key=normalized_idempotency_key,
                request_fingerprint_value=payload_fingerprint,
                trace_id=trace_id,
                response=response,
            )
        return response

    async def write_odoo_prod_backup_restore_plan(
        request: Request,
        identity: Annotated[LaunchplaneIdentity, Depends(read_write_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
    ) -> AcceptedEvidenceResponse | JSONResponse:
        trace_id = next_trace_id()
        try:
            raw_payload = await request.json()
        except ValueError as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request payload failed validation.",
            ) from error
        if not isinstance(raw_payload, dict):
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request payload failed validation.",
            )
        try:
            plan_request = OdooProdBackupRestorePlanEnvelope.model_validate(raw_payload)
            lane = resolve_odoo_prod_backup_restore_lane(
                record_store=record_store,
                product=plan_request.product,
                context=plan_request.restore.context,
                instance=plan_request.restore.instance,
            )
        except OdooProdBackupRestoreRouteDependencyError:
            return driver_route_dependency_not_found_response(
                trace_id=trace_id,
                route_path=_ODOO_PROD_BACKUP_RESTORE_PLAN_ROUTE,
            )
        except OdooProdBackupRestoreProductMismatchError as error:
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="product_driver_mismatch",
                message="Product is not configured for the requested driver route.",
            ) from error
        except (ValidationError, ValueError) as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request payload failed validation.",
            ) from error
        if not native_routes.native_driver_route_authorization_allows(
            endpoint=write_odoo_prod_backup_restore_plan,
            authorization_allows=resolved_authz_policy_runtime.policy.allows,
            identity=identity,
            product=plan_request.product,
            context=lane.context,
            instances=(lane.instance,),
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Workflow cannot read the requested Odoo production restore plan.",
            )
        try:
            plan = build_odoo_prod_backup_restore_plan(
                control_plane_root=resolved_control_plane_root,
                record_store=cast(OdooProdBackupRestoreStore, record_store),
                request=plan_request.restore,
            )
        except (ValueError, click.ClickException) as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request could not be completed.",
            ) from error
        return accepted_evidence_response(
            trace_id=trace_id,
            records={
                "backup_record_id": plan.backup_record_id,
                "backup_verification_record_id": plan.verification_record_id,
            },
            result=plan.model_dump(mode="json"),
        )

    async def write_odoo_prod_backup_restore_apply(
        request: Request,
        identity: Annotated[LaunchplaneIdentity, Depends(read_write_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key", include_in_schema=False)
        ] = "",
    ) -> AcceptedEvidenceResponse | JSONResponse:
        trace_id = next_trace_id()
        try:
            raw_payload = await request.json()
        except ValueError as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request payload failed validation.",
            ) from error
        if not isinstance(raw_payload, dict):
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request payload failed validation.",
            )
        try:
            apply_request = OdooProdBackupRestoreApplyEnvelope.model_validate(raw_payload)
            lane = resolve_odoo_prod_backup_restore_lane(
                record_store=record_store,
                product=apply_request.product,
                context=apply_request.restore.context,
                instance=apply_request.restore.instance,
            )
        except OdooProdBackupRestoreRouteDependencyError:
            return driver_route_dependency_not_found_response(
                trace_id=trace_id,
                route_path=_ODOO_PROD_BACKUP_RESTORE_APPLY_ROUTE,
            )
        except OdooProdBackupRestoreProductMismatchError as error:
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="product_driver_mismatch",
                message="Product is not configured for the requested driver route.",
            ) from error
        except (ValidationError, ValueError) as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request payload failed validation.",
            ) from error
        if not native_routes.native_driver_route_authorization_allows(
            endpoint=write_odoo_prod_backup_restore_apply,
            authorization_allows=resolved_authz_policy_runtime.policy.allows,
            identity=identity,
            product=apply_request.product,
            context=lane.context,
            instances=(lane.instance,),
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Workflow cannot apply the requested Odoo production backup restore.",
            )
        normalized_idempotency_key = idempotency_key.strip()
        if not normalized_idempotency_key:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="idempotency_key_required",
                message="Odoo production backup restore requires an Idempotency-Key header.",
            )
        created_at = utc_now_timestamp()
        try:
            operation_authorization = capture_durable_operation_authorization(
                identity=identity,
                action=native_routes.native_driver_route_authz_action(
                    write_odoo_prod_backup_restore_apply
                ),
                product=apply_request.product,
                context=lane.context,
                instances=(lane.instance,),
                policy_record=resolved_authz_policy_runtime.policy_record(updated_at=created_at),
                authorized_at=created_at,
            )
        except DurableOperationAuthorizationCaptureError as error:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="authorization_provenance_unavailable",
                message="Durable operation authorization provenance is unavailable.",
            ) from error
        try:
            records, driver_result = enqueue_odoo_prod_backup_restore_operation(
                control_plane_root=resolved_control_plane_root,
                record_store=record_store,
                request=apply_request,
                idempotency_key=normalized_idempotency_key,
                idempotency_scope=idempotency_scope(identity),
                request_fingerprint=build_request_fingerprint(raw_payload),
                created_at=created_at,
                authorization=operation_authorization,
            )
        except OdooProdBackupRestorePlanChangedError as error:
            raise _launchplane_http_error(
                status_code=409,
                trace_id=trace_id,
                code="odoo_prod_backup_restore_plan_changed",
                message=str(error),
            ) from error
        except OdooProdBackupRestoreIdempotencyKeyReusedError as error:
            raise _launchplane_http_error(
                status_code=409,
                trace_id=trace_id,
                code="idempotency_key_reused",
                message="Idempotency-Key was used for a different production restore request.",
            ) from error
        except OdooProdBackupRestoreLaneBusyError as error:
            raise _launchplane_http_error(
                status_code=409,
                trace_id=trace_id,
                code="odoo_stable_lane_operation_active",
                message=(
                    "Another Odoo stable-lane operation is already active for this "
                    "product/context/instance."
                ),
            ) from error
        except OdooProdBackupRestoreOperationActiveError as error:
            return JSONResponse(
                status_code=409,
                content={
                    "status": "rejected",
                    "trace_id": trace_id,
                    "error": {
                        "code": "odoo_prod_backup_restore_operation_active",
                        "message": (
                            "An Odoo production backup restore operation is already active for "
                            "this lane."
                        ),
                    },
                    "operation": odoo_prod_backup_restore_operation_payload(error.operation),
                },
            )
        except OdooProdBackupRestoreReplayNotEligibleError as error:
            raise _launchplane_http_error(
                status_code=409,
                trace_id=trace_id,
                code="odoo_prod_backup_restore_replay_not_eligible",
                message=str(error),
            ) from error
        except (ValueError, click.ClickException) as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request could not be completed.",
            ) from error
        return accepted_evidence_response(
            trace_id=trace_id,
            records={key: string_value(value) for key, value in records.items()},
            result=driver_result,
        )

    async def write_odoo_prod_retained_volume_backup_import_plan(
        request: Request,
        identity: Annotated[LaunchplaneIdentity, Depends(read_write_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key", include_in_schema=False)
        ] = "",
    ) -> AcceptedEvidenceResponse | JSONResponse:
        trace_id = next_trace_id()
        try:
            raw_payload = await request.json()
        except ValueError as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request payload failed validation.",
            ) from error
        if not isinstance(raw_payload, dict):
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request payload failed validation.",
            )
        try:
            plan_request = OdooProdRetainedVolumeBackupImportPlanEnvelope.model_validate(
                raw_payload
            )
            lane = resolve_odoo_prod_retained_volume_backup_import_lane(
                record_store=record_store,
                product=plan_request.product,
                context=plan_request.backup_import.context,
                instance=plan_request.backup_import.instance,
            )
        except OdooProdRetainedVolumeBackupImportRouteDependencyError:
            return driver_route_dependency_not_found_response(
                trace_id=trace_id,
                route_path=_ODOO_PROD_RETAINED_VOLUME_BACKUP_IMPORT_PLAN_ROUTE,
            )
        except OdooProdRetainedVolumeBackupImportProductMismatchError as error:
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="product_driver_mismatch",
                message="Product is not configured for the requested driver route.",
            ) from error
        except (ValidationError, ValueError) as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request payload failed validation.",
            ) from error
        if not native_routes.native_driver_route_authorization_allows(
            endpoint=write_odoo_prod_retained_volume_backup_import_plan,
            authorization_allows=resolved_authz_policy_runtime.policy.allows,
            identity=identity,
            product=plan_request.product,
            context=lane.context,
            instances=(lane.instance,),
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Workflow cannot plan the requested retained-volume backup import.",
            )
        normalized_idempotency_key = idempotency_key.strip()
        if not normalized_idempotency_key:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="idempotency_key_required",
                message="Retained-volume backup import planning requires an Idempotency-Key header.",
            )
        created_at = utc_now_timestamp()
        try:
            operation_authorization = capture_durable_operation_authorization(
                identity=identity,
                action=native_routes.native_driver_route_authz_action(
                    write_odoo_prod_retained_volume_backup_import_plan
                ),
                product=plan_request.product,
                context=lane.context,
                instances=(lane.instance,),
                policy_record=resolved_authz_policy_runtime.policy_record(updated_at=created_at),
                authorized_at=created_at,
            )
        except DurableOperationAuthorizationCaptureError as error:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="authorization_provenance_unavailable",
                message="Durable operation authorization provenance is unavailable.",
            ) from error
        try:
            records, driver_result = enqueue_odoo_prod_retained_volume_backup_import_plan_operation(
                record_store=record_store,
                request=plan_request,
                idempotency_key=normalized_idempotency_key,
                idempotency_scope=idempotency_scope(identity),
                request_fingerprint=build_request_fingerprint(raw_payload),
                created_at=created_at,
                authorization=operation_authorization,
            )
        except OdooProdRetainedVolumeBackupImportIdempotencyKeyReusedError as error:
            raise _launchplane_http_error(
                status_code=409,
                trace_id=trace_id,
                code="idempotency_key_reused",
                message="Idempotency-Key was used for a different retained-volume plan request.",
            ) from error
        except OdooProdRetainedVolumeBackupImportLaneBusyError as error:
            raise _launchplane_http_error(
                status_code=409,
                trace_id=trace_id,
                code="odoo_stable_lane_operation_active",
                message=(
                    "Another Odoo stable-lane operation is already active for this "
                    "product/context/instance."
                ),
            ) from error
        except OdooProdRetainedVolumeBackupImportOperationActiveError as error:
            return JSONResponse(
                status_code=409,
                content={
                    "status": "rejected",
                    "trace_id": trace_id,
                    "error": {
                        "code": "odoo_retained_volume_backup_import_operation_active",
                        "message": (
                            "An Odoo retained-volume backup import operation is already active for "
                            "this lane."
                        ),
                    },
                    "operation": odoo_prod_retained_volume_backup_import_operation_payload(
                        error.operation
                    ),
                },
            )
        except (ValueError, click.ClickException) as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request could not be completed.",
            ) from error
        return accepted_evidence_response(
            trace_id=trace_id,
            records={key: string_value(value) for key, value in records.items()},
            result=driver_result,
        )

    async def write_odoo_prod_retained_volume_backup_import_apply(
        request: Request,
        identity: Annotated[LaunchplaneIdentity, Depends(read_write_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key", include_in_schema=False)
        ] = "",
    ) -> AcceptedEvidenceResponse | JSONResponse:
        trace_id = next_trace_id()
        try:
            raw_payload = await request.json()
        except ValueError as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request payload failed validation.",
            ) from error
        if not isinstance(raw_payload, dict):
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request payload failed validation.",
            )
        try:
            apply_request = OdooProdRetainedVolumeBackupImportApplyEnvelope.model_validate(
                raw_payload
            )
            lane = resolve_odoo_prod_retained_volume_backup_import_lane(
                record_store=record_store,
                product=apply_request.product,
                context=apply_request.backup_import.context,
                instance=apply_request.backup_import.instance,
            )
        except OdooProdRetainedVolumeBackupImportRouteDependencyError:
            return driver_route_dependency_not_found_response(
                trace_id=trace_id,
                route_path=_ODOO_PROD_RETAINED_VOLUME_BACKUP_IMPORT_APPLY_ROUTE,
            )
        except OdooProdRetainedVolumeBackupImportProductMismatchError as error:
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="product_driver_mismatch",
                message="Product is not configured for the requested driver route.",
            ) from error
        except (ValidationError, ValueError) as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request payload failed validation.",
            ) from error
        if not native_routes.native_driver_route_authorization_allows(
            endpoint=write_odoo_prod_retained_volume_backup_import_apply,
            authorization_allows=resolved_authz_policy_runtime.policy.allows,
            identity=identity,
            product=apply_request.product,
            context=lane.context,
            instances=(lane.instance,),
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Workflow cannot apply the requested retained-volume backup import.",
            )
        normalized_idempotency_key = idempotency_key.strip()
        if not normalized_idempotency_key:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="idempotency_key_required",
                message="Retained-volume backup import apply requires an Idempotency-Key header.",
            )
        created_at = utc_now_timestamp()
        try:
            operation_authorization = capture_durable_operation_authorization(
                identity=identity,
                action=native_routes.native_driver_route_authz_action(
                    write_odoo_prod_retained_volume_backup_import_apply
                ),
                product=apply_request.product,
                context=lane.context,
                instances=(lane.instance,),
                policy_record=resolved_authz_policy_runtime.policy_record(updated_at=created_at),
                authorized_at=created_at,
            )
        except DurableOperationAuthorizationCaptureError as error:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="authorization_provenance_unavailable",
                message="Durable operation authorization provenance is unavailable.",
            ) from error
        try:
            records, driver_result = (
                enqueue_odoo_prod_retained_volume_backup_import_apply_operation(
                    record_store=record_store,
                    request=apply_request,
                    idempotency_key=normalized_idempotency_key,
                    idempotency_scope=idempotency_scope(identity),
                    request_fingerprint=build_request_fingerprint(raw_payload),
                    created_at=created_at,
                    authorization=operation_authorization,
                )
            )
        except OdooProdRetainedVolumeBackupImportPlanChangedError as error:
            raise _launchplane_http_error(
                status_code=409,
                trace_id=trace_id,
                code="odoo_retained_volume_backup_import_plan_changed",
                message=str(error),
            ) from error
        except OdooProdRetainedVolumeBackupImportIdempotencyKeyReusedError as error:
            raise _launchplane_http_error(
                status_code=409,
                trace_id=trace_id,
                code="idempotency_key_reused",
                message="Idempotency-Key was used for a different retained-volume apply request.",
            ) from error
        except OdooProdRetainedVolumeBackupImportLaneBusyError as error:
            raise _launchplane_http_error(
                status_code=409,
                trace_id=trace_id,
                code="odoo_stable_lane_operation_active",
                message=(
                    "Another Odoo stable-lane operation is already active for this "
                    "product/context/instance."
                ),
            ) from error
        except OdooProdRetainedVolumeBackupImportOperationActiveError as error:
            return JSONResponse(
                status_code=409,
                content={
                    "status": "rejected",
                    "trace_id": trace_id,
                    "error": {
                        "code": "odoo_retained_volume_backup_import_operation_active",
                        "message": (
                            "An Odoo retained-volume backup import operation is already active for "
                            "this lane."
                        ),
                    },
                    "operation": odoo_prod_retained_volume_backup_import_operation_payload(
                        error.operation
                    ),
                },
            )
        except (ValueError, click.ClickException) as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request could not be completed.",
            ) from error
        return accepted_evidence_response(
            trace_id=trace_id,
            records={key: string_value(value) for key, value in records.items()},
            result=driver_result,
        )

    def cancel_pending_durable_operation(
        *,
        trace_id: str,
        record_store: object,
        identity: LaunchplaneIdentity,
        operation_id: str,
        action: str,
        read_method_name: str,
        cancel_method_name: str,
        cancellation_request: DurableOperationCancellationRequest,
    ) -> Any:
        read_operation = optional_callable_attribute(record_store, read_method_name)
        cancel_operation = optional_callable_attribute(record_store, cancel_method_name)
        if read_operation is None or cancel_operation is None:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message="Durable operation cancellation requires operation-record storage.",
            )
        try:
            operation = read_operation(operation_id.strip())
        except FileNotFoundError as error:
            raise _launchplane_http_error(
                status_code=404,
                trace_id=trace_id,
                code="not_found",
                message="Durable operation was not found.",
            ) from error
        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action=action,
            product=operation.product,
            context=operation.context,
            target=AuthorizationTarget(
                scope="instance",
                instances=(operation.instance,),
            ),
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Caller cannot cancel this durable operation target.",
            )
        if operation.status == "cancelled":
            return operation
        if operation.status not in {"pending", "reconciliation_required"}:
            raise _launchplane_http_error(
                status_code=409,
                trace_id=trace_id,
                code="operation_not_pending",
                message=(
                    "Only pending durable operations, or reconciliation-required operations after "
                    "provider inspection, can be cancelled safely."
                ),
            )
        cancelled_at = utc_now_timestamp()
        if operation.status == "reconciliation_required":
            reconciliation_attestation = cancellation_request.reconciliation_attestation
            if reconciliation_attestation is None:
                raise _launchplane_http_error(
                    status_code=409,
                    trace_id=trace_id,
                    code="reconciliation_attestation_required",
                    message=(
                        "Reconciliation-required durable operations need an explicit provider "
                        "inspection attestation before cancellation can release the lane."
                    ),
                )
            try:
                provider_inspected_at = parse_utc_timestamp(
                    reconciliation_attestation.provider_inspected_at
                )
                reconciliation_required_at = parse_utc_timestamp(operation.updated_at)
                cancellation_requested_at = parse_utc_timestamp(cancelled_at)
            except ValueError as error:
                raise _launchplane_http_error(
                    status_code=422,
                    trace_id=trace_id,
                    code="invalid_reconciliation_attestation",
                    message="Provider inspection evidence requires valid timestamps.",
                ) from error
            if provider_inspected_at < reconciliation_required_at:
                raise _launchplane_http_error(
                    status_code=409,
                    trace_id=trace_id,
                    code="stale_reconciliation_attestation",
                    message=(
                        "Provider inspection must occur after the operation entered "
                        "reconciliation-required state."
                    ),
                )
            if provider_inspected_at > cancellation_requested_at:
                raise _launchplane_http_error(
                    status_code=422,
                    trace_id=trace_id,
                    code="future_reconciliation_attestation",
                    message="Provider inspection timestamp cannot be in the future.",
                )
        elif cancellation_request.reconciliation_attestation is not None:
            raise _launchplane_http_error(
                status_code=422,
                trace_id=trace_id,
                code="reconciliation_attestation_not_applicable",
                message="Pending operation cancellation does not accept reconciliation evidence.",
            )
        cancelled_operation = type(operation).model_validate(
            {
                **operation.model_dump(mode="json"),
                "status": "cancelled",
                "phase": "cancelled",
                "updated_at": cancelled_at,
                "finished_at": cancelled_at,
                "lease_owner": "",
                "lease_expires_at": "",
                "heartbeat_at": "",
                "result": None,
                "cancellation": build_durable_operation_cancellation(
                    identity=identity,
                    reason=cancellation_request.reason,
                    cancelled_at=cancelled_at,
                    reconciliation_attestation=cancellation_request.reconciliation_attestation,
                ).model_dump(mode="json"),
                "error_code": "",
                "error_message": "",
                "runner_trace_id": trace_id,
            }
        )
        if cancel_operation(cancelled_operation):
            return cancelled_operation
        current_operation = read_operation(operation_id.strip())
        if current_operation.status == "cancelled":
            return current_operation
        raise _launchplane_http_error(
            status_code=409,
            trace_id=trace_id,
            code="operation_not_pending",
            message="The durable operation left cancellable state before cancellation committed.",
        )

    async def cancel_odoo_stable_bootstrap_operation(
        operation_id: str,
        cancellation_request: DurableOperationCancellationRequest,
        identity: Annotated[LaunchplaneIdentity, Depends(read_write_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
    ) -> DurableOperationCancellationResponse:
        trace_id = next_trace_id()
        operation = cancel_pending_durable_operation(
            trace_id=trace_id,
            record_store=record_store,
            identity=identity,
            operation_id=operation_id,
            action="odoo_stable_bootstrap.execute",
            read_method_name="read_odoo_stable_bootstrap_operation_record",
            cancel_method_name="cancel_pending_odoo_stable_bootstrap_operation_record",
            cancellation_request=cancellation_request,
        )
        return DurableOperationCancellationResponse(
            trace_id=trace_id,
            operation=operation.model_dump(mode="json"),
        )

    async def cancel_odoo_target_replacement_operation(
        operation_id: str,
        cancellation_request: DurableOperationCancellationRequest,
        identity: Annotated[LaunchplaneIdentity, Depends(read_write_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
    ) -> DurableOperationCancellationResponse:
        trace_id = next_trace_id()
        operation = cancel_pending_durable_operation(
            trace_id=trace_id,
            record_store=record_store,
            identity=identity,
            operation_id=operation_id,
            action="odoo_target_replacement_apply.execute",
            read_method_name="read_odoo_stable_target_replacement_operation_record",
            cancel_method_name="cancel_pending_odoo_stable_target_replacement_operation_record",
            cancellation_request=cancellation_request,
        )
        return DurableOperationCancellationResponse(
            trace_id=trace_id,
            operation=operation.model_dump(mode="json"),
        )

    async def cancel_odoo_prod_backup_restore_operation(
        operation_id: str,
        cancellation_request: DurableOperationCancellationRequest,
        identity: Annotated[LaunchplaneIdentity, Depends(read_write_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
    ) -> DurableOperationCancellationResponse:
        trace_id = next_trace_id()
        operation = cancel_pending_durable_operation(
            trace_id=trace_id,
            record_store=record_store,
            identity=identity,
            operation_id=operation_id,
            action="odoo_prod_backup_restore_apply.execute",
            read_method_name="read_odoo_prod_backup_restore_operation_record",
            cancel_method_name="cancel_pending_odoo_prod_backup_restore_operation_record",
            cancellation_request=cancellation_request,
        )
        return DurableOperationCancellationResponse(
            trace_id=trace_id,
            operation=operation.model_dump(mode="json"),
        )

    async def cancel_odoo_prod_retained_volume_backup_import_operation(
        operation_id: str,
        cancellation_request: DurableOperationCancellationRequest,
        identity: Annotated[LaunchplaneIdentity, Depends(read_write_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
    ) -> DurableOperationCancellationResponse:
        trace_id = next_trace_id()
        read_operation = optional_callable_attribute(
            record_store,
            "read_odoo_prod_retained_volume_backup_import_operation_record",
        )
        if read_operation is None:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message="Durable operation cancellation requires database-backed storage.",
            )
        try:
            existing_operation = read_operation(operation_id.strip())
        except FileNotFoundError as error:
            raise _launchplane_http_error(
                status_code=404,
                trace_id=trace_id,
                code="not_found",
                message="Durable operation was not found.",
            ) from error
        action = (
            "odoo_prod_retained_volume_backup_import_plan.execute"
            if existing_operation.operation_kind == "plan"
            else "odoo_prod_retained_volume_backup_import_apply.execute"
        )
        operation = cancel_pending_durable_operation(
            trace_id=trace_id,
            record_store=record_store,
            identity=identity,
            operation_id=operation_id,
            action=action,
            read_method_name="read_odoo_prod_retained_volume_backup_import_operation_record",
            cancel_method_name=(
                "cancel_pending_odoo_prod_retained_volume_backup_import_operation_record"
            ),
            cancellation_request=cancellation_request,
        )
        return DurableOperationCancellationResponse(
            trace_id=trace_id,
            operation=operation.model_dump(mode="json"),
        )

    async def cancel_verireel_prod_backup_gate_operation(
        operation_id: str,
        cancellation_request: DurableOperationCancellationRequest,
        identity: Annotated[LaunchplaneIdentity, Depends(read_write_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
    ) -> DurableOperationCancellationResponse:
        trace_id = next_trace_id()
        operation = cancel_pending_durable_operation(
            trace_id=trace_id,
            record_store=record_store,
            identity=identity,
            operation_id=operation_id,
            action="verireel_prod_backup_gate.execute",
            read_method_name="read_verireel_prod_backup_gate_operation_record",
            cancel_method_name="cancel_pending_verireel_prod_backup_gate_operation_record",
            cancellation_request=cancellation_request,
        )
        return DurableOperationCancellationResponse(
            trace_id=trace_id,
            operation=operation.model_dump(mode="json"),
        )

    async def write_odoo_stable_bootstrap(
        request: Request,
        identity: Annotated[LaunchplaneIdentity, Depends(read_write_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key", include_in_schema=False)
        ] = "",
    ) -> AcceptedEvidenceResponse | JSONResponse:
        trace_id = next_trace_id()
        try:
            raw_payload = await request.json()
        except ValueError as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request payload failed validation.",
            ) from error
        if not isinstance(raw_payload, dict):
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request payload failed validation.",
            )
        try:
            bootstrap_request = OdooStableBootstrapEnvelope.model_validate(raw_payload)
        except ValidationError as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request payload failed validation.",
            ) from error
        try:
            resolve_odoo_stable_bootstrap_product_route(
                record_store=record_store,
                product=bootstrap_request.product,
                context=bootstrap_request.bootstrap.context,
                instance=bootstrap_request.bootstrap.instance,
            )
        except OdooStableBootstrapRouteDependencyError:
            return driver_route_dependency_not_found_response(
                trace_id=trace_id,
                route_path=_ODOO_STABLE_BOOTSTRAP_ROUTE,
            )
        except OdooStableBootstrapProductMismatchError as error:
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="product_driver_mismatch",
                message="Product is not configured for the requested driver route.",
            ) from error
        except ValueError as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request could not be completed.",
            ) from error

        if not native_routes.native_driver_route_authorization_allows(
            endpoint=write_odoo_stable_bootstrap,
            authorization_allows=resolved_authz_policy_runtime.policy.allows,
            identity=identity,
            product=bootstrap_request.product,
            context=bootstrap_request.bootstrap.context,
            instances=(bootstrap_request.bootstrap.instance,),
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message=(
                    "Workflow cannot execute Odoo stable bootstrap for the"
                    " requested product/context."
                ),
            )

        normalized_idempotency_key = idempotency_key.strip()
        if not normalized_idempotency_key:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="idempotency_key_required",
                message="Odoo stable bootstrap operations require an Idempotency-Key header.",
            )
        created_at = utc_now_timestamp()
        try:
            operation_authorization = capture_durable_operation_authorization(
                identity=identity,
                action=native_routes.native_driver_route_authz_action(write_odoo_stable_bootstrap),
                product=bootstrap_request.product,
                context=bootstrap_request.bootstrap.context,
                instances=(bootstrap_request.bootstrap.instance,),
                policy_record=resolved_authz_policy_runtime.policy_record(updated_at=created_at),
                authorized_at=created_at,
            )
        except DurableOperationAuthorizationCaptureError as error:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="authorization_provenance_unavailable",
                message="Durable operation authorization provenance is unavailable.",
            ) from error
        try:
            records, driver_result = enqueue_odoo_stable_bootstrap_operation(
                record_store=record_store,
                request=bootstrap_request,
                idempotency_key=normalized_idempotency_key,
                request_fingerprint=build_request_fingerprint(raw_payload),
                created_at=created_at,
                authorization=operation_authorization,
            )
        except OdooStableBootstrapIdempotencyKeyReusedError as error:
            raise _launchplane_http_error(
                status_code=409,
                trace_id=trace_id,
                code="idempotency_key_reused",
                message=(
                    "Idempotency-Key was already used for a different Odoo stable"
                    " bootstrap request."
                ),
            ) from error
        except OdooStableBootstrapLaneBusyError as error:
            raise _launchplane_http_error(
                status_code=409,
                trace_id=trace_id,
                code="odoo_stable_lane_operation_active",
                message=str(error),
            ) from error
        except OdooStableBootstrapOperationActiveError as error:
            return JSONResponse(
                status_code=409,
                content={
                    "status": "rejected",
                    "trace_id": trace_id,
                    "error": {
                        "code": "odoo_stable_bootstrap_operation_active",
                        "message": (
                            "An Odoo stable bootstrap operation is already active for"
                            " this product/context/instance."
                        ),
                    },
                    "operation": odoo_stable_bootstrap_operation_payload(error.operation),
                },
            )
        except (ValueError, click.ClickException) as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request could not be completed.",
            ) from error

        return accepted_evidence_response(
            trace_id=trace_id,
            records={key: string_value(value) for key, value in records.items()},
            result=driver_result,
        )

    async def write_odoo_target_replacement_plan(
        request: Request,
        identity: Annotated[LaunchplaneIdentity, Depends(read_write_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
    ) -> AcceptedEvidenceResponse | JSONResponse:
        trace_id = next_trace_id()
        try:
            raw_payload = await request.json()
        except ValueError as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request payload failed validation.",
            ) from error
        if not isinstance(raw_payload, dict):
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request payload failed validation.",
            )
        try:
            plan_request = OdooTargetReplacementPlanEnvelope.model_validate(raw_payload)
        except ValidationError as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request payload failed validation.",
            ) from error

        try:
            lane = resolve_odoo_target_replacement_plan_lane(
                record_store=record_store,
                product=plan_request.product,
                instance=plan_request.replacement.instance,
            )
        except OdooTargetReplacementPlanRouteDependencyError:
            return driver_route_dependency_not_found_response(
                trace_id=trace_id,
                route_path=_ODOO_TARGET_REPLACEMENT_PLAN_ROUTE,
            )
        except OdooTargetReplacementPlanProductMismatchError as error:
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="product_driver_mismatch",
                message="Product is not configured for the requested driver route.",
            ) from error
        except ValueError as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request could not be completed.",
            ) from error

        if not native_routes.native_driver_route_authorization_allows(
            endpoint=write_odoo_target_replacement_plan,
            authorization_allows=resolved_authz_policy_runtime.policy.allows,
            identity=identity,
            product=plan_request.product,
            context=lane.context,
            instances=(lane.instance,),
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message=(
                    "Workflow cannot read the Odoo target replacement plan for"
                    " the requested product/context."
                ),
            )

        try:
            driver_result = build_odoo_stable_target_replacement_plan(
                control_plane_root=resolved_control_plane_root,
                record_store=cast(OdooStableTargetReplacementStore, record_store),
                request=plan_request.replacement,
            )
        except (ValueError, click.ClickException) as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request could not be completed.",
            ) from error

        return accepted_evidence_response(
            trace_id=trace_id,
            records={},
            result=driver_result.model_dump(mode="json"),
        )

    async def write_odoo_target_replacement_apply(
        request: Request,
        identity: Annotated[LaunchplaneIdentity, Depends(read_write_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key", include_in_schema=False)
        ] = "",
    ) -> AcceptedEvidenceResponse | JSONResponse:
        trace_id = next_trace_id()
        try:
            raw_payload = await request.json()
        except ValueError as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request payload failed validation.",
            ) from error
        if not isinstance(raw_payload, dict):
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request payload failed validation.",
            )
        try:
            apply_request = OdooTargetReplacementApplyEnvelope.model_validate(raw_payload)
        except ValidationError as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request payload failed validation.",
            ) from error

        try:
            lane = resolve_odoo_target_replacement_apply_lane(
                record_store=record_store,
                product=apply_request.product,
                instance=apply_request.replacement.instance,
            )
        except OdooTargetReplacementApplyRouteDependencyError:
            return driver_route_dependency_not_found_response(
                trace_id=trace_id,
                route_path=_ODOO_TARGET_REPLACEMENT_APPLY_ROUTE,
            )
        except OdooTargetReplacementApplyProductMismatchError as error:
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="product_driver_mismatch",
                message="Product is not configured for the requested driver route.",
            ) from error
        except ValueError as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request could not be completed.",
            ) from error

        if not native_routes.native_driver_route_authorization_allows(
            endpoint=write_odoo_target_replacement_apply,
            authorization_allows=resolved_authz_policy_runtime.policy.allows,
            identity=identity,
            product=apply_request.product,
            context=lane.context,
            instances=(lane.instance,),
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message=(
                    "Workflow cannot apply Odoo target replacement for"
                    " the requested product/context."
                ),
            )

        normalized_idempotency_key = idempotency_key.strip()
        if not normalized_idempotency_key:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="idempotency_key_required",
                message="Odoo target replacement operations require an Idempotency-Key header.",
            )
        created_at = utc_now_timestamp()
        try:
            operation_authorization = capture_durable_operation_authorization(
                identity=identity,
                action=native_routes.native_driver_route_authz_action(
                    write_odoo_target_replacement_apply
                ),
                product=apply_request.product,
                context=lane.context,
                instances=(lane.instance,),
                policy_record=resolved_authz_policy_runtime.policy_record(updated_at=created_at),
                authorized_at=created_at,
            )
        except DurableOperationAuthorizationCaptureError as error:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="authorization_provenance_unavailable",
                message="Durable operation authorization provenance is unavailable.",
            ) from error
        try:
            records, driver_result = enqueue_odoo_target_replacement_apply_operation(
                record_store=record_store,
                request=apply_request,
                context=lane.context,
                idempotency_key=normalized_idempotency_key,
                idempotency_scope=idempotency_scope(identity),
                request_fingerprint=build_request_fingerprint(raw_payload),
                created_at=created_at,
                authorization=operation_authorization,
            )
        except OdooTargetReplacementApplyCurrentArtifactChangedError as error:
            raise _launchplane_http_error(
                status_code=409,
                trace_id=trace_id,
                code="odoo_target_replacement_current_artifact_changed",
                message=str(error),
            ) from error
        except OdooTargetReplacementApplyIdempotencyKeyReusedError as error:
            raise _launchplane_http_error(
                status_code=409,
                trace_id=trace_id,
                code="idempotency_key_reused",
                message=(
                    "Idempotency-Key was already used for a different Odoo"
                    " target replacement request."
                ),
            ) from error
        except OdooTargetReplacementApplyLaneBusyError as error:
            raise _launchplane_http_error(
                status_code=409,
                trace_id=trace_id,
                code="odoo_stable_lane_operation_active",
                message=str(error),
            ) from error
        except OdooTargetReplacementApplyOperationActiveError as error:
            return JSONResponse(
                status_code=409,
                content={
                    "status": "rejected",
                    "trace_id": trace_id,
                    "error": {
                        "code": "odoo_stable_target_replacement_operation_active",
                        "message": (
                            "An Odoo target replacement operation is already active"
                            " for this product/context/instance."
                        ),
                    },
                    "operation": odoo_target_replacement_apply_operation_payload(error.operation),
                },
            )
        except (ValueError, click.ClickException) as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request could not be completed.",
            ) from error

        return accepted_evidence_response(
            trace_id=trace_id,
            records={key: string_value(value) for key, value in records.items()},
            result=driver_result,
        )

    async def write_odoo_prod_rollback(
        request: Request,
        identity: Annotated[LaunchplaneIdentity, Depends(read_write_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> AcceptedEvidenceResponse | JSONResponse:
        trace_id = next_trace_id()
        try:
            raw_payload = await request.json()
        except ValueError as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request payload failed validation.",
            ) from error
        if not isinstance(raw_payload, dict):
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request payload failed validation.",
            )
        try:
            rollback_request = OdooProdRollbackEnvelope.model_validate(raw_payload)
        except ValidationError as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request payload failed validation.",
            ) from error
        try:
            product_profile = resolve_odoo_prod_rollback_product_route(
                record_store=record_store,
                product=rollback_request.product,
                context=rollback_request.rollback.context,
                instance=rollback_request.rollback.instance,
            )
        except OdooProdRollbackRouteDependencyError:
            return driver_route_dependency_not_found_response(
                trace_id=trace_id,
                route_path=_ODOO_PROD_ROLLBACK_ROUTE,
            )
        except OdooProdRollbackProductMismatchError as error:
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="product_driver_mismatch",
                message="Product is not configured for the requested driver route.",
            ) from error
        except ValueError as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request could not be completed.",
            ) from error

        authorization_product = product_profile.product
        if not native_routes.native_driver_route_authorization_allows(
            endpoint=write_odoo_prod_rollback,
            authorization_allows=resolved_authz_policy_runtime.policy.allows,
            identity=identity,
            product=authorization_product,
            context=rollback_request.rollback.context,
            instances=(rollback_request.rollback.instance,),
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message=(
                    "Workflow cannot execute the Odoo prod rollback driver"
                    " for the requested product/context."
                ),
            )

        (
            normalized_idempotency_key,
            payload_fingerprint,
            replay_response,
        ) = await replay_apply_idempotency(
            request=request,
            record_store=record_store,
            identity=identity,
            route_path=_ODOO_PROD_ROLLBACK_ROUTE,
            idempotency_key=idempotency_key,
            trace_id=trace_id,
            check_replay=bool(idempotency_key.strip()),
        )
        if replay_response is not None:
            return replay_response

        try:
            records, driver_result = execute_odoo_prod_rollback_result(
                control_plane_root=resolved_control_plane_root,
                record_store=record_store,
                request=rollback_request,
            )
        except FileNotFoundError as error:
            raise _launchplane_http_error(
                status_code=404,
                trace_id=trace_id,
                code="not_found",
                message=f"No Launchplane route for {_ODOO_PROD_ROLLBACK_ROUTE}.",
            ) from error
        except (ValueError, click.ClickException) as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request could not be completed.",
            ) from error

        response = accepted_evidence_response(
            trace_id=trace_id,
            records={key: string_value(value) for key, value in records.items()},
            result=driver_result,
        )
        if should_store_odoo_prod_rollback_idempotency(driver_result):
            store_apply_idempotency(
                record_store=record_store,
                identity=identity,
                route_path=_ODOO_PROD_ROLLBACK_ROUTE,
                idempotency_key=normalized_idempotency_key,
                request_fingerprint_value=payload_fingerprint,
                trace_id=trace_id,
                response=response,
            )
        return response

    async def write_odoo_prod_promotion(
        request: Request,
        identity: Annotated[LaunchplaneIdentity, Depends(read_write_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> AcceptedEvidenceResponse | JSONResponse:
        trace_id = next_trace_id()
        try:
            raw_payload = await request.json()
        except ValueError as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request payload failed validation.",
            ) from error
        if not isinstance(raw_payload, dict):
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request payload failed validation.",
            )
        try:
            promotion_request = OdooProdPromotionEnvelope.model_validate(raw_payload)
        except ValidationError as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request payload failed validation.",
            ) from error
        try:
            product_profile = resolve_odoo_prod_promotion_product_route(
                record_store=record_store,
                product=promotion_request.product,
                context=promotion_request.promotion.context,
                instances=(
                    promotion_request.promotion.from_instance,
                    promotion_request.promotion.to_instance,
                ),
            )
        except OdooProdPromotionRouteDependencyError:
            return driver_route_dependency_not_found_response(
                trace_id=trace_id,
                route_path=_ODOO_PROD_PROMOTION_ROUTE,
            )
        except OdooProdPromotionProductMismatchError as error:
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="product_driver_mismatch",
                message="Product is not configured for the requested driver route.",
            ) from error
        except ValueError as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request could not be completed.",
            ) from error

        authorization_product = product_profile.product
        if not native_routes.native_driver_route_authorization_allows(
            endpoint=write_odoo_prod_promotion,
            authorization_allows=resolved_authz_policy_runtime.policy.allows,
            identity=identity,
            product=authorization_product,
            context=promotion_request.promotion.context,
            instances=(
                promotion_request.promotion.from_instance,
                promotion_request.promotion.to_instance,
            ),
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message=(
                    "Workflow cannot execute the Odoo prod promotion driver for the"
                    " requested product/context."
                ),
            )

        (
            normalized_idempotency_key,
            payload_fingerprint,
            replay_response,
        ) = await replay_apply_idempotency(
            request=request,
            record_store=record_store,
            identity=identity,
            route_path=_ODOO_PROD_PROMOTION_ROUTE,
            idempotency_key=idempotency_key,
            trace_id=trace_id,
            check_replay=bool(idempotency_key.strip()),
        )
        if replay_response is not None:
            return replay_response

        try:
            records, driver_result = execute_odoo_prod_promotion_result(
                control_plane_root=resolved_control_plane_root,
                state_dir=resolved_state_dir,
                database_url=getattr(record_store, "database_url", database_url),
                record_store=record_store,
                request=promotion_request,
            )
        except FileNotFoundError as error:
            raise _launchplane_http_error(
                status_code=404,
                trace_id=trace_id,
                code="not_found",
                message=f"No Launchplane route for {_ODOO_PROD_PROMOTION_ROUTE}.",
            ) from error
        except (ValueError, click.ClickException) as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request could not be completed.",
            ) from error

        response = accepted_evidence_response(
            trace_id=trace_id,
            records={key: string_value(value) for key, value in records.items()},
            result=driver_result,
        )
        if should_store_prod_promotion_idempotency(driver_result):
            store_apply_idempotency(
                record_store=record_store,
                identity=identity,
                route_path=_ODOO_PROD_PROMOTION_ROUTE,
                idempotency_key=normalized_idempotency_key,
                request_fingerprint_value=payload_fingerprint,
                trace_id=trace_id,
                response=response,
            )
        return response

    async def write_odoo_prod_promotion_inputs(
        request: Request,
        identity: Annotated[LaunchplaneIdentity, Depends(read_write_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> AcceptedEvidenceResponse | JSONResponse:
        trace_id = next_trace_id()
        try:
            raw_payload = await request.json()
        except ValueError as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request payload failed validation.",
            ) from error
        if not isinstance(raw_payload, dict):
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request payload failed validation.",
            )
        try:
            inputs_request = OdooProdPromotionInputsEnvelope.model_validate(raw_payload)
        except ValidationError as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request payload failed validation.",
            ) from error
        try:
            product_profile = resolve_odoo_prod_promotion_product_route(
                record_store=record_store,
                product=inputs_request.product,
                context=inputs_request.inputs.context,
                instances=(
                    inputs_request.inputs.from_instance,
                    inputs_request.inputs.to_instance,
                ),
            )
        except OdooProdPromotionRouteDependencyError:
            return driver_route_dependency_not_found_response(
                trace_id=trace_id,
                route_path=_ODOO_PROD_PROMOTION_INPUTS_ROUTE,
            )
        except OdooProdPromotionProductMismatchError as error:
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="product_driver_mismatch",
                message="Product is not configured for the requested driver route.",
            ) from error
        except ValueError as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request could not be completed.",
            ) from error

        authorization_product = product_profile.product
        if not native_routes.native_driver_route_authorization_allows(
            endpoint=write_odoo_prod_promotion_inputs,
            authorization_allows=resolved_authz_policy_runtime.policy.allows,
            identity=identity,
            product=authorization_product,
            context=inputs_request.inputs.context,
            instances=(
                inputs_request.inputs.from_instance,
                inputs_request.inputs.to_instance,
            ),
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message=(
                    "Workflow cannot read Odoo prod promotion inputs for the requested"
                    " product/context."
                ),
            )

        (
            normalized_idempotency_key,
            payload_fingerprint,
            replay_response,
        ) = await replay_apply_idempotency(
            request=request,
            record_store=record_store,
            identity=identity,
            route_path=_ODOO_PROD_PROMOTION_INPUTS_ROUTE,
            idempotency_key=idempotency_key,
            trace_id=trace_id,
            check_replay=bool(idempotency_key.strip()),
        )
        if replay_response is not None:
            return replay_response

        try:
            records, driver_result = resolve_odoo_prod_promotion_inputs_result(
                record_store=record_store,
                request=inputs_request,
            )
        except OdooProdPromotionRouteDependencyError:
            return driver_route_dependency_not_found_response(
                trace_id=trace_id,
                route_path=_ODOO_PROD_PROMOTION_INPUTS_ROUTE,
            )
        except FileNotFoundError as error:
            raise _launchplane_http_error(
                status_code=404,
                trace_id=trace_id,
                code="not_found",
                message=f"No Launchplane route for {_ODOO_PROD_PROMOTION_INPUTS_ROUTE}.",
            ) from error
        except (ValueError, click.ClickException) as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request could not be completed.",
            ) from error

        response = accepted_evidence_response(
            trace_id=trace_id,
            records={key: string_value(value) for key, value in records.items()},
            result=driver_result,
        )
        if should_store_prod_promotion_idempotency(driver_result):
            store_apply_idempotency(
                record_store=record_store,
                identity=identity,
                route_path=_ODOO_PROD_PROMOTION_INPUTS_ROUTE,
                idempotency_key=normalized_idempotency_key,
                request_fingerprint_value=payload_fingerprint,
                trace_id=trace_id,
                response=response,
            )
        return response

    async def write_odoo_prod_promotion_run(
        request: Request,
        identity: Annotated[LaunchplaneIdentity, Depends(read_write_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> AcceptedEvidenceResponse | JSONResponse:
        trace_id = next_trace_id()
        try:
            raw_payload = await request.json()
        except ValueError as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request payload failed validation.",
            ) from error
        if not isinstance(raw_payload, dict):
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request payload failed validation.",
            )
        try:
            run_request = OdooProdPromotionRunEnvelope.model_validate(raw_payload)
        except ValidationError as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request payload failed validation.",
            ) from error
        try:
            product_profile = resolve_odoo_prod_promotion_product_route(
                record_store=record_store,
                product=run_request.product,
                context=run_request.run.context,
                instances=(
                    run_request.run.from_instance,
                    run_request.run.to_instance,
                ),
            )
        except OdooProdPromotionRouteDependencyError:
            return driver_route_dependency_not_found_response(
                trace_id=trace_id,
                route_path=_ODOO_PROD_PROMOTION_RUN_ROUTE,
            )
        except OdooProdPromotionProductMismatchError as error:
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="product_driver_mismatch",
                message="Product is not configured for the requested driver route.",
            ) from error
        except ValueError as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request could not be completed.",
            ) from error

        authorization_product = product_profile.product
        if not native_routes.native_driver_route_authorization_allows(
            endpoint=write_odoo_prod_promotion_run,
            authorization_allows=resolved_authz_policy_runtime.policy.allows,
            identity=identity,
            product=authorization_product,
            context=run_request.run.context,
            instances=(run_request.run.from_instance, run_request.run.to_instance),
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message=(
                    "Workflow cannot execute the Odoo prod promotion run for the requested"
                    " product/context."
                ),
            )

        (
            normalized_idempotency_key,
            payload_fingerprint,
            replay_response,
        ) = await replay_apply_idempotency(
            request=request,
            record_store=record_store,
            identity=identity,
            route_path=_ODOO_PROD_PROMOTION_RUN_ROUTE,
            idempotency_key=idempotency_key,
            trace_id=trace_id,
            check_replay=bool(idempotency_key.strip()),
        )
        if replay_response is not None:
            return replay_response

        try:
            records, driver_result = execute_odoo_prod_promotion_run_result(
                control_plane_root=resolved_control_plane_root,
                state_dir=resolved_state_dir,
                database_url=getattr(record_store, "database_url", database_url),
                record_store=record_store,
                request=run_request,
            )
        except OdooProdPromotionRouteDependencyError:
            return driver_route_dependency_not_found_response(
                trace_id=trace_id,
                route_path=_ODOO_PROD_PROMOTION_RUN_ROUTE,
            )
        except FileNotFoundError as error:
            raise _launchplane_http_error(
                status_code=404,
                trace_id=trace_id,
                code="not_found",
                message=f"No Launchplane route for {_ODOO_PROD_PROMOTION_RUN_ROUTE}.",
            ) from error
        except (ValueError, click.ClickException) as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request could not be completed.",
            ) from error

        response = accepted_evidence_response(
            trace_id=trace_id,
            records={key: string_value(value) for key, value in records.items()},
            result=driver_result,
        )
        if should_store_prod_promotion_idempotency(driver_result):
            store_apply_idempotency(
                record_store=record_store,
                identity=identity,
                route_path=_ODOO_PROD_PROMOTION_RUN_ROUTE,
                idempotency_key=normalized_idempotency_key,
                request_fingerprint_value=payload_fingerprint,
                trace_id=trace_id,
                response=response,
            )
        return response

    async def write_launchplane_self_deploy(
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
                message="Request payload failed validation.",
            ) from error
        if not isinstance(raw_payload, dict):
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request payload failed validation.",
            )
        try:
            self_deploy_request = LaunchplaneSelfDeployEnvelope.model_validate(raw_payload)
        except ValidationError as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request payload failed validation.",
            ) from error

        normalized_idempotency_key = idempotency_key.strip()
        payload_fingerprint = build_request_fingerprint(raw_payload)
        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action="launchplane_service_deploy.execute",
            product=self_deploy_request.product,
            context=_LAUNCHPLANE_SERVICE_CONTEXT,
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Workflow cannot execute Launchplane self deploy.",
            )

        if normalized_idempotency_key:
            (
                normalized_idempotency_key,
                payload_fingerprint,
                replay_response,
            ) = await replay_apply_idempotency(
                request=request,
                record_store=record_store,
                identity=identity,
                route_path=_LAUNCHPLANE_SELF_DEPLOY_ROUTE,
                idempotency_key=normalized_idempotency_key,
                trace_id=trace_id,
                check_replay=True,
            )
            if replay_response is not None:
                return replay_response

        try:
            driver_result = execute_launchplane_self_deploy(
                control_plane_root_path=resolved_control_plane_root,
                request=self_deploy_request.deploy,
            )
        except (ValueError, click.ClickException) as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request could not be completed.",
            ) from error

        response = accepted_evidence_response(
            trace_id=trace_id,
            records=launchplane_self_deploy_records(driver_result),
            result=driver_result.model_dump(mode="json"),
        )
        store_apply_idempotency(
            record_store=record_store,
            identity=identity,
            route_path=_LAUNCHPLANE_SELF_DEPLOY_ROUTE,
            idempotency_key=normalized_idempotency_key,
            request_fingerprint_value=payload_fingerprint,
            trace_id=trace_id,
            response=response,
        )
        return response

    async def write_merge_train_stack_collapse_run_once(
        request: Request,
        identity: Annotated[LaunchplaneIdentity, Depends(read_write_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> AcceptedEvidenceResponse | JSONResponse:
        trace_id = next_trace_id()
        try:
            raw_payload = await request.json()
        except ValueError as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request payload failed validation.",
            ) from error
        if not isinstance(raw_payload, dict):
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request payload failed validation.",
            )
        try:
            stack_request = MergeTrainStackCollapseRunOnceEnvelope.model_validate(raw_payload)
        except ValidationError as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request payload failed validation.",
            ) from error

        normalized_idempotency_key = idempotency_key.strip()
        payload_fingerprint = build_request_fingerprint(raw_payload)
        if normalized_idempotency_key:
            (
                normalized_idempotency_key,
                payload_fingerprint,
                replay_response,
            ) = await replay_apply_idempotency(
                request=request,
                record_store=record_store,
                identity=identity,
                route_path=_MERGE_TRAIN_STACK_COLLAPSE_RUN_ONCE_ROUTE,
                idempotency_key=normalized_idempotency_key,
                trace_id=trace_id,
                check_replay=True,
            )
            if replay_response is not None:
                return replay_response

        try:
            policy_record = resolve_merge_train_policy_record(record_store)
        except MergeTrainPolicyStoreMissingError as error:
            raise merge_train_policy_not_configured_error(trace_id=trace_id, error=error) from error
        try:
            repository_policy = policy_record.policy.find_repository_policy(
                repository=stack_request.repository,
                base_branch=stack_request.base_branch,
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
                message="Workflow cannot run the requested merge train policy.",
            )
        token_env = repository_policy.github_token.env_var
        if not token_env:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="github_token_not_configured",
                message="Merge train policy does not define a GitHub token environment variable.",
            )
        token = os.environ.get(token_env, "").strip()
        if not token:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="github_token_not_configured",
                message="Configured merge train GitHub token is not available.",
            )
        try:
            stack_collapse_store = require_merge_train_stack_collapse_plan_record_store(
                record_store
            )
        except TypeError as error:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message="Merge train stack collapse storage requires database-backed records.",
            ) from error
        try:
            batch_candidate_store = (
                require_merge_train_batch_candidate_record_store(record_store)
                if stack_request.mode == "admit"
                else None
            )
        except TypeError as error:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message="Merge train stack collapse admission requires database-backed candidate records.",
            ) from error
        try:
            controller_state_store = require_merge_train_controller_state_record_store(record_store)
        except TypeError as error:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message="Merge train controller storage requires database-backed records.",
            ) from error
        try:
            with merge_train_controller_mutation_fence(
                record_store=controller_state_store,
                repository=stack_request.repository,
                base_branch=stack_request.base_branch,
                policy_key=repository_policy.policy_key,
                policy_sha256=policy_record.policy_sha256,
                trace_id=trace_id,
                active_action="stack_collapse_run_once",
                active_phase=stack_request.mode,
                active_record_id=stack_request.stack_collapse_plan_record_id,
            ) as lease:

                def checkpoint_stack_mutation(phase: str, pull_request_number: int | None) -> None:
                    lease.checkpoint(
                        active_action="stack_collapse_run_once",
                        active_phase=phase,
                        active_record_id=stack_request.stack_collapse_plan_record_id,
                        active_pull_request_number=pull_request_number,
                        step_payload={"mode": stack_request.mode},
                    )

                stack_result = execute_merge_train_stack_collapse_run_once(
                    request=stack_request,
                    policy=policy_record.policy,
                    policy_sha256=policy_record.policy_sha256,
                    token=token,
                    trace_id=trace_id,
                    recorded_at=lease.record.updated_at,
                    stack_collapse_store=stack_collapse_store,
                    batch_candidate_store=batch_candidate_store,
                    mutation_checkpoint=checkpoint_stack_mutation,
                )
                response = accepted_evidence_response(
                    trace_id=trace_id,
                    records=stack_result.records,
                    result=stack_result.accepted_result,
                )
                store_apply_idempotency(
                    record_store=record_store,
                    identity=identity,
                    route_path=_MERGE_TRAIN_STACK_COLLAPSE_RUN_ONCE_ROUTE,
                    idempotency_key=normalized_idempotency_key,
                    request_fingerprint_value=payload_fingerprint,
                    trace_id=trace_id,
                    response=response,
                )
        except MergeTrainGitHubStaleHeadError as error:
            return merge_train_github_stale_state_response(trace_id=trace_id, error=error)
        except MergeTrainGitHubError as error:
            return merge_train_github_request_failed_response(trace_id=trace_id, error=error)
        except (
            MergeTrainControllerLeaseHeldError,
            MergeTrainControllerLeaseLostError,
            MergeTrainControllerReconciliationRequiredError,
            MergeTrainControllerAdoptionRejectedError,
        ) as error:
            raise merge_train_controller_fence_http_error(trace_id=trace_id, error=error) from error
        except MergeTrainStackCollapseBatchCandidateStoreMissingError as error:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message="Merge train stack collapse admission requires database-backed candidate records.",
            ) from error
        except MergeTrainStackCollapsePlanRecordNotFoundError as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request could not be completed.",
            ) from error
        except ValueError as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request could not be completed.",
            ) from error
        return response

    async def write_merge_train_batch_landing_run_once(
        request: Request,
        identity: Annotated[LaunchplaneIdentity, Depends(read_write_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> AcceptedEvidenceResponse | JSONResponse:
        trace_id = next_trace_id()
        try:
            raw_payload = await request.json()
        except ValueError as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request payload failed validation.",
            ) from error
        if not isinstance(raw_payload, dict):
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request payload failed validation.",
            )
        try:
            landing_request = MergeTrainBatchLandingRunOnceEnvelope.model_validate(raw_payload)
        except ValidationError as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request payload failed validation.",
            ) from error

        normalized_idempotency_key = idempotency_key.strip()
        payload_fingerprint = build_request_fingerprint(raw_payload)
        if normalized_idempotency_key:
            (
                normalized_idempotency_key,
                payload_fingerprint,
                replay_response,
            ) = await replay_apply_idempotency(
                request=request,
                record_store=record_store,
                identity=identity,
                route_path=_MERGE_TRAIN_BATCH_LANDING_RUN_ONCE_ROUTE,
                idempotency_key=normalized_idempotency_key,
                trace_id=trace_id,
                check_replay=True,
            )
            if replay_response is not None:
                return replay_response

        try:
            policy_record = resolve_merge_train_policy_record(record_store)
        except MergeTrainPolicyStoreMissingError as error:
            raise merge_train_policy_not_configured_error(trace_id=trace_id, error=error) from error
        try:
            repository_policy = policy_record.policy.find_repository_policy(
                repository=landing_request.repository,
                base_branch=landing_request.base_branch,
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
                message="Workflow cannot run the requested merge train policy.",
            )
        token_env = repository_policy.github_token.env_var
        if not token_env:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="github_token_not_configured",
                message="Merge train policy does not define a GitHub token environment variable.",
            )
        token = os.environ.get(token_env, "").strip()
        if not token:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="github_token_not_configured",
                message="Configured merge train GitHub token is not available.",
            )
        try:
            candidate_store = require_merge_train_batch_candidate_record_store(record_store)
            landing_store = require_merge_train_batch_landing_plan_record_store(record_store)
            stack_collapse_store = require_merge_train_stack_collapse_plan_record_store(
                record_store
            )
            controller_state_store = require_merge_train_controller_state_record_store(record_store)
            admission_store = require_merge_admission_record_store(record_store)
        except TypeError as error:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message="Merge train batch landing storage requires database-backed records.",
            ) from error
        try:
            with merge_train_controller_mutation_fence(
                record_store=controller_state_store,
                repository=landing_request.repository,
                base_branch=landing_request.base_branch,
                policy_key=repository_policy.policy_key,
                policy_sha256=policy_record.policy_sha256,
                trace_id=trace_id,
                active_action="batch_landing_run_once",
                active_phase=landing_request.mode,
                active_record_id=(
                    landing_request.landing_plan_record_id or landing_request.candidate_record_id
                ),
            ) as lease:
                active_record_id = (
                    landing_request.landing_plan_record_id or landing_request.candidate_record_id
                )

                def checkpoint_landing_mutation(
                    phase: str,
                    pull_request_number: int | None,
                    landing_plan_id: str,
                    expected_effect_sha: str,
                ) -> None:
                    lease.checkpoint(
                        active_action="batch_landing_run_once",
                        active_phase=phase,
                        active_record_id=active_record_id,
                        active_pull_request_number=pull_request_number,
                        step_payload={
                            "mode": landing_request.mode,
                            "landing_plan_id": landing_plan_id,
                            "expected_effect_sha": expected_effect_sha,
                        },
                    )

                landing_result = execute_merge_train_batch_landing_run_once(
                    request=landing_request,
                    repository_policy=repository_policy,
                    policy_sha256=policy_record.policy_sha256,
                    token=token,
                    trace_id=trace_id,
                    recorded_at=lease.record.updated_at,
                    candidate_store=candidate_store,
                    landing_store=landing_store,
                    stack_collapse_store=stack_collapse_store,
                    admission_store=admission_store,
                    admission_evaluator=LiveMergeAdmissionEvaluator(
                        store=record_store,
                        repository_evidence_provider=(
                            resolved_change_impact_repository_evidence_provider
                        ),
                        technical_check_client=TenantAdmissionControllerGitHubClient(
                            transport=UrllibMergeTrainGitHubTransport(
                                token=token,
                                api_base_url=landing_request.github_api_base_url,
                            )
                        ),
                    ),
                    controller_state_provider=lease.read_current,
                    mutation_checkpoint=checkpoint_landing_mutation,
                )
                response = accepted_evidence_response(
                    trace_id=trace_id,
                    records=landing_result.records,
                    result=landing_result.accepted_result,
                )
                store_apply_idempotency(
                    record_store=record_store,
                    identity=identity,
                    route_path=_MERGE_TRAIN_BATCH_LANDING_RUN_ONCE_ROUTE,
                    idempotency_key=normalized_idempotency_key,
                    request_fingerprint_value=payload_fingerprint,
                    trace_id=trace_id,
                    response=response,
                )
        except MergeTrainGitHubStaleHeadError as error:
            return merge_train_github_stale_state_response(trace_id=trace_id, error=error)
        except MergeTrainGitHubError as error:
            return merge_train_github_request_failed_response(trace_id=trace_id, error=error)
        except MergeAdmissionDeniedError as error:
            raise _launchplane_http_error(
                status_code=409,
                trace_id=trace_id,
                code="merge_train_landing_not_admitted",
                message=str(error),
            ) from error
        except MergeAdmissionReconciliationRequiredError as error:
            raise _launchplane_http_error(
                status_code=409,
                trace_id=trace_id,
                code="merge_train_landing_reconcile_required",
                message=str(error),
            ) from error
        except (
            MergeTrainControllerLeaseHeldError,
            MergeTrainControllerLeaseLostError,
            MergeTrainControllerReconciliationRequiredError,
            MergeTrainControllerAdoptionRejectedError,
        ) as error:
            raise merge_train_controller_fence_http_error(trace_id=trace_id, error=error) from error
        except (
            MergeTrainBatchCandidateRecordNotFoundError,
            MergeTrainBatchLandingPlanRecordNotFoundError,
            MergeTrainStackCollapsePlanRecordNotFoundError,
        ) as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request could not be completed.",
            ) from error
        except ValueError as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request could not be completed.",
            ) from error
        return response

    async def write_merge_train_run_once(
        request: Request,
        identity: Annotated[LaunchplaneIdentity, Depends(read_write_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> AcceptedEvidenceResponse | JSONResponse:
        trace_id = next_trace_id()
        try:
            raw_payload = await request.json()
        except ValueError as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request payload failed validation.",
            ) from error
        if not isinstance(raw_payload, dict):
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request payload failed validation.",
            )
        try:
            merge_train_request = MergeTrainRunOnceEnvelope.model_validate(raw_payload)
        except ValidationError as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request payload failed validation.",
            ) from error

        normalized_idempotency_key = idempotency_key.strip()
        payload_fingerprint = build_request_fingerprint(raw_payload)
        if normalized_idempotency_key:
            (
                normalized_idempotency_key,
                payload_fingerprint,
                replay_response,
            ) = await replay_apply_idempotency(
                request=request,
                record_store=record_store,
                identity=identity,
                route_path=_MERGE_TRAIN_RUN_ONCE_ROUTE,
                idempotency_key=normalized_idempotency_key,
                trace_id=trace_id,
                check_replay=True,
            )
            if replay_response is not None:
                return replay_response

        try:
            policy_record = resolve_merge_train_policy_record(record_store)
        except MergeTrainPolicyStoreMissingError as error:
            raise merge_train_policy_not_configured_error(trace_id=trace_id, error=error) from error
        try:
            repository_policy = policy_record.policy.find_repository_policy(
                repository=merge_train_request.repository,
                base_branch=merge_train_request.base_branch,
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
                message="Workflow cannot run the requested merge train policy.",
            )
        token_env = repository_policy.github_token.env_var
        if not token_env:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="github_token_not_configured",
                message="Merge train policy does not define a GitHub token environment variable.",
            )
        token = os.environ.get(token_env, "").strip()
        if not token:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="github_token_not_configured",
                message="Configured merge train GitHub token is not available.",
            )
        try:
            run_record_store = require_merge_train_run_record_store(record_store)
            controller_state_store = (
                require_merge_train_controller_state_record_store(record_store)
                if merge_train_request.mutate
                else None
            )
        except TypeError as error:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message=str(error),
            ) from error
        try:
            if controller_state_store is None:
                run_once_result = execute_merge_train_run_once(
                    request=merge_train_request,
                    policy=policy_record.policy,
                    policy_sha256=policy_record.policy_sha256,
                    token=token,
                    trace_id=trace_id,
                    recorded_at=utc_now_timestamp(),
                )
                run_record_store.write_merge_train_run_record(run_once_result.run_record)
                response = accepted_evidence_response(
                    trace_id=trace_id,
                    records=run_once_result.records,
                    result=run_once_result.accepted_result,
                )
                store_apply_idempotency(
                    record_store=record_store,
                    identity=identity,
                    route_path=_MERGE_TRAIN_RUN_ONCE_ROUTE,
                    idempotency_key=normalized_idempotency_key,
                    request_fingerprint_value=payload_fingerprint,
                    trace_id=trace_id,
                    response=response,
                )
                return response
            else:
                with merge_train_controller_mutation_fence(
                    record_store=controller_state_store,
                    repository=merge_train_request.repository,
                    base_branch=merge_train_request.base_branch,
                    policy_key=repository_policy.policy_key,
                    policy_sha256=policy_record.policy_sha256,
                    trace_id=trace_id,
                    active_action="legacy_run_once",
                    active_phase="worker_step",
                ) as lease:

                    def checkpoint_legacy_mutation() -> None:
                        lease.checkpoint(
                            active_action="legacy_run_once",
                            active_phase="worker_step_mutation",
                            active_record_id="",
                            active_pull_request_number=None,
                            step_payload={},
                        )

                    run_once_result = execute_merge_train_run_once(
                        request=merge_train_request,
                        policy=policy_record.policy,
                        policy_sha256=policy_record.policy_sha256,
                        token=token,
                        trace_id=trace_id,
                        recorded_at=lease.record.updated_at,
                        mutation_checkpoint=checkpoint_legacy_mutation,
                    )
                    run_record_store.write_merge_train_run_record(run_once_result.run_record)
                    response = accepted_evidence_response(
                        trace_id=trace_id,
                        records=run_once_result.records,
                        result=run_once_result.accepted_result,
                    )
                    store_apply_idempotency(
                        record_store=record_store,
                        identity=identity,
                        route_path=_MERGE_TRAIN_RUN_ONCE_ROUTE,
                        idempotency_key=normalized_idempotency_key,
                        request_fingerprint_value=payload_fingerprint,
                        trace_id=trace_id,
                        response=response,
                    )
                    return response
        except MergeTrainGitHubStaleHeadError as error:
            return merge_train_github_stale_state_response(trace_id=trace_id, error=error)
        except MergeTrainGitHubError as error:
            return merge_train_github_request_failed_response(trace_id=trace_id, error=error)
        except (
            MergeTrainControllerLeaseHeldError,
            MergeTrainControllerLeaseLostError,
            MergeTrainControllerReconciliationRequiredError,
            MergeTrainControllerAdoptionRejectedError,
        ) as error:
            raise merge_train_controller_fence_http_error(trace_id=trace_id, error=error) from error

    async def write_merge_train_pr_feedback(
        request: Request,
        identity: Annotated[LaunchplaneIdentity, Depends(read_write_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> AcceptedEvidenceResponse | JSONResponse:
        trace_id = next_trace_id()
        try:
            raw_payload = await request.json()
        except ValueError as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request payload failed validation.",
            ) from error
        if not isinstance(raw_payload, dict):
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request payload failed validation.",
            )
        try:
            feedback_request = MergeTrainPrFeedbackEnvelope.model_validate(raw_payload)
        except ValidationError as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request payload failed validation.",
            ) from error

        normalized_idempotency_key = idempotency_key.strip()
        payload_fingerprint = build_request_fingerprint(raw_payload)
        if normalized_idempotency_key:
            (
                normalized_idempotency_key,
                payload_fingerprint,
                replay_response,
            ) = await replay_apply_idempotency(
                request=request,
                record_store=record_store,
                identity=identity,
                route_path=_MERGE_TRAIN_PR_FEEDBACK_ROUTE,
                idempotency_key=normalized_idempotency_key,
                trace_id=trace_id,
                check_replay=True,
            )
            if replay_response is not None:
                return replay_response

        try:
            policy_record = resolve_merge_train_policy_record(record_store)
        except MergeTrainPolicyStoreMissingError as error:
            raise merge_train_policy_not_configured_error(trace_id=trace_id, error=error) from error
        try:
            repository_policy = policy_record.policy.find_repository_policy(
                repository=feedback_request.repository,
                base_branch=feedback_request.base_branch,
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
                message="Workflow cannot write merge train PR feedback.",
            )
        token_env = repository_policy.github_token.env_var
        if not token_env:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="github_token_not_configured",
                message="Merge train policy does not define a GitHub token environment variable.",
            )
        token = os.environ.get(token_env, "").strip()
        if not token:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="github_token_not_configured",
                message="Configured merge train GitHub token is not available.",
            )
        try:
            feedback_store = require_merge_train_pr_feedback_record_store(record_store)
        except TypeError as error:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message=str(error),
            ) from error
        feedback_record = build_merge_train_pr_feedback_record(
            request=feedback_request,
            policy_key=repository_policy.policy_key,
            policy_sha256=policy_record.policy_sha256,
            token=token,
            recorded_at=utc_now_timestamp(),
            response_trace_id=trace_id,
        )
        feedback_store.write_merge_train_pr_feedback_record(feedback_record)
        result: dict[str, object] = {"feedback": feedback_record.model_dump(mode="json")}
        if feedback_record.delivery_status == "failed":
            return JSONResponse(
                status_code=502,
                content={
                    "status": "rejected",
                    "trace_id": trace_id,
                    "error": {
                        "code": "github_comment_delivery_failed",
                        "message": (
                            feedback_record.error_message
                            or "Merge train PR feedback comment delivery failed."
                        ),
                    },
                    "records": {
                        "merge_train_pr_feedback_id": feedback_record.feedback_id,
                    },
                    "result": result,
                },
            )
        response = accepted_evidence_response(
            trace_id=trace_id,
            records={"merge_train_pr_feedback_id": feedback_record.feedback_id},
            result=result,
        )
        store_apply_idempotency(
            record_store=record_store,
            identity=identity,
            route_path=_MERGE_TRAIN_PR_FEEDBACK_ROUTE,
            idempotency_key=normalized_idempotency_key,
            request_fingerprint_value=payload_fingerprint,
            trace_id=trace_id,
            response=response,
        )
        return response

    async def evaluate_agent_write_intent_route(
        request: Request,
        intent_request: AgentWriteIntentRequest,
        identity: Annotated[LaunchplaneIdentity, Depends(read_browser_mutation_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> AcceptedEvidenceResponse:
        trace_id = next_trace_id()
        (
            normalized_idempotency_key,
            payload_fingerprint,
            replayed_response,
        ) = await replay_apply_idempotency(
            request=request,
            record_store=record_store,
            identity=identity,
            route_path=_AGENT_WRITE_INTENT_EVALUATE_ROUTE,
            idempotency_key=idempotency_key,
            trace_id=trace_id,
            check_replay=True,
        )
        if replayed_response is not None:
            return replayed_response
        try:
            intent_store = require_agent_write_intent_write_store(record_store)
        except TypeError as error:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message=str(error),
            ) from error
        intent_authz_action = authz_action_for_agent_write_intent(intent_request.intent)
        authorized = resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action=intent_authz_action,
            product=intent_request.product,
            context=intent_request.context,
        )
        if intent_request.secret_bindings:
            secret_authz_action = agent_write_intent_secret_action(intent_request)
            authorized = authorized and resolved_authz_policy_runtime.policy.allows(
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
            policy_source=resolved_authz_policy_runtime.source,
            policy_sha256=resolved_authz_policy_runtime.policy_sha256,
        )
        secret_evidence = agent_write_intent_secret_evidence(
            record_store=record_store,
            request=intent_request,
        )
        evaluation = evaluate_agent_write_intent(
            request=intent_request,
            authorized=authorized,
            audit=intent_audit,
            secret_evidence=secret_evidence,
        )
        recorded_at = utc_now_timestamp()
        intent_record = AgentWriteIntentRecord(
            record_id=build_agent_write_intent_record_id(
                recorded_at=recorded_at,
                trace_id=trace_id,
                request=intent_request,
                evaluation=evaluation,
            ),
            recorded_at=recorded_at,
            trace_id=trace_id,
            idempotency_key=normalized_idempotency_key,
            request=intent_request,
            evaluation=evaluation,
        )
        intent_store.write_agent_write_intent_record(intent_record)
        response = accepted_evidence_response(
            trace_id=trace_id,
            records={},
            result={
                "intent": evaluation.model_dump(mode="json"),
                "record": {
                    "record_id": intent_record.record_id,
                    "recorded_at": intent_record.recorded_at,
                },
            },
        )
        store_apply_idempotency(
            record_store=record_store,
            identity=identity,
            route_path=_AGENT_WRITE_INTENT_EVALUATE_ROUTE,
            idempotency_key=normalized_idempotency_key,
            request_fingerprint_value=payload_fingerprint,
            trace_id=trace_id,
            response=response,
        )
        return response

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

    async def claim_every_code_work_request(
        request: Request,
        payload: dict[str, object],
        identity: Annotated[
            LaunchplaneIdentity | None,
            Depends(read_every_code_work_request_worker_write_identity),
        ],
        record_store: Annotated[object, Depends(get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> AcceptedEvidenceResponse:
        trace_id = next_trace_id()
        worker_token_authorized = every_code_worker_token_authorized(
            request.headers.get("Authorization", "")
        )
        idempotency_identity = identity or (
            TerminalAgentIdentity(
                subject="every-code-worker",
                token_label="every-code-worker-token",
            )
            if worker_token_authorized
            else None
        )
        if identity is not None:
            if not resolved_authz_policy_runtime.policy.allows(
                identity=identity,
                action="every_code_work_request.claim",
                product="launchplane",
                context=_LAUNCHPLANE_SERVICE_CONTEXT,
            ):
                raise _launchplane_http_error(
                    status_code=403,
                    trace_id=trace_id,
                    code="authorization_denied",
                    message="Workflow cannot claim Every Code work requests.",
                )
        normalized_idempotency_key = ""
        payload_fingerprint = ""
        if idempotency_identity is not None:
            (
                normalized_idempotency_key,
                payload_fingerprint,
                replayed_response,
            ) = await replay_apply_idempotency(
                request=request,
                record_store=record_store,
                identity=idempotency_identity,
                route_path="/v1/every-code/work-requests/claim",
                idempotency_key=idempotency_key,
                trace_id=trace_id,
                check_replay=True,
            )
            if replayed_response is not None:
                return replayed_response
        try:
            every_code_store = require_every_code_work_request_claim_store(record_store)
        except TypeError as error:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message=str(error),
            ) from error
        try:
            claim_request = EveryCodeWorkRequestClaimEnvelope.model_validate(payload)
        except (ValueError, ValidationError) as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_payload",
                message=str(error),
            ) from error

        def claim_response(record: EveryCodeWorkRequestRecord) -> AcceptedEvidenceResponse:
            return accepted_evidence_response(
                trace_id=trace_id,
                records={"request_id": record.request_id, "state": record.state},
                result={"request": record.model_dump(mode="json")},
            )

        def claim_idempotency_record(
            record: EveryCodeWorkRequestRecord,
        ) -> LaunchplaneIdempotencyRecord:
            if idempotency_identity is None:
                raise RuntimeError("Every Code claim idempotency requires an identity.")
            return build_apply_idempotency_record(
                identity=idempotency_identity,
                route_path="/v1/every-code/work-requests/claim",
                idempotency_key=normalized_idempotency_key,
                request_fingerprint_value=payload_fingerprint,
                trace_id=trace_id,
                response=claim_response(record),
            )

        try:
            claimed_record = every_code_store.claim_every_code_work_request_record(
                request_id=claim_request.request_id.strip(),
                host=claim_request.host.strip(),
                claimed_at=utc_now_timestamp(),
                lease_seconds=claim_request.lease_seconds,
                idempotency_record_factory=(
                    claim_idempotency_record
                    if idempotency_identity is not None and normalized_idempotency_key
                    else None
                ),
            )
        except FileNotFoundError as error:
            raise _launchplane_http_error(
                status_code=404,
                trace_id=trace_id,
                code="not_found",
                message=str(error),
            ) from error
        if claimed_record is None:
            if idempotency_identity is not None and normalized_idempotency_key:
                _, _, replayed_response = await replay_apply_idempotency(
                    request=request,
                    record_store=record_store,
                    identity=idempotency_identity,
                    route_path="/v1/every-code/work-requests/claim",
                    idempotency_key=normalized_idempotency_key,
                    trace_id=trace_id,
                    check_replay=True,
                )
                if replayed_response is not None:
                    return replayed_response
            raise _launchplane_http_error(
                status_code=409,
                trace_id=trace_id,
                code="work_request_already_claimed",
                message="Every Code work request is not queued for claim.",
            )
        return claim_response(claimed_record)

    async def write_every_code_work_request_heartbeat(
        request: Request,
        payload: dict[str, object],
        identity: Annotated[
            LaunchplaneIdentity | None,
            Depends(read_every_code_work_request_worker_write_identity),
        ],
        record_store: Annotated[object, Depends(get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> AcceptedEvidenceResponse:
        del idempotency_key
        trace_id = next_trace_id()
        worker_token_authorized = every_code_worker_token_authorized(
            request.headers.get("Authorization", "")
        )
        if identity is None and not worker_token_authorized:
            raise _launchplane_http_error(
                status_code=401,
                trace_id=trace_id,
                code="unauthorized",
                message="Every Code heartbeat requires authorization.",
            )
        try:
            heartbeat_store = require_every_code_work_request_heartbeat_store(record_store)
        except TypeError as error:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message=str(error),
            ) from error
        try:
            heartbeat_request = EveryCodeWorkRequestHeartbeatEnvelope.model_validate(payload)
        except (ValueError, ValidationError) as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_payload",
                message=str(error),
            ) from error
        now = utc_now_timestamp()
        new_lease_expires_at = add_lease_seconds(now, heartbeat_request.lease_seconds)
        accepted = heartbeat_store.heartbeat_every_code_work_request_record(
            request_id=heartbeat_request.request_id.strip(),
            host=heartbeat_request.host.strip(),
            fencing_token=heartbeat_request.fencing_token,
            heartbeat_at=now,
            lease_expires_at=new_lease_expires_at,
        )
        if not accepted:
            raise _launchplane_http_error(
                status_code=409,
                trace_id=trace_id,
                code="heartbeat_rejected",
                message="Every Code heartbeat rejected: wrong owner, fencing token, or terminal state.",
            )
        return accepted_evidence_response(
            trace_id=trace_id,
            records={
                "request_id": heartbeat_request.request_id.strip(),
                "fencing_token": heartbeat_request.fencing_token,
            },
            result={
                "request_id": heartbeat_request.request_id.strip(),
                "lease_expires_at": new_lease_expires_at,
            },
        )

    async def recover_stale_every_code_work_requests(
        request: Request,
        payload: dict[str, object],
        identity: Annotated[
            LaunchplaneIdentity | None,
            Depends(read_every_code_work_request_worker_write_identity),
        ],
        record_store: Annotated[object, Depends(get_record_store)],
    ) -> AcceptedEvidenceResponse:
        del payload
        trace_id = next_trace_id()
        worker_token_authorized = every_code_worker_token_authorized(
            request.headers.get("Authorization", "")
        )
        if identity is None and not worker_token_authorized:
            raise _launchplane_http_error(
                status_code=401,
                trace_id=trace_id,
                code="unauthorized",
                message="Every Code stale recovery requires authorization.",
            )
        if identity is not None and not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action="every_code_work_request.update",
            product="launchplane",
            context=_LAUNCHPLANE_SERVICE_CONTEXT,
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Workflow cannot recover stale Every Code work requests.",
            )
        try:
            stale_store = require_every_code_work_request_stale_store(record_store)
        except TypeError as error:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message=str(error),
            ) from error
        now = utc_now_timestamp()
        stale_records = stale_store.list_stale_every_code_work_request_records(
            as_of=now,
            limit=20,
        )
        requeued: list[str] = []
        flagged: list[str] = []
        for stale in stale_records:
            recovered = stale_store.recover_stale_every_code_work_request_record(
                expected_record=stale,
                recovered_at=now,
            )
            if recovered is None:
                continue
            if recovered.state == "queued":
                requeued.append(recovered.request_id)
            elif recovered.state == "blocked":
                flagged.append(recovered.request_id)
        return accepted_evidence_response(
            trace_id=trace_id,
            records={
                "checked": len(stale_records),
                "requeued": len(requeued),
                "flagged": len(flagged),
            },
            result={"requeued": requeued, "flagged": flagged},
        )

    async def write_every_code_work_request_status(
        request: Request,
        payload: dict[str, object],
        identity: Annotated[
            LaunchplaneIdentity | None,
            Depends(read_every_code_work_request_worker_write_identity),
        ],
        record_store: Annotated[object, Depends(get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> AcceptedEvidenceResponse:
        trace_id = next_trace_id()
        normalized_idempotency_key = ""
        payload_fingerprint = ""
        worker_token_authorized = every_code_worker_token_authorized(
            request.headers.get("Authorization", "")
        )
        idempotency_identity = identity or (
            TerminalAgentIdentity(
                subject="every-code-worker",
                token_label="every-code-worker-token",
            )
            if worker_token_authorized
            else None
        )
        if identity is not None:
            if not resolved_authz_policy_runtime.policy.allows(
                identity=identity,
                action="every_code_work_request.update",
                product="launchplane",
                context=_LAUNCHPLANE_SERVICE_CONTEXT,
            ):
                raise _launchplane_http_error(
                    status_code=403,
                    trace_id=trace_id,
                    code="authorization_denied",
                    message="Workflow cannot update Every Code work requests.",
                )
        if idempotency_identity is not None:
            (
                normalized_idempotency_key,
                payload_fingerprint,
                replayed_response,
            ) = await replay_apply_idempotency(
                request=request,
                record_store=record_store,
                identity=idempotency_identity,
                route_path="/v1/every-code/work-requests/status",
                idempotency_key=idempotency_key,
                trace_id=trace_id,
                check_replay=True,
            )
            if replayed_response is not None:
                return replayed_response
        try:
            every_code_store = require_every_code_work_request_status_store(record_store)
        except TypeError as error:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message=str(error),
            ) from error
        try:
            status_request = EveryCodeWorkRequestStatusEnvelope.model_validate(payload)
        except (ValueError, ValidationError) as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_payload",
                message=str(error),
            ) from error
        try:
            updated_record = every_code_store.update_every_code_work_request_status_record(
                request_id=status_request.request_id.strip(),
                update=EveryCodeWorkRequestStatusUpdate(
                    state=status_request.state,
                    host=status_request.host,
                    updated_at=status_request.updated_at.strip() or utc_now_timestamp(),
                    fencing_token=status_request.fencing_token,
                    result_pr_url=status_request.result_pr_url,
                    result_summary=status_request.result_summary,
                    error_message=status_request.error_message,
                ),
            )
        except FileNotFoundError as error:
            raise _launchplane_http_error(
                status_code=404,
                trace_id=trace_id,
                code="not_found",
                message=str(error),
            ) from error
        except (ValueError, ValidationError) as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_payload",
                message=str(error),
            ) from error
        notification_attempts: tuple[EveryCodeNotificationAttemptRecord, ...] = ()
        if updated_record.state == "blocked":
            notification_attempts = deliver_every_code_blocked_notifications(
                record_store=record_store,
                request=updated_record,
                attempted_at=utc_now_timestamp(),
                discord_sender=every_code_discord_sender,
            )
        response = accepted_evidence_response(
            trace_id=trace_id,
            records={"request_id": updated_record.request_id, "state": updated_record.state},
            result={
                "request": updated_record.model_dump(mode="json"),
                "notifications": [
                    notification_attempt.model_dump(mode="json")
                    for notification_attempt in notification_attempts
                ],
            },
        )
        if idempotency_identity is not None:
            store_apply_idempotency(
                record_store=record_store,
                identity=idempotency_identity,
                route_path="/v1/every-code/work-requests/status",
                idempotency_key=normalized_idempotency_key,
                request_fingerprint_value=payload_fingerprint,
                trace_id=trace_id,
                response=response,
            )
        return response

    async def rerun_every_code_work_request(
        request: Request,
        payload: dict[str, object],
        identity: Annotated[
            LaunchplaneIdentity | None,
            Depends(read_every_code_work_request_worker_write_identity),
        ],
        record_store: Annotated[object, Depends(get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> AcceptedEvidenceResponse:
        trace_id = next_trace_id()
        normalized_idempotency_key = ""
        intent_idempotency_key = idempotency_key.strip()
        payload_fingerprint = ""
        worker_token_authorized = every_code_worker_token_authorized(
            request.headers.get("Authorization", "")
        )
        idempotency_identity = identity or (
            TerminalAgentIdentity(
                subject="every-code-worker",
                token_label="every-code-worker-token",
            )
            if worker_token_authorized
            else None
        )
        if identity is not None:
            if not resolved_authz_policy_runtime.policy.allows(
                identity=identity,
                action="every_code_work_request.rerun",
                product="launchplane",
                context=_LAUNCHPLANE_SERVICE_CONTEXT,
            ):
                raise _launchplane_http_error(
                    status_code=403,
                    trace_id=trace_id,
                    code="authorization_denied",
                    message="Workflow cannot rerun Every Code work requests.",
                )
        if idempotency_identity is not None:
            (
                normalized_idempotency_key,
                payload_fingerprint,
                replayed_response,
            ) = await replay_apply_idempotency(
                request=request,
                record_store=record_store,
                identity=idempotency_identity,
                route_path=_EVERY_CODE_WORK_REQUEST_RERUN_ROUTE,
                idempotency_key=idempotency_key,
                trace_id=trace_id,
                check_replay=True,
            )
            if replayed_response is not None:
                return replayed_response
            intent_idempotency_key = normalized_idempotency_key
        try:
            rerun_request = EveryCodeWorkRequestRerunEnvelope.model_validate(payload)
        except (ValueError, ValidationError) as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_payload",
                message=str(error),
            ) from error
        try:
            every_code_store = require_every_code_work_request_rerun_store(record_store)
            require_agent_write_intent_read_store(record_store)
        except TypeError as error:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message=str(error),
            ) from error
        rerun_checked_at = datetime.now(timezone.utc)
        intent_record = validate_every_code_rerun_write_intent(
            record_store=record_store,
            rerun_request=rerun_request,
            idempotency_key=intent_idempotency_key,
            now=rerun_checked_at,
            trace_id=trace_id,
        )
        try:
            existing_record = every_code_store.read_every_code_work_request_record(
                rerun_request.request_id
            )
        except FileNotFoundError as error:
            raise _launchplane_http_error(
                status_code=404,
                trace_id=trace_id,
                code="not_found",
                message=str(error),
            ) from error
        if intent_record is None:
            intent_record = matching_every_code_rerun_intent_record(
                record_store=record_store,
                source_url=existing_record.issue_url,
                now=rerun_checked_at,
            )
            if intent_record is None:
                raise reject_agent_write_intent(
                    trace_id=trace_id,
                    code="agent_write_intent_required",
                    message="Every Code rerun requires matching approved write-intent evidence.",
                )
        if intent_record.request.source_url != existing_record.issue_url:
            raise reject_agent_write_intent(
                trace_id=trace_id,
                code="agent_write_intent_source_mismatch",
                message="Every Code rerun write-intent source_url does not match the work-request issue URL.",
                record_id=intent_record.record_id,
            )
        if rerun_request.source_url and rerun_request.source_url != existing_record.issue_url:
            raise reject_agent_write_intent(
                trace_id=trace_id,
                code="agent_write_intent_source_mismatch",
                message="Every Code rerun source_url does not match the work-request issue URL.",
                record_id=intent_record.record_id,
            )
        try:
            requeued_record = requeue_every_code_work_request(
                existing_record,
                queued_at=utc_now_timestamp(),
                trigger_actor=rerun_request.trigger_actor,
            )
        except (ValueError, ValidationError) as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_payload",
                message=str(error),
            ) from error
        response = accepted_evidence_response(
            trace_id=trace_id,
            records={
                "request_id": requeued_record.request_id,
                "state": requeued_record.state,
                "agent_write_intent_record_id": intent_record.record_id,
            },
            result={"request": requeued_record.model_dump(mode="json")},
        )
        idempotency_record = None
        if idempotency_identity is not None and normalized_idempotency_key:
            idempotency_record = build_apply_idempotency_record(
                identity=idempotency_identity,
                route_path=_EVERY_CODE_WORK_REQUEST_RERUN_ROUTE,
                idempotency_key=normalized_idempotency_key,
                request_fingerprint_value=payload_fingerprint,
                trace_id=trace_id,
                response=response,
            )
        write_status = every_code_store.compare_and_write_every_code_work_request_record(
            expected_record=existing_record,
            record=requeued_record,
            idempotency_record=idempotency_record,
        )
        if write_status == "missing":
            raise _launchplane_http_error(
                status_code=404,
                trace_id=trace_id,
                code="not_found",
                message=f"Every Code work request {rerun_request.request_id!r} was not found.",
            )
        if write_status == "changed":
            if idempotency_identity is not None and normalized_idempotency_key:
                _, _, replayed_response = await replay_apply_idempotency(
                    request=request,
                    record_store=record_store,
                    identity=idempotency_identity,
                    route_path=_EVERY_CODE_WORK_REQUEST_RERUN_ROUTE,
                    idempotency_key=normalized_idempotency_key,
                    trace_id=trace_id,
                    check_replay=True,
                )
                if replayed_response is not None:
                    return replayed_response
            raise _launchplane_http_error(
                status_code=409,
                trace_id=trace_id,
                code="work_request_changed",
                message="Every Code work request changed while the rerun was being prepared.",
            )
        return response

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

    def ensure_route_binding_allowed(
        *,
        identity: LaunchplaneIdentity,
        trace_id: str,
        action: str,
        product: str,
        context_name: str,
        instance_name: str,
        message: str,
    ) -> None:
        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action=action,
            product=product,
            context=context_name,
            target=AuthorizationTarget(scope="instance", instances=(instance_name,)),
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message=message,
            )

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
        try:
            existing_profile = profile_store.read_product_profile_record(profile.product)
        except (FileNotFoundError, KeyError):
            existing_profile = None
        try:
            validate_product_profile_history_transition(
                existing_profile=existing_profile,
                replacement_profile=profile,
            )
        except ValueError as error:
            raise _launchplane_http_error(
                status_code=409,
                trace_id=trace_id,
                code="historical_context_reactivation_blocked",
                message=str(error),
            ) from error
        if existing_profile is not None:
            if control_plane_product_health_monitoring.product_health_monitoring_authority(
                existing_profile
            ) != control_plane_product_health_monitoring.product_health_monitoring_authority(
                profile
            ):
                raise _launchplane_http_error(
                    status_code=409,
                    trace_id=trace_id,
                    code="health_monitoring_bounded_apply_required",
                    message=(
                        "Existing product health monitoring authority must be changed through "
                        "the reviewed health-monitoring apply endpoint."
                    ),
                )
            if (
                control_plane_product_prelaunch_rebuild_policy.product_prelaunch_rebuild_policy_authority(
                    existing_profile
                )
                != control_plane_product_prelaunch_rebuild_policy.product_prelaunch_rebuild_policy_authority(
                    profile
                )
            ):
                raise _launchplane_http_error(
                    status_code=409,
                    trace_id=trace_id,
                    code="prelaunch_rebuild_bounded_apply_required",
                    message=(
                        "Existing product prelaunch rebuild authority must be changed through "
                        "the reviewed prelaunch-rebuild apply endpoint."
                    ),
                )
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

    async def apply_product_expected_config(
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
                message="Product expected config request failed validation.",
            ) from error
        if not isinstance(raw_payload, dict):
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Product expected config request failed validation.",
            )
        request_payload = raw_payload
        try:
            expected_config_request = ProductExpectedConfigApplyEnvelope.model_validate(
                request_payload
            )
        except ValidationError as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Product expected config request failed validation.",
            ) from error
        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action="product_profile.expected_config.apply",
            product=expected_config_request.product,
            context=_LAUNCHPLANE_SERVICE_CONTEXT,
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Workflow cannot apply product expected config metadata.",
            )
        (
            normalized_idempotency_key,
            payload_fingerprint,
            replay_response,
        ) = await replay_apply_idempotency(
            request=request,
            record_store=record_store,
            identity=identity,
            route_path=_PRODUCT_EXPECTED_CONFIG_APPLY_ROUTE,
            idempotency_key=idempotency_key,
            trace_id=trace_id,
            check_replay=bool(idempotency_key.strip()),
        )
        if replay_response is not None:
            return replay_response
        try:
            profile_read_store = require_product_profile_read_store(record_store)
            profile = profile_read_store.read_product_profile_record(
                expected_config_request.product
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
        merged_profile, result = _merge_product_expected_config(
            profile=profile,
            request=expected_config_request,
            updated_at=utc_now_timestamp(),
        )
        if expected_config_request.mode == "apply" and result["changed"]:
            try:
                profile_write_store = require_product_profile_write_store(record_store)
            except TypeError as error:
                raise _launchplane_http_error(
                    status_code=503,
                    trace_id=trace_id,
                    code="database_storage_required",
                    message=str(error),
                ) from error
            profile_write_store.write_product_profile_record(merged_profile)
        response = accepted_evidence_response(
            trace_id=trace_id,
            records={"product_profile": expected_config_request.product},
            result=result,
        )
        store_apply_idempotency(
            record_store=record_store,
            identity=identity,
            route_path=_PRODUCT_EXPECTED_CONFIG_APPLY_ROUTE,
            idempotency_key=normalized_idempotency_key,
            request_fingerprint_value=payload_fingerprint,
            trace_id=trace_id,
            response=response,
        )
        return response

    async def apply_product_health_monitoring(
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
                message="Product health monitoring request failed validation.",
            ) from error
        if not isinstance(raw_payload, dict):
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Product health monitoring request failed validation.",
            )
        try:
            health_monitoring_request = control_plane_product_health_monitoring.ProductHealthMonitoringApplyRequest.model_validate(
                raw_payload
            )
        except ValidationError as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Product health monitoring request failed validation.",
            ) from error
        action = (
            "product_profile.health_monitoring.apply"
            if health_monitoring_request.mode == "apply"
            else "product_profile.health_monitoring.plan"
        )
        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action=action,
            product=health_monitoring_request.product,
            context=health_monitoring_request.context,
            target=AuthorizationTarget(
                scope="instance",
                instances=(health_monitoring_request.instance,),
            ),
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Workflow cannot apply product health monitoring policy.",
            )
        normalized_idempotency_key = idempotency_key.strip()
        if health_monitoring_request.mode == "apply" and not normalized_idempotency_key:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="idempotency_key_required",
                message="Product health monitoring apply requests require an Idempotency-Key header.",
            )
        database_store = require_product_health_monitoring_database_store(
            record_store=record_store,
            trace_id=trace_id,
        )
        payload_fingerprint = ""

        def prepare_product_health_monitoring_mutation() -> AcceptedEvidenceResponse | None:
            preflight = database_store.prepare_db_only_mutation(
                scope=idempotency_scope(identity),
                route_path=_PRODUCT_HEALTH_MONITORING_APPLY_ROUTE,
                idempotency_key=normalized_idempotency_key,
                request_fingerprint=payload_fingerprint,
            )
            if preflight.status in {"missing", "released"}:
                return None
            if preflight.record is None:
                raise RuntimeError(
                    "Product health monitoring mutation preflight requires evidence."
                )
            if preflight.status == "replayed":
                return replay_idempotent_response(
                    trace_id=trace_id,
                    stored_record=preflight.record,
                    route_path=_PRODUCT_HEALTH_MONITORING_APPLY_ROUTE,
                )
            if preflight.status == "conflict":
                raise _launchplane_http_error(
                    status_code=409,
                    trace_id=trace_id,
                    code="idempotency_key_reused",
                    message=(
                        "Idempotency-Key was already used for a different "
                        "Launchplane request payload on this route."
                    ),
                )
            if preflight.status == "in_progress":
                raise _launchplane_http_error(
                    status_code=409,
                    trace_id=trace_id,
                    code="mutation_in_progress",
                    message=(
                        "A matching product health monitoring mutation is already running. "
                        "Retry with the same Idempotency-Key."
                    ),
                )
            if preflight.status == "reconcile_required":
                raise _launchplane_http_error(
                    status_code=409,
                    trace_id=trace_id,
                    code="mutation_reconciliation_required",
                    message=(
                        "The product health monitoring mutation requires reconciliation "
                        "before retry."
                    ),
                )
            raise RuntimeError(
                "Unsupported product health monitoring mutation preflight status: "
                f"{preflight.status}"
            )

        if health_monitoring_request.mode == "apply":
            payload_fingerprint = idempotency_request_fingerprint(
                route_path=_PRODUCT_HEALTH_MONITORING_APPLY_ROUTE,
                payload=raw_payload,
            )
            replay_response = prepare_product_health_monitoring_mutation()
            if replay_response is not None:
                return replay_response
        try:
            profile = database_store.read_product_profile_record(health_monitoring_request.product)
        except FileNotFoundError as error:
            raise _launchplane_http_error(
                status_code=404,
                trace_id=trace_id,
                code="not_found",
                message=str(error),
            ) from error
        try:
            if (
                health_monitoring_request.check_kind == "private_http"
                and health_monitoring_request.enabled
            ):
                try:
                    private_endpoint = database_store.read_private_health_endpoint_record(
                        health_monitoring_request.private_endpoint_key
                    )
                except FileNotFoundError as error:
                    raise control_plane_product_health_monitoring.ProductHealthMonitoringTargetError(
                        "Private health endpoint was not found."
                    ) from error
                control_plane_product_health_monitoring.validate_product_health_monitoring_private_endpoint(
                    request=health_monitoring_request,
                    endpoint=private_endpoint,
                )
            plan = control_plane_product_health_monitoring.build_product_health_monitoring_plan(
                profile=profile,
                request=health_monitoring_request,
            )
        except (
            control_plane_product_health_monitoring.ProductHealthMonitoringCheckKindError
        ) as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="unsupported_health_check_kind",
                message=str(error),
            ) from error
        except control_plane_product_health_monitoring.ProductHealthMonitoringTargetError as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_health_monitoring_target",
                message=str(error),
            ) from error
        if (
            health_monitoring_request.mode == "apply"
            and health_monitoring_request.reviewed_plan_sha256 != plan.plan_sha256
        ):
            replay_response = prepare_product_health_monitoring_mutation()
            if replay_response is not None:
                return replay_response
            raise _launchplane_http_error(
                status_code=409,
                trace_id=trace_id,
                code="stale",
                message=(
                    "Reviewed product health monitoring plan no longer matches the stored profile."
                ),
            )
        if health_monitoring_request.mode == "apply":
            replacement_profile = profile
            profile_updated_at_after = profile.updated_at
            if plan.changed:
                try:
                    replacement_profile = control_plane_product_health_monitoring.updated_product_health_monitoring_profile(
                        profile=profile,
                        request=health_monitoring_request,
                        updated_at=utc_now_timestamp(),
                    )
                except ValueError as error:
                    raise _launchplane_http_error(
                        status_code=400,
                        trace_id=trace_id,
                        code="invalid_product_profile",
                        message="Updated product profile failed validation.",
                    ) from error
                profile_updated_at_after = replacement_profile.updated_at
            result_plan = plan.model_copy(
                update={
                    "applied": True,
                    "profile_updated_at_after": profile_updated_at_after,
                }
            )
            response = accepted_evidence_response(
                trace_id=trace_id,
                records={
                    "product_profile": health_monitoring_request.product,
                    "context": health_monitoring_request.context,
                    "instance": health_monitoring_request.instance,
                    "health_check": result_plan.check_name,
                },
                result=result_plan.model_dump(mode="json"),
            )
            mutation = DbOnlyMutationRequest(
                scope=idempotency_scope(identity),
                route_path=_PRODUCT_HEALTH_MONITORING_APPLY_ROUTE,
                idempotency_key=normalized_idempotency_key,
                request_fingerprint=payload_fingerprint,
                lease_owner=trace_id,
                response_status_code=202,
                response_trace_id=trace_id,
                response_payload=response.model_dump(mode="json", exclude_none=True),
                lease_seconds=int(_DB_ONLY_MUTATION_LEASE.total_seconds()),
            )
            write_result = database_store.compare_and_write_product_profile_record(
                expected_record=profile,
                replacement_record=replacement_profile,
                mutation=mutation,
            )
            if write_result.status == "replayed":
                if write_result.idempotency_record is None:
                    raise RuntimeError("Replayed product profile write requires evidence.")
                return replay_idempotent_response(
                    trace_id=trace_id,
                    stored_record=write_result.idempotency_record,
                    route_path=_PRODUCT_HEALTH_MONITORING_APPLY_ROUTE,
                )
            if write_result.status == "idempotency_conflict":
                raise _launchplane_http_error(
                    status_code=409,
                    trace_id=trace_id,
                    code="idempotency_key_reused",
                    message=(
                        "Idempotency-Key was already used for a different "
                        "Launchplane request payload on this route."
                    ),
                )
            if write_result.status == "missing":
                raise _launchplane_http_error(
                    status_code=404,
                    trace_id=trace_id,
                    code="not_found",
                    message=(
                        "Product profile disappeared before the health monitoring "
                        "change could be applied."
                    ),
                )
            if write_result.status == "changed":
                raise _launchplane_http_error(
                    status_code=409,
                    trace_id=trace_id,
                    code="stale",
                    message=(
                        "Product profile changed while applying the reviewed health "
                        "monitoring plan."
                    ),
                )
            if write_result.status == "reservation_in_progress":
                raise _launchplane_http_error(
                    status_code=409,
                    trace_id=trace_id,
                    code="mutation_in_progress",
                    message=(
                        "A matching product health monitoring mutation is already running. "
                        "Retry with the same Idempotency-Key."
                    ),
                )
            if write_result.status == "reconciliation_required":
                raise _launchplane_http_error(
                    status_code=409,
                    trace_id=trace_id,
                    code="mutation_reconciliation_required",
                    message=(
                        "The product health monitoring mutation requires reconciliation "
                        "before retry."
                    ),
                )
            return response
        return accepted_evidence_response(
            trace_id=trace_id,
            records={
                "product_profile": health_monitoring_request.product,
                "context": health_monitoring_request.context,
                "instance": health_monitoring_request.instance,
                "health_check": plan.check_name,
            },
            result=plan.model_dump(mode="json"),
        )

    async def apply_product_prelaunch_rebuild_policy(
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
                message="Product prelaunch rebuild policy request failed validation.",
            ) from error
        if not isinstance(raw_payload, dict):
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Product prelaunch rebuild policy request failed validation.",
            )
        try:
            prelaunch_rebuild_request = control_plane_product_prelaunch_rebuild_policy.ProductPrelaunchRebuildPolicyApplyRequest.model_validate(
                raw_payload
            )
        except ValidationError as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Product prelaunch rebuild policy request failed validation.",
            ) from error
        action = (
            "product_profile.prelaunch_rebuild.apply"
            if prelaunch_rebuild_request.mode == "apply"
            else "product_profile.prelaunch_rebuild.plan"
        )
        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action=action,
            product=prelaunch_rebuild_request.product,
            context=prelaunch_rebuild_request.context,
            target=AuthorizationTarget(
                scope="instance",
                instances=(prelaunch_rebuild_request.instance,),
            ),
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Workflow cannot apply product prelaunch rebuild policy.",
            )
        normalized_idempotency_key = idempotency_key.strip()
        if prelaunch_rebuild_request.mode == "apply" and not normalized_idempotency_key:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="idempotency_key_required",
                message=(
                    "Product prelaunch rebuild policy apply requests require an "
                    "Idempotency-Key header."
                ),
            )
        database_store = require_product_prelaunch_rebuild_policy_database_store(
            record_store=record_store,
            trace_id=trace_id,
        )
        payload_fingerprint = ""

        def prepare_product_prelaunch_rebuild_policy_mutation() -> AcceptedEvidenceResponse | None:
            preflight = database_store.prepare_db_only_mutation(
                scope=idempotency_scope(identity),
                route_path=_PRODUCT_PRELAUNCH_REBUILD_POLICY_APPLY_ROUTE,
                idempotency_key=normalized_idempotency_key,
                request_fingerprint=payload_fingerprint,
            )
            if preflight.status in {"missing", "released"}:
                return None
            if preflight.record is None:
                raise RuntimeError(
                    "Product prelaunch rebuild policy mutation preflight requires evidence."
                )
            if preflight.status == "replayed":
                return replay_idempotent_response(
                    trace_id=trace_id,
                    stored_record=preflight.record,
                    route_path=_PRODUCT_PRELAUNCH_REBUILD_POLICY_APPLY_ROUTE,
                )
            if preflight.status == "conflict":
                raise _launchplane_http_error(
                    status_code=409,
                    trace_id=trace_id,
                    code="idempotency_key_reused",
                    message=(
                        "Idempotency-Key was already used for a different "
                        "Launchplane request payload on this route."
                    ),
                )
            if preflight.status == "in_progress":
                raise _launchplane_http_error(
                    status_code=409,
                    trace_id=trace_id,
                    code="mutation_in_progress",
                    message=(
                        "A matching product prelaunch rebuild policy mutation is already "
                        "running. Retry with the same Idempotency-Key."
                    ),
                )
            if preflight.status == "reconcile_required":
                raise _launchplane_http_error(
                    status_code=409,
                    trace_id=trace_id,
                    code="mutation_reconciliation_required",
                    message=(
                        "The product prelaunch rebuild policy mutation requires "
                        "reconciliation before retry."
                    ),
                )
            raise RuntimeError(
                "Unsupported product prelaunch rebuild policy mutation preflight status: "
                f"{preflight.status}"
            )

        if prelaunch_rebuild_request.mode == "apply":
            payload_fingerprint = idempotency_request_fingerprint(
                route_path=_PRODUCT_PRELAUNCH_REBUILD_POLICY_APPLY_ROUTE,
                payload=raw_payload,
            )
            replay_response = prepare_product_prelaunch_rebuild_policy_mutation()
            if replay_response is not None:
                return replay_response
        try:
            profile = database_store.read_product_profile_record(prelaunch_rebuild_request.product)
        except FileNotFoundError as error:
            raise _launchplane_http_error(
                status_code=404,
                trace_id=trace_id,
                code="not_found",
                message=str(error),
            ) from error
        try:
            plan = control_plane_product_prelaunch_rebuild_policy.build_product_prelaunch_rebuild_policy_plan(
                profile=profile,
                request=prelaunch_rebuild_request,
            )
        except (
            control_plane_product_prelaunch_rebuild_policy.ProductPrelaunchRebuildPolicyDriverError
        ) as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="unsupported_product_driver",
                message=str(error),
            ) from error
        except (
            control_plane_product_prelaunch_rebuild_policy.ProductPrelaunchRebuildPolicyStateError
        ) as error:
            raise _launchplane_http_error(
                status_code=409,
                trace_id=trace_id,
                code="prelaunch_rebuild_policy_blocked",
                message=str(error),
            ) from error
        except (
            control_plane_product_prelaunch_rebuild_policy.ProductPrelaunchRebuildPolicyTargetError
        ) as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message=str(error),
            ) from error
        if (
            prelaunch_rebuild_request.mode == "apply"
            and prelaunch_rebuild_request.reviewed_plan_sha256 != plan.plan_sha256
        ):
            replay_response = prepare_product_prelaunch_rebuild_policy_mutation()
            if replay_response is not None:
                return replay_response
            raise _launchplane_http_error(
                status_code=409,
                trace_id=trace_id,
                code="stale",
                message=(
                    "Reviewed product prelaunch rebuild policy plan no longer matches "
                    "the stored profile."
                ),
            )
        if prelaunch_rebuild_request.mode == "apply":
            replacement_profile = profile
            profile_updated_at_after = profile.updated_at
            if plan.changed:
                try:
                    replacement_profile = control_plane_product_prelaunch_rebuild_policy.updated_product_prelaunch_rebuild_policy_profile(
                        profile=profile,
                        request=prelaunch_rebuild_request,
                        updated_at=utc_now_timestamp(),
                    )
                except ValueError as error:
                    raise _launchplane_http_error(
                        status_code=400,
                        trace_id=trace_id,
                        code="invalid_product_profile",
                        message="Updated product profile failed validation.",
                    ) from error
                profile_updated_at_after = replacement_profile.updated_at
            result_plan = plan.model_copy(
                update={
                    "applied": True,
                    "profile_updated_at_after": profile_updated_at_after,
                }
            )
            response = accepted_evidence_response(
                trace_id=trace_id,
                records={
                    "product_profile": prelaunch_rebuild_request.product,
                    "context": prelaunch_rebuild_request.context,
                    "instance": prelaunch_rebuild_request.instance,
                },
                result=result_plan.model_dump(mode="json"),
            )
            mutation = DbOnlyMutationRequest(
                scope=idempotency_scope(identity),
                route_path=_PRODUCT_PRELAUNCH_REBUILD_POLICY_APPLY_ROUTE,
                idempotency_key=normalized_idempotency_key,
                request_fingerprint=payload_fingerprint,
                lease_owner=trace_id,
                response_status_code=202,
                response_trace_id=trace_id,
                response_payload=response.model_dump(mode="json", exclude_none=True),
                lease_seconds=int(_DB_ONLY_MUTATION_LEASE.total_seconds()),
            )
            write_result = database_store.compare_and_write_product_profile_record(
                expected_record=profile,
                replacement_record=replacement_profile,
                mutation=mutation,
            )
            if write_result.status == "replayed":
                if write_result.idempotency_record is None:
                    raise RuntimeError("Replayed product profile write requires evidence.")
                return replay_idempotent_response(
                    trace_id=trace_id,
                    stored_record=write_result.idempotency_record,
                    route_path=_PRODUCT_PRELAUNCH_REBUILD_POLICY_APPLY_ROUTE,
                )
            if write_result.status == "idempotency_conflict":
                raise _launchplane_http_error(
                    status_code=409,
                    trace_id=trace_id,
                    code="idempotency_key_reused",
                    message=(
                        "Idempotency-Key was already used for a different "
                        "Launchplane request payload on this route."
                    ),
                )
            if write_result.status == "missing":
                raise _launchplane_http_error(
                    status_code=404,
                    trace_id=trace_id,
                    code="not_found",
                    message=(
                        "Product profile disappeared before the prelaunch rebuild "
                        "policy change could be applied."
                    ),
                )
            if write_result.status == "changed":
                raise _launchplane_http_error(
                    status_code=409,
                    trace_id=trace_id,
                    code="stale",
                    message=(
                        "Product profile changed while applying the reviewed prelaunch "
                        "rebuild policy plan."
                    ),
                )
            if write_result.status == "reservation_in_progress":
                raise _launchplane_http_error(
                    status_code=409,
                    trace_id=trace_id,
                    code="mutation_in_progress",
                    message=(
                        "A matching product prelaunch rebuild policy mutation is already "
                        "running. Retry with the same Idempotency-Key."
                    ),
                )
            if write_result.status == "reconciliation_required":
                raise _launchplane_http_error(
                    status_code=409,
                    trace_id=trace_id,
                    code="mutation_reconciliation_required",
                    message=(
                        "The product prelaunch rebuild policy mutation requires "
                        "reconciliation before retry."
                    ),
                )
            return response
        return accepted_evidence_response(
            trace_id=trace_id,
            records={
                "product_profile": prelaunch_rebuild_request.product,
                "context": prelaunch_rebuild_request.context,
                "instance": prelaunch_rebuild_request.instance,
            },
            result=plan.model_dump(mode="json"),
        )

    async def apply_product_preview_tls(
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
                message="Product preview TLS request failed validation.",
            ) from error
        if not isinstance(raw_payload, dict):
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Product preview TLS request failed validation.",
            )
        try:
            preview_tls_request = (
                control_plane_product_preview_tls.ProductPreviewTlsApplyRequest.model_validate(
                    raw_payload
                )
            )
        except ValidationError as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Product preview TLS request failed validation.",
            ) from error
        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action="product_profile.preview_tls.apply",
            product=preview_tls_request.product,
            context=_LAUNCHPLANE_SERVICE_CONTEXT,
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Workflow cannot apply product preview TLS policy.",
            )
        normalized_idempotency_key = idempotency_key.strip()
        if preview_tls_request.mode == "apply" and not normalized_idempotency_key:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="idempotency_key_required",
                message="Product preview TLS apply requests require an Idempotency-Key header.",
            )
        database_store = require_product_preview_tls_database_store(
            record_store=record_store,
            trace_id=trace_id,
        )
        payload_fingerprint = ""

        def prepare_product_preview_tls_mutation() -> AcceptedEvidenceResponse | None:
            preflight = database_store.prepare_db_only_mutation(
                scope=idempotency_scope(identity),
                route_path=_PRODUCT_PREVIEW_TLS_APPLY_ROUTE,
                idempotency_key=normalized_idempotency_key,
                request_fingerprint=payload_fingerprint,
            )
            if preflight.status in {"missing", "released"}:
                return None
            if preflight.record is None:
                raise RuntimeError("Product preview TLS mutation preflight requires evidence.")
            if preflight.status == "replayed":
                return replay_idempotent_response(
                    trace_id=trace_id,
                    stored_record=preflight.record,
                    route_path=_PRODUCT_PREVIEW_TLS_APPLY_ROUTE,
                )
            if preflight.status == "conflict":
                raise _launchplane_http_error(
                    status_code=409,
                    trace_id=trace_id,
                    code="idempotency_key_reused",
                    message=(
                        "Idempotency-Key was already used for a different "
                        "Launchplane request payload on this route."
                    ),
                )
            if preflight.status == "in_progress":
                raise _launchplane_http_error(
                    status_code=409,
                    trace_id=trace_id,
                    code="mutation_in_progress",
                    message=(
                        "A matching product preview TLS mutation is already running. "
                        "Retry with the same Idempotency-Key."
                    ),
                )
            if preflight.status == "reconcile_required":
                raise _launchplane_http_error(
                    status_code=409,
                    trace_id=trace_id,
                    code="mutation_reconciliation_required",
                    message=(
                        "The product preview TLS mutation requires reconciliation before retry."
                    ),
                )
            raise RuntimeError(
                f"Unsupported product preview TLS mutation preflight status: {preflight.status}"
            )

        if preview_tls_request.mode == "apply":
            payload_fingerprint = idempotency_request_fingerprint(
                route_path=_PRODUCT_PREVIEW_TLS_APPLY_ROUTE,
                payload=raw_payload,
            )
            replay_response = prepare_product_preview_tls_mutation()
            if replay_response is not None:
                return replay_response
        try:
            profile = database_store.read_product_profile_record(preview_tls_request.product)
        except FileNotFoundError as error:
            raise _launchplane_http_error(
                status_code=404,
                trace_id=trace_id,
                code="not_found",
                message=str(error),
            ) from error
        try:
            plan = control_plane_product_preview_tls.build_product_preview_tls_plan(
                profile=profile,
                request=preview_tls_request,
            )
        except control_plane_product_preview_tls.ProductPreviewTlsDriverError as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="unsupported_product_driver",
                message=str(error),
            ) from error
        except ValueError as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Product preview TLS request does not match the stored profile.",
            ) from error
        if (
            preview_tls_request.mode == "apply"
            and preview_tls_request.reviewed_plan_sha256 != plan.plan_sha256
        ):
            replay_response = prepare_product_preview_tls_mutation()
            if replay_response is not None:
                return replay_response
            raise _launchplane_http_error(
                status_code=409,
                trace_id=trace_id,
                code="stale",
                message="Reviewed product preview TLS plan no longer matches the stored profile.",
            )
        if preview_tls_request.mode == "apply":
            replacement_profile = profile
            profile_updated_at_after = profile.updated_at
            if plan.changed:
                try:
                    replacement_profile = (
                        control_plane_product_preview_tls.updated_product_preview_tls_profile(
                            profile=profile,
                            request=preview_tls_request,
                            updated_at=utc_now_timestamp(),
                        )
                    )
                except ValueError as error:
                    raise _launchplane_http_error(
                        status_code=400,
                        trace_id=trace_id,
                        code="invalid_product_profile",
                        message="Updated product profile failed validation.",
                    ) from error
                profile_updated_at_after = replacement_profile.updated_at
            result_plan = plan.model_copy(
                update={
                    "applied": True,
                    "profile_updated_at_after": profile_updated_at_after,
                }
            )
            response = accepted_evidence_response(
                trace_id=trace_id,
                records={"product_profile": preview_tls_request.product},
                result=result_plan.model_dump(mode="json"),
            )
            mutation = DbOnlyMutationRequest(
                scope=idempotency_scope(identity),
                route_path=_PRODUCT_PREVIEW_TLS_APPLY_ROUTE,
                idempotency_key=normalized_idempotency_key,
                request_fingerprint=payload_fingerprint,
                lease_owner=trace_id,
                response_status_code=202,
                response_trace_id=trace_id,
                response_payload=response.model_dump(mode="json", exclude_none=True),
                lease_seconds=int(_DB_ONLY_MUTATION_LEASE.total_seconds()),
            )
            write_result = database_store.compare_and_write_product_profile_record(
                expected_record=profile,
                replacement_record=replacement_profile,
                mutation=mutation,
            )
            if write_result.status == "replayed":
                if write_result.idempotency_record is None:
                    raise RuntimeError("Replayed product profile write requires evidence.")
                return replay_idempotent_response(
                    trace_id=trace_id,
                    stored_record=write_result.idempotency_record,
                    route_path=_PRODUCT_PREVIEW_TLS_APPLY_ROUTE,
                )
            if write_result.status == "idempotency_conflict":
                raise _launchplane_http_error(
                    status_code=409,
                    trace_id=trace_id,
                    code="idempotency_key_reused",
                    message=(
                        "Idempotency-Key was already used for a different "
                        "Launchplane request payload on this route."
                    ),
                )
            if write_result.status == "missing":
                raise _launchplane_http_error(
                    status_code=404,
                    trace_id=trace_id,
                    code="not_found",
                    message=(
                        "Product profile disappeared before the preview TLS change could be applied."
                    ),
                )
            if write_result.status == "changed":
                raise _launchplane_http_error(
                    status_code=409,
                    trace_id=trace_id,
                    code="stale",
                    message=(
                        "Product profile changed while applying the reviewed preview TLS plan."
                    ),
                )
            if write_result.status == "reservation_in_progress":
                raise _launchplane_http_error(
                    status_code=409,
                    trace_id=trace_id,
                    code="mutation_in_progress",
                    message=(
                        "A matching product preview TLS mutation is already running. "
                        "Retry with the same Idempotency-Key."
                    ),
                )
            if write_result.status == "reconciliation_required":
                raise _launchplane_http_error(
                    status_code=409,
                    trace_id=trace_id,
                    code="mutation_reconciliation_required",
                    message=(
                        "The product preview TLS mutation requires reconciliation before retry."
                    ),
                )
            return response
        result_plan = plan
        response = accepted_evidence_response(
            trace_id=trace_id,
            records={"product_profile": preview_tls_request.product},
            result=result_plan.model_dump(mode="json"),
        )
        return response

    async def apply_product_stable_lane_repair(
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
                message="Product stable lane repair request failed validation.",
            ) from error
        if not isinstance(raw_payload, dict):
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Product stable lane repair request failed validation.",
            )
        try:
            repair_request = control_plane_product_stable_lane_repair.ProductStableLaneRepairRequest.model_validate(
                raw_payload
            )
        except ValidationError as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Product stable lane repair request failed validation.",
            ) from error
        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action="product_onboarding.apply",
            product="launchplane",
            context=_LAUNCHPLANE_SERVICE_CONTEXT,
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Workflow cannot plan or apply product stable lane repair.",
            )
        normalized_idempotency_key = idempotency_key.strip()
        if repair_request.mode == "apply" and not normalized_idempotency_key:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="idempotency_key_required",
                message="Product stable lane repair apply requires an Idempotency-Key header.",
            )
        database_store = require_product_stable_lane_repair_database_store(
            record_store=record_store,
            trace_id=trace_id,
        )
        payload_fingerprint = ""

        def prepare_product_stable_lane_repair_mutation() -> AcceptedEvidenceResponse | None:
            preflight = database_store.prepare_db_only_mutation(
                scope=idempotency_scope(identity),
                route_path=_PRODUCT_STABLE_LANE_REPAIR_APPLY_ROUTE,
                idempotency_key=normalized_idempotency_key,
                request_fingerprint=payload_fingerprint,
            )
            if preflight.status in {"missing", "released"}:
                return None
            if preflight.record is None:
                raise RuntimeError("Product stable lane repair preflight requires evidence.")
            if preflight.status == "replayed":
                return replay_idempotent_response(
                    trace_id=trace_id,
                    stored_record=preflight.record,
                    route_path=_PRODUCT_STABLE_LANE_REPAIR_APPLY_ROUTE,
                )
            if preflight.status == "conflict":
                raise _launchplane_http_error(
                    status_code=409,
                    trace_id=trace_id,
                    code="idempotency_key_reused",
                    message=(
                        "Idempotency-Key was already used for a different Launchplane request "
                        "payload on this route."
                    ),
                )
            if preflight.status == "in_progress":
                raise _launchplane_http_error(
                    status_code=409,
                    trace_id=trace_id,
                    code="mutation_in_progress",
                    message=(
                        "A matching product stable lane repair is already running. Retry with "
                        "the same Idempotency-Key."
                    ),
                )
            if preflight.status == "reconcile_required":
                raise _launchplane_http_error(
                    status_code=409,
                    trace_id=trace_id,
                    code="mutation_reconciliation_required",
                    message="The product stable lane repair requires reconciliation before retry.",
                )
            raise RuntimeError(
                f"Unsupported product stable lane repair preflight status: {preflight.status}"
            )

        if repair_request.mode == "apply":
            payload_fingerprint = idempotency_request_fingerprint(
                route_path=_PRODUCT_STABLE_LANE_REPAIR_APPLY_ROUTE,
                payload=raw_payload,
            )
            replay_response = prepare_product_stable_lane_repair_mutation()
            if replay_response is not None:
                return replay_response
        try:
            (
                plan,
                profile,
                replacement_profile,
                provider_target,
                dokploy_target,
                dokploy_target_id,
            ) = control_plane_product_stable_lane_repair.build_product_stable_lane_repair_plan(
                record_store=database_store,
                request=repair_request,
            )
        except FileNotFoundError as error:
            raise _launchplane_http_error(
                status_code=404,
                trace_id=trace_id,
                code="not_found",
                message=str(error),
            ) from error
        except (
            control_plane_product_stable_lane_repair.ProductStableLaneRepairBoundaryError
        ) as error:
            raise _launchplane_http_error(
                status_code=409,
                trace_id=trace_id,
                code="stable_lane_repair_blocked",
                message=str(error),
            ) from error
        except (ValueError, ValidationError) as error:
            raise _launchplane_http_error(
                status_code=409,
                trace_id=trace_id,
                code="stable_lane_repair_blocked",
                message="Product stable lane repair cannot produce a valid profile transition.",
            ) from error
        if (
            repair_request.mode == "apply"
            and repair_request.reviewed_plan_sha256 != plan.plan_sha256
        ):
            replay_response = prepare_product_stable_lane_repair_mutation()
            if replay_response is not None:
                return replay_response
            raise _launchplane_http_error(
                status_code=409,
                trace_id=trace_id,
                code="stale",
                message="Reviewed product stable lane repair plan no longer matches authority.",
            )
        if repair_request.mode == "dry-run":
            return accepted_evidence_response(
                trace_id=trace_id,
                records={
                    "product_profile": repair_request.product,
                    "context": repair_request.context,
                    "instance": repair_request.instance,
                },
                result=plan.model_dump(mode="json"),
            )
        updated_profile = (
            control_plane_product_stable_lane_repair.updated_product_stable_lane_repair_profile(
                replacement_profile=replacement_profile,
                updated_at=utc_now_timestamp(),
            )
        )
        applied_plan = plan.model_copy(
            update={
                "applied": True,
                "profile_sha256_after": product_profile_record_sha256(updated_profile),
                "profile_updated_at_after": updated_profile.updated_at,
            }
        )
        response = accepted_evidence_response(
            trace_id=trace_id,
            records={
                "product_profile": repair_request.product,
                "context": repair_request.context,
                "instance": repair_request.instance,
            },
            result=applied_plan.model_dump(mode="json"),
        )
        mutation = DbOnlyMutationRequest(
            scope=idempotency_scope(identity),
            route_path=_PRODUCT_STABLE_LANE_REPAIR_APPLY_ROUTE,
            idempotency_key=normalized_idempotency_key,
            request_fingerprint=payload_fingerprint,
            lease_owner=trace_id,
            response_status_code=202,
            response_trace_id=trace_id,
            response_payload=response.model_dump(mode="json", exclude_none=True),
            lease_seconds=int(_DB_ONLY_MUTATION_LEASE.total_seconds()),
        )
        write_result = database_store.compare_and_write_product_profile_record(
            expected_record=profile,
            replacement_record=updated_profile,
            mutation=mutation,
            expected_provider_targets=(provider_target,),
            expected_dokploy_targets=(dokploy_target,),
            expected_dokploy_target_ids=(dokploy_target_id,),
        )
        if write_result.status == "replayed":
            if write_result.idempotency_record is None:
                raise RuntimeError("Replayed product stable lane repair requires evidence.")
            return replay_idempotent_response(
                trace_id=trace_id,
                stored_record=write_result.idempotency_record,
                route_path=_PRODUCT_STABLE_LANE_REPAIR_APPLY_ROUTE,
            )
        if write_result.status == "idempotency_conflict":
            raise _launchplane_http_error(
                status_code=409,
                trace_id=trace_id,
                code="idempotency_key_reused",
                message=(
                    "Idempotency-Key was already used for a different Launchplane request "
                    "payload on this route."
                ),
            )
        if write_result.status == "missing":
            raise _launchplane_http_error(
                status_code=404,
                trace_id=trace_id,
                code="not_found",
                message="Product profile disappeared before stable lane repair could apply.",
            )
        if write_result.status == "changed":
            raise _launchplane_http_error(
                status_code=409,
                trace_id=trace_id,
                code="stale",
                message=(
                    "Product profile or tracked target authority changed while applying the "
                    "reviewed stable lane repair plan."
                ),
            )
        if write_result.status == "reservation_in_progress":
            raise _launchplane_http_error(
                status_code=409,
                trace_id=trace_id,
                code="mutation_in_progress",
                message=(
                    "A matching product stable lane repair is already running. Retry with the "
                    "same Idempotency-Key."
                ),
            )
        if write_result.status == "reconciliation_required":
            raise _launchplane_http_error(
                status_code=409,
                trace_id=trace_id,
                code="mutation_reconciliation_required",
                message="The product stable lane repair requires reconciliation before retry.",
            )
        return response

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

    def require_product_config_database_store(
        *, record_store: object, trace_id: str
    ) -> PostgresRecordStore:
        if not isinstance(record_store, PostgresRecordStore):
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_required",
                message="Product config apply requires DB-backed Launchplane storage.",
            )
        return record_store

    def require_product_onboarding_database_store(
        *, record_store: object, trace_id: str
    ) -> PostgresRecordStore:
        if not isinstance(record_store, PostgresRecordStore):
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_required",
                message="Product onboarding writes require Launchplane database storage.",
            )
        return record_store

    def require_product_preview_tls_database_store(
        *, record_store: object, trace_id: str
    ) -> PostgresRecordStore:
        if not isinstance(record_store, PostgresRecordStore):
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_required",
                message="Product preview TLS writes require Launchplane database storage.",
            )
        return record_store

    def require_product_stable_lane_repair_database_store(
        *, record_store: object, trace_id: str
    ) -> PostgresRecordStore:
        if not isinstance(record_store, PostgresRecordStore):
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_required",
                message="Product stable lane repair requires Launchplane database storage.",
            )
        return record_store

    def require_product_health_monitoring_database_store(
        *, record_store: object, trace_id: str
    ) -> PostgresRecordStore:
        if not isinstance(record_store, PostgresRecordStore):
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_required",
                message="Product health monitoring writes require Launchplane database storage.",
            )
        return record_store

    def require_product_prelaunch_rebuild_policy_database_store(
        *, record_store: object, trace_id: str
    ) -> PostgresRecordStore:
        if not isinstance(record_store, PostgresRecordStore):
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_required",
                message=(
                    "Product prelaunch rebuild policy writes require Launchplane database storage."
                ),
            )
        return record_store

    def require_merge_train_policy_database_store(
        *, record_store: object, trace_id: str
    ) -> PostgresRecordStore:
        if not isinstance(record_store, PostgresRecordStore):
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_required",
                message="Merge train policy writes require Launchplane database storage.",
            )
        return record_store

    def require_authz_policy_database_store(
        *, record_store: object, trace_id: str, message: str
    ) -> PostgresRecordStore:
        if not isinstance(record_store, PostgresRecordStore):
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_required",
                message=message,
            )
        return record_store

    def require_live_target_runtime_database_store(
        *, record_store: object, trace_id: str
    ) -> PostgresRecordStore:
        if not isinstance(record_store, PostgresRecordStore):
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_required",
                message="Live target runtime apply requires DB-backed Launchplane storage.",
            )
        return record_store

    def require_provider_target_operation_database_store(
        *, record_store: object, trace_id: str
    ) -> PostgresRecordStore:
        if not isinstance(record_store, PostgresRecordStore):
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_required",
                message="Provider-target operations require Launchplane database storage.",
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
        payload_fingerprint = build_request_fingerprint(raw_payload)
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
                if stored_record.state == "running":
                    raise _launchplane_http_error(
                        status_code=409,
                        trace_id=trace_id,
                        code="mutation_in_progress",
                        message=(
                            "A matching Launchplane mutation is already running. "
                            "Retry with the same Idempotency-Key."
                        ),
                    )
                if stored_record.state == "reconcile_required":
                    raise _launchplane_http_error(
                        status_code=409,
                        trace_id=trace_id,
                        code="mutation_reconciliation_required",
                        message=(
                            "The prior Launchplane mutation requires reconciliation before retry."
                        ),
                    )
                return (
                    normalized_idempotency_key,
                    payload_fingerprint,
                    replay_idempotent_response(
                        trace_id=trace_id,
                        stored_record=stored_record,
                        route_path=route_path,
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

    def replay_stored_apply_idempotency(
        *,
        record_store: object,
        identity: LaunchplaneIdentity,
        route_path: str,
        idempotency_key: str,
        request_fingerprint_value: str,
        trace_id: str,
        idempotency_scope_override: str = "",
    ) -> AcceptedEvidenceResponse | None:
        idempotency_store = idempotency_capable_store(record_store)
        if idempotency_store is None or not idempotency_key:
            return None
        stored_record = idempotency_store.read_idempotency_record(
            scope=idempotency_scope_override.strip() or idempotency_scope(identity),
            route_path=route_path,
            idempotency_key=idempotency_key,
        )
        if stored_record is None:
            return None
        if stored_record.request_fingerprint != request_fingerprint_value:
            raise _launchplane_http_error(
                status_code=409,
                trace_id=trace_id,
                code="idempotency_key_reused",
                message=(
                    "Idempotency-Key was already used for a different "
                    "Launchplane request payload on this route."
                ),
            )
        if stored_record.state == "running":
            raise _launchplane_http_error(
                status_code=409,
                trace_id=trace_id,
                code="mutation_in_progress",
                message=(
                    "A matching Launchplane mutation is already running. "
                    "Retry with the same Idempotency-Key."
                ),
            )
        if stored_record.state == "reconcile_required":
            raise _launchplane_http_error(
                status_code=409,
                trace_id=trace_id,
                code="mutation_reconciliation_required",
                message="The prior Launchplane mutation requires reconciliation before retry.",
            )
        return replay_idempotent_response(
            trace_id=trace_id,
            stored_record=stored_record,
            route_path=route_path,
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
        request_payload: dict[str, object] | None = None,
        idempotency_scope_override: str = "",
    ) -> tuple[str, str, AcceptedEvidenceResponse | None]:
        normalized_idempotency_key = idempotency_key.strip()
        raw_payload = request_payload if request_payload is not None else await request.json()
        payload_fingerprint = idempotency_request_fingerprint(
            route_path=route_path,
            payload=raw_payload,
        )
        if normalized_idempotency_key and check_replay:
            replay_response = replay_stored_apply_idempotency(
                record_store=record_store,
                identity=identity,
                route_path=route_path,
                idempotency_key=normalized_idempotency_key,
                request_fingerprint_value=payload_fingerprint,
                trace_id=trace_id,
                idempotency_scope_override=idempotency_scope_override,
            )
            if replay_response is not None:
                return normalized_idempotency_key, payload_fingerprint, replay_response
        return normalized_idempotency_key, payload_fingerprint, None

    def build_apply_idempotency_record(
        *,
        identity: LaunchplaneIdentity,
        route_path: str,
        idempotency_key: str,
        request_fingerprint_value: str,
        trace_id: str,
        response: BaseModel,
    ) -> LaunchplaneIdempotencyRecord:
        return LaunchplaneIdempotencyRecord(
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

    def store_apply_idempotency(
        *,
        record_store: object,
        identity: LaunchplaneIdentity,
        route_path: str,
        idempotency_key: str,
        request_fingerprint_value: str,
        trace_id: str,
        response: BaseModel,
    ) -> None:
        idempotency_store = idempotency_capable_store(record_store)
        if idempotency_store is None or not idempotency_key:
            return
        idempotency_store.write_idempotency_record(
            build_apply_idempotency_record(
                identity=identity,
                route_path=route_path,
                idempotency_key=idempotency_key,
                request_fingerprint_value=request_fingerprint_value,
                trace_id=trace_id,
                response=response,
            )
        )

    def authority_bundle_with_apply_idempotency(
        *,
        bundle: ProductAuthorityBundle,
        identity: LaunchplaneIdentity,
        route_path: str,
        idempotency_key: str,
        request_fingerprint_value: str,
        trace_id: str,
        response: BaseModel,
    ) -> ProductAuthorityBundle:
        if not idempotency_key.strip():
            return bundle
        return bundle.model_copy(
            update={
                "idempotency_record": build_apply_idempotency_record(
                    identity=identity,
                    route_path=route_path,
                    idempotency_key=idempotency_key,
                    request_fingerprint_value=request_fingerprint_value,
                    trace_id=trace_id,
                    response=response,
                )
            }
        )

    def require_provider_operation_store(
        *, record_store: object, trace_id: str
    ) -> PostgresRecordStore:
        if not isinstance(record_store, PostgresRecordStore):
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message="Durable provider operations require Launchplane database storage.",
            )
        return record_store

    def provider_mutation_http_response(
        *,
        result: DurableProviderOperationResult,
        trace_id: str,
        route_path: str,
        in_progress_message: str,
        reconcile_message: str,
    ) -> AcceptedEvidenceResponse:
        if result.status in {"completed", "unstored", "adopted"}:
            if result.response_status_code >= 400:
                raise _launchplane_http_error(
                    status_code=result.response_status_code,
                    trace_id=trace_id,
                    code="provider_mutation_failed",
                    message=_provider_mutation_failure_message(result.response_payload),
                )
            return AcceptedEvidenceResponse.model_validate(result.response_payload)
        if result.status == "replayed":
            if result.record is None:
                raise RuntimeError("Replayed provider mutation requires a stored record.")
            if (result.record.response_status_code or 0) >= 400:
                raise _launchplane_http_error(
                    status_code=result.record.response_status_code or 502,
                    trace_id=trace_id,
                    code="provider_mutation_failed",
                    message=_provider_mutation_failure_message(result.record.response_payload),
                )
            return replay_idempotent_response(
                trace_id=trace_id,
                stored_record=result.record,
                route_path=route_path,
            )
        if result.status == "conflict":
            raise _launchplane_http_error(
                status_code=409,
                trace_id=trace_id,
                code="idempotency_key_reused",
                message=(
                    "Idempotency-Key was already used for a different "
                    "Launchplane request payload on this route."
                ),
            )
        if result.status in {"in_progress", "target_busy"}:
            raise _launchplane_http_error(
                status_code=409,
                trace_id=trace_id,
                code="mutation_in_progress",
                message=in_progress_message,
            )
        if result.status == "reconcile_required":
            raise _launchplane_http_error(
                status_code=409,
                trace_id=trace_id,
                code="mutation_reconciliation_required",
                message=reconcile_message,
            )
        raise RuntimeError(f"Unsupported provider mutation status: {result.status}")

    def _provider_mutation_failure_message(response_payload: Mapping[str, object]) -> str:
        result_payload = response_payload.get("result")
        if isinstance(result_payload, Mapping):
            error_message = str(result_payload.get("error_message") or "").strip()
            if error_message:
                return error_message
        return "Provider mutation completed with terminal failure evidence."

    async def run_provider_mutation(
        *,
        record_store: object,
        identity: LaunchplaneIdentity,
        route_path: str,
        idempotency_key: str,
        request_fingerprint: str,
        trace_id: str,
        adapter: DurableProviderMutationAdapter,
        in_progress_message: str,
        reconcile_message: str,
        reservation_scope: str = "",
        target_supersession: ProviderTargetSupersession | None = None,
    ) -> AcceptedEvidenceResponse:
        reservation_store = require_provider_operation_store(
            record_store=record_store,
            trace_id=trace_id,
        )

        def execute() -> DurableProviderOperationResult:
            return run_durable_provider_operation(
                store=reservation_store,
                scope=reservation_scope.strip() or idempotency_scope(identity),
                route_path=route_path,
                idempotency_key=idempotency_key,
                request_fingerprint=request_fingerprint,
                lease_owner=trace_id,
                response_trace_id=trace_id,
                adapter=adapter,
                target_supersession=target_supersession,
            )

        operation_task = asyncio.create_task(
            asyncio.to_thread(execute),
            name=f"provider-mutation:{trace_id}",
        )
        try:
            result = await asyncio.shield(operation_task)
        except asyncio.CancelledError as cancellation:
            while not operation_task.done():
                try:
                    await asyncio.shield(asyncio.wait({operation_task}))
                except asyncio.CancelledError:
                    continue
            operation_error = operation_task.exception()
            if operation_error is not None:
                _LOGGER.error(
                    "Provider mutation failed after request cancellation",
                    exc_info=(
                        type(operation_error),
                        operation_error,
                        operation_error.__traceback__,
                    ),
                    extra={"trace_id": trace_id},
                )
            raise cancellation
        return provider_mutation_http_response(
            result=result,
            trace_id=trace_id,
            route_path=route_path,
            in_progress_message=in_progress_message,
            reconcile_message=reconcile_message,
        )

    async def execute_product_config_request(
        *,
        request: Request,
        identity: LaunchplaneIdentity,
        record_store: object,
        idempotency_key: str,
        trace_id: str,
        route_path: str,
        product_config_request: ProductConfigApplyEnvelope,
        expected_confirmation: str = "",
    ) -> ProductConfigApplyResponse:
        if isinstance(identity, TerminalAgentIdentity):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Terminal agent credentials can only read redacted Launchplane context.",
            )
        operator_identity = isinstance(
            identity, GitHubHumanIdentity | LocalOperatorIdentity | LocalAdminIdentity
        )
        action = (
            "product_config.apply"
            if product_config_request.mode == "apply"
            else "product_config.plan"
        )
        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action=action,
            product=product_config_request.product,
            context=product_config_request.context,
            target=AuthorizationTarget(
                scope="instance" if product_config_request.instance else "context",
                instances=(product_config_request.instance,)
                if product_config_request.instance
                else (),
            ),
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message=(
                    "Workflow cannot plan or apply product config for the requested"
                    " product/context."
                ),
            )
        if operator_identity and not product_config_request.reason:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="reason_required",
                message="Operator product-config requests require a reason.",
            )
        normalized_idempotency_key = idempotency_key.strip()
        if (
            operator_identity
            and product_config_request.mode == "apply"
            and not normalized_idempotency_key
        ):
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="idempotency_key_required",
                message="Operator product-config apply requires an Idempotency-Key header.",
            )
        if (
            product_config_request.mode == "apply"
            and expected_confirmation
            and product_config_request.confirmation != expected_confirmation
        ):
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="confirmation_required",
                message="Product config apply requires the exact environment confirmation.",
            )
        try:
            request_payload = canonical_product_config_request_payload(
                product_config_request.model_dump(mode="json", exclude_none=True)
            )
        except control_plane_product_config.ProductConfigError as error:
            product_config_error = (
                control_plane_product_config_service.product_config_service_error(error)
            )
            raise _launchplane_http_error(
                status_code=product_config_error.status_code,
                trace_id=trace_id,
                code=product_config_error.code,
                message=product_config_error.message,
            ) from error
        database_store = require_product_config_database_store(
            record_store=record_store,
            trace_id=trace_id,
        )
        try:
            (
                normalized_idempotency_key,
                payload_fingerprint,
                replay_response,
            ) = await replay_apply_idempotency(
                request=request,
                record_store=database_store,
                identity=identity,
                route_path=route_path,
                idempotency_key=idempotency_key,
                trace_id=trace_id,
                check_replay=bool(idempotency_key.strip()),
                request_payload=request_payload,
            )
        except click.ClickException as error:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="secret_configuration_required",
                message="Launchplane service is missing required secret write configuration.",
            ) from error
        if replay_response is not None:
            return ProductConfigApplyResponse.model_validate(
                replay_response.model_dump(mode="json")
            )
        if (
            operator_identity
            and product_config_request.mode == "apply"
            and not product_config_dry_run_exists(
                record_store=database_store,
                identity=identity,
                request_payload=request_payload,
                route_path=route_path,
            )
        ):
            raise _launchplane_http_error(
                status_code=409,
                trace_id=trace_id,
                code="matching_dry_run_required",
                message="Operator product-config apply requires a prior matching dry-run.",
            )
        try:
            planned_driver_result, authority_bundle = (
                control_plane_product_config.plan_product_config_authority_bundle(
                    record_store=database_store,
                    payload=product_config_request.product_config_payload(),
                    mode=product_config_request.mode,
                    actor=launchplane_identity_actor(identity),
                    source_label=product_config_request.source_label,
                )
            )
        except control_plane_product_config.ProductConfigError as error:
            product_config_error = (
                control_plane_product_config_service.product_config_service_error(error)
            )
            raise _launchplane_http_error(
                status_code=product_config_error.status_code,
                trace_id=trace_id,
                code=product_config_error.code,
                message=product_config_error.message,
            )
        driver_result: dict[str, object] = {
            **planned_driver_result,
            "reason": product_config_request.reason,
        }
        next_actions = product_config_live_target_next_actions(
            request=product_config_request,
            driver_result=driver_result,
            tracked_targets=database_store.list_dokploy_target_records(),
        )
        if next_actions:
            driver_result = {
                **driver_result,
                "next_actions": next_actions,
            }
            if product_config_request.mode == "apply":
                driver_result["status"] = "records_applied_live_sync_required"
        product_config_response = ProductConfigApplyResponse(
            trace_id=trace_id,
            records={},
            result=ProductConfigApplyResult.model_validate(driver_result),
        )
        if operator_identity and product_config_request.mode == "dry-run":
            store_product_config_dry_run_record(
                record_store=database_store,
                identity=identity,
                request_payload=request_payload,
                trace_id=trace_id,
                response=product_config_response,
                route_path=route_path,
            )
        if product_config_request.mode == "dry-run":
            store_apply_idempotency(
                record_store=database_store,
                identity=identity,
                route_path=route_path,
                idempotency_key=normalized_idempotency_key,
                request_fingerprint_value=payload_fingerprint,
                trace_id=trace_id,
                response=product_config_response,
            )
        else:
            try:
                database_store.write_product_authority_bundle(
                    authority_bundle_with_apply_idempotency(
                        bundle=authority_bundle,
                        identity=identity,
                        route_path=route_path,
                        idempotency_key=normalized_idempotency_key,
                        request_fingerprint_value=payload_fingerprint,
                        trace_id=trace_id,
                        response=product_config_response,
                    )
                )
            except Exception as write_error:
                if not normalized_idempotency_key:
                    raise
                try:
                    stored_record = database_store.read_idempotency_record(
                        scope=idempotency_scope(identity),
                        route_path=route_path,
                        idempotency_key=normalized_idempotency_key,
                    )
                except Exception as read_error:
                    raise write_error from read_error
                if (
                    stored_record is None
                    or stored_record.state != "completed"
                    or stored_record.request_fingerprint != payload_fingerprint
                ):
                    raise write_error
                replay_response = replay_idempotent_response(
                    trace_id=trace_id,
                    stored_record=stored_record,
                    route_path=route_path,
                )
                return ProductConfigApplyResponse.model_validate(
                    replay_response.model_dump(mode="json")
                )
        return product_config_response

    async def apply_product_config(
        request: Request,
        identity: Annotated[LaunchplaneIdentity, Depends(read_browser_mutation_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> ProductConfigApplyResponse:
        trace_id = next_trace_id()
        if isinstance(identity, TerminalAgentIdentity):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Terminal agent credentials can only read redacted Launchplane context.",
            )
        try:
            raw_payload = await request.json()
            if not isinstance(raw_payload, dict):
                raise ValueError("Product config request body must be an object.")
            product_config_request = ProductConfigApplyEnvelope.model_validate(raw_payload)
        except (ValidationError, ValueError) as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Product config request failed validation.",
            ) from error
        return await execute_product_config_request(
            request=request,
            identity=identity,
            record_store=record_store,
            idempotency_key=idempotency_key,
            trace_id=trace_id,
            route_path=_PRODUCT_CONFIG_APPLY_ROUTE,
            product_config_request=product_config_request,
        )

    async def apply_product_environment_config(
        product: str,
        environment: str,
        request: Request,
        identity: Annotated[LaunchplaneIdentity, Depends(read_browser_mutation_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> ProductConfigApplyResponse:
        trace_id = next_trace_id()
        if isinstance(identity, TerminalAgentIdentity):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Terminal agent credentials can only read redacted Launchplane context.",
            )
        if not isinstance(
            identity, GitHubHumanIdentity | LocalOperatorIdentity | LocalAdminIdentity
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Product environment config writes require an operator identity.",
            )
        try:
            raw_payload = await request.json()
            if not isinstance(raw_payload, dict):
                raise ValueError("Product environment config request body must be an object.")
            environment_request = ProductEnvironmentConfigApplyEnvelope.model_validate(raw_payload)
        except (ValidationError, ValueError) as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Product config request failed validation.",
            ) from error
        database_store = require_product_config_database_store(
            record_store=record_store,
            trace_id=trace_id,
        )
        try:
            profile = database_store.read_product_profile_record(product.strip())
            lane = next(
                candidate
                for candidate in profile.lanes
                if candidate.instance == environment.strip()
            )
        except (FileNotFoundError, StopIteration) as error:
            raise _launchplane_http_error(
                status_code=404,
                trace_id=trace_id,
                code="not_found",
                message="Product environment was not found.",
            ) from error
        action = (
            "product_config.apply" if environment_request.mode == "apply" else "product_config.plan"
        )
        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action=action,
            product=profile.product,
            context=lane.context,
            target=AuthorizationTarget(scope="instance", instances=(lane.instance,)),
        ):
            raise _launchplane_http_error(
                status_code=404,
                trace_id=trace_id,
                code="not_found",
                message="Product environment was not found.",
            )
        try:
            product_config_request = product_environment_config_apply_request(
                profile=profile,
                lane=lane,
                request=environment_request,
            )
        except (ValidationError, ValueError) as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Product config request failed validation.",
            ) from error
        return await execute_product_config_request(
            request=request,
            identity=identity,
            record_store=database_store,
            idempotency_key=idempotency_key,
            trace_id=trace_id,
            route_path=_PRODUCT_ENVIRONMENT_CONFIG_APPLY_ROUTE,
            product_config_request=product_config_request,
            expected_confirmation=product_environment_config_confirmation(
                product=profile.product,
                environment=lane.instance,
            ),
        )

    def require_product_promotion_operator(
        *,
        identity: LaunchplaneIdentity,
        trace_id: str,
    ) -> None:
        if not isinstance(
            identity, GitHubHumanIdentity | LocalOperatorIdentity | LocalAdminIdentity
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Product promotion browser routes require an operator identity.",
            )

    def resolve_authorized_product_promotion_target(
        *,
        record_store: object,
        identity: LaunchplaneIdentity,
        product: str,
        environment: str,
        action: str,
        trace_id: str,
    ) -> tuple[LaunchplaneProductProfileRecord, ProductLaneProfile]:
        try:
            profile, lane = resolve_product_promotion_target(
                record_store=record_store,
                product=product,
                destination_environment=environment,
            )
        except (AttributeError, FileNotFoundError) as error:
            raise _launchplane_http_error(
                status_code=404,
                trace_id=trace_id,
                code="not_found",
                message="Product promotion target was not found.",
            ) from error
        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action=action,
            product=profile.product,
            context=lane.context,
            target=AuthorizationTarget(scope="instance", instances=(lane.instance,)),
        ):
            raise _launchplane_http_error(
                status_code=404,
                trace_id=trace_id,
                code="not_found",
                message="Product promotion target was not found.",
            )
        return profile, lane

    def current_product_promotion_status(
        *,
        record_store: object,
        identity: LaunchplaneIdentity,
        product: str,
        environment: str,
        trace_id: str,
    ) -> tuple[LaunchplaneProductProfileRecord, ProductLaneProfile, ProductPromotionStatus]:
        try:
            return build_product_promotion_status(
                record_store=record_store,
                product=product,
                destination_environment=environment,
                action_allowed=lambda action, requested_product, context, instances: (
                    resolved_authz_policy_runtime.policy.allows(
                        identity=identity,
                        action=action,
                        product=requested_product,
                        context=context,
                        target=AuthorizationTarget(
                            scope="instance",
                            instances=instances,
                        )
                        if instances
                        else AuthorizationTarget(scope="context"),
                    )
                ),
                workflow_credentials_ready=lambda context: bool(
                    resolve_launchplane_github_token(
                        control_plane_root=resolved_control_plane_root,
                        context_name=context,
                    )
                ),
            )
        except (AttributeError, FileNotFoundError) as error:
            raise _launchplane_http_error(
                status_code=404,
                trace_id=trace_id,
                code="not_found",
                message="Product promotion target was not found.",
            ) from error
        except (ValueError, click.ClickException) as error:
            raise _launchplane_http_error(
                status_code=409,
                trace_id=trace_id,
                code="promotion_evidence_invalid",
                message="Current product promotion evidence is invalid.",
            ) from error

    async def dry_run_product_promotion(
        product: str,
        environment: str,
        request: Request,
        promotion_request: ProductPromotionDryRunEnvelope,
        identity: Annotated[LaunchplaneIdentity, Depends(read_browser_mutation_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> ProductPromotionDryRunResponse:
        trace_id = next_trace_id()
        require_product_promotion_operator(identity=identity, trace_id=trace_id)
        profile, lane = resolve_authorized_product_promotion_target(
            record_store=record_store,
            identity=identity,
            product=product,
            environment=environment,
            action="generic_web_prod_promotion.execute",
            trace_id=trace_id,
        )
        normalized_key = idempotency_key.strip()
        if not normalized_key:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="idempotency_key_required",
                message="Product promotion dry-run requires an Idempotency-Key header.",
            )
        request_payload = product_promotion_request_payload(
            product=profile.product,
            destination_environment=lane.instance,
            request=promotion_request,
        )
        normalized_key, payload_fingerprint, replay_response = await replay_apply_idempotency(
            request=request,
            record_store=record_store,
            identity=identity,
            route_path=_PRODUCT_PROMOTION_DRY_RUN_ROUTE,
            idempotency_key=normalized_key,
            trace_id=trace_id,
            check_replay=True,
            request_payload=request_payload,
        )
        if replay_response is not None:
            replayed = ProductPromotionDryRunResponse.model_validate(
                replay_response.model_dump(mode="json")
            )
            try:
                _, _, replay_status = current_product_promotion_status(
                    record_store=record_store,
                    identity=identity,
                    product=profile.product,
                    environment=lane.instance,
                    trace_id=trace_id,
                )
            except HTTPException:
                return replayed
            if (
                promotion_request.evidence_fingerprint == replay_status.evidence_fingerprint
                and replay_status.direct_dry_run.enabled
            ):
                store_product_promotion_dry_run_record(
                    record_store=record_store,
                    identity=identity,
                    status=replay_status,
                    bump=promotion_request.bump,
                    trace_id=replayed.original_trace_id or replayed.trace_id,
                    response=replayed,
                )
            return replayed
        profile, _, promotion_status = current_product_promotion_status(
            record_store=record_store,
            identity=identity,
            product=profile.product,
            environment=lane.instance,
            trace_id=trace_id,
        )
        if promotion_request.evidence_fingerprint != promotion_status.evidence_fingerprint:
            raise _launchplane_http_error(
                status_code=409,
                trace_id=trace_id,
                code="promotion_evidence_changed",
                message="Product promotion evidence changed. Refresh and review the current evidence.",
            )
        if not promotion_status.direct_dry_run.enabled:
            raise _launchplane_http_error(
                status_code=409,
                trace_id=trace_id,
                code="promotion_unavailable",
                message="Product promotion dry-run is blocked by current server evidence.",
            )
        try:
            records, result = execute_generic_web_prod_promotion_result(
                control_plane_root=resolved_control_plane_root,
                record_store=record_store,
                request=product_promotion_direct_request(
                    profile=profile,
                    status=promotion_status,
                ),
            )
        except FileNotFoundError as error:
            raise _launchplane_http_error(
                status_code=404,
                trace_id=trace_id,
                code="not_found",
                message="Product promotion route was not found.",
            ) from error
        except (ValueError, click.ClickException) as error:
            raise _launchplane_http_error(
                status_code=409,
                trace_id=trace_id,
                code="promotion_dry_run_failed",
                message="Product promotion dry-run could not validate the reviewed evidence.",
            ) from error
        response = ProductPromotionDryRunResponse(
            trace_id=trace_id,
            records=records,
            result=ProductPromotionDryRunResult.model_validate(
                {
                    **result.model_dump(mode="json"),
                    "evidence_fingerprint": promotion_status.evidence_fingerprint,
                    "bump": promotion_request.bump,
                }
            ),
        )
        try:
            store_apply_idempotency(
                record_store=record_store,
                identity=identity,
                route_path=_PRODUCT_PROMOTION_DRY_RUN_ROUTE,
                idempotency_key=normalized_key,
                request_fingerprint_value=payload_fingerprint,
                trace_id=trace_id,
                response=response,
            )
        except Exception as write_error:
            try:
                replay_response = replay_stored_apply_idempotency(
                    record_store=record_store,
                    identity=identity,
                    route_path=_PRODUCT_PROMOTION_DRY_RUN_ROUTE,
                    idempotency_key=normalized_key,
                    request_fingerprint_value=payload_fingerprint,
                    trace_id=trace_id,
                )
            except HTTPException:
                raise
            except Exception as read_error:
                raise write_error from read_error
            if replay_response is None:
                raise
            response = ProductPromotionDryRunResponse.model_validate(
                replay_response.model_dump(mode="json")
            )
        store_product_promotion_dry_run_record(
            record_store=record_store,
            identity=identity,
            status=promotion_status,
            bump=promotion_request.bump,
            trace_id=trace_id,
            response=response,
        )
        return response

    async def dispatch_product_promotion_workflow(
        product: str,
        environment: str,
        request: Request,
        workflow_request: ProductPromotionWorkflowDispatchEnvelope,
        identity: Annotated[LaunchplaneIdentity, Depends(read_browser_mutation_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> ProductPromotionWorkflowDispatchResponse:
        trace_id = next_trace_id()
        require_product_promotion_operator(identity=identity, trace_id=trace_id)
        profile, lane = resolve_authorized_product_promotion_target(
            record_store=record_store,
            identity=identity,
            product=product,
            environment=environment,
            action="generic_web_prod_promotion.dispatch",
            trace_id=trace_id,
        )
        normalized_key = idempotency_key.strip()
        if not normalized_key:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="idempotency_key_required",
                message="Product promotion workflow dispatch requires an Idempotency-Key header.",
            )
        request_payload = product_promotion_request_payload(
            product=profile.product,
            destination_environment=lane.instance,
            request=workflow_request,
        )
        normalized_key, payload_fingerprint, replay_response = await replay_apply_idempotency(
            request=request,
            record_store=record_store,
            identity=identity,
            route_path=_PRODUCT_PROMOTION_WORKFLOW_ROUTE,
            idempotency_key=normalized_key,
            trace_id=trace_id,
            check_replay=True,
            request_payload=request_payload,
        )
        if replay_response is not None:
            return ProductPromotionWorkflowDispatchResponse.model_validate(
                replay_response.model_dump(mode="json")
            )
        profile, _, promotion_status = current_product_promotion_status(
            record_store=record_store,
            identity=identity,
            product=profile.product,
            environment=lane.instance,
            trace_id=trace_id,
        )
        if workflow_request.evidence_fingerprint != promotion_status.evidence_fingerprint:
            raise _launchplane_http_error(
                status_code=409,
                trace_id=trace_id,
                code="promotion_evidence_changed",
                message="Product promotion evidence changed. Refresh and repeat the direct dry-run.",
            )
        availability = (
            promotion_status.workflow_dry_run
            if workflow_request.dry_run
            else promotion_status.workflow_live
        )
        if not availability.enabled:
            raise _launchplane_http_error(
                status_code=409,
                trace_id=trace_id,
                code="promotion_unavailable",
                message="Product promotion workflow dispatch is blocked by current server evidence.",
            )
        direct_dry_run = product_promotion_dry_run_record(
            record_store=record_store,
            identity=identity,
            status=promotion_status,
            bump=workflow_request.bump,
        )
        if direct_dry_run is None:
            raise _launchplane_http_error(
                status_code=409,
                trace_id=trace_id,
                code="matching_dry_run_required",
                message="Run and accept the matching direct promotion dry-run before dispatch.",
            )
        if (
            not workflow_request.dry_run
            and workflow_request.confirmation
            != product_promotion_confirmation(status=promotion_status, bump=workflow_request.bump)
        ):
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="confirmation_required",
                message="Live product promotion requires the exact reviewed confirmation.",
            )
        if not isinstance(record_store, PostgresRecordStore):
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_required",
                message="Product promotion workflow dispatch requires PostgreSQL storage.",
            )
        try:
            records, result, outbox_delivery = dispatch_generic_web_promotion_workflow_result(
                request=product_promotion_workflow_request(
                    profile=profile,
                    status=promotion_status,
                    request=workflow_request,
                ),
                profile=profile,
                delivery_key=f"{idempotency_scope(identity)}:{normalized_key}",
            )
        except (FileNotFoundError, ValueError, click.ClickException) as error:
            raise _launchplane_http_error(
                status_code=409,
                trace_id=trace_id,
                code="promotion_workflow_dispatch_failed",
                message="Product promotion workflow dispatch could not be prepared.",
            ) from error
        delivery_id = records.get("outbox_delivery_id", "")
        response = ProductPromotionWorkflowDispatchResponse(
            trace_id=trace_id,
            records=records,
            result=ProductPromotionWorkflowDispatchResult.model_validate(
                {
                    **result.model_dump(mode="json"),
                    "evidence_fingerprint": promotion_status.evidence_fingerprint,
                    "delivery_id": delivery_id,
                    "artifact_id": promotion_status.source.artifact_id,
                    "source_git_ref": promotion_status.source.source_git_ref,
                }
            ),
        )
        idempotency_record = build_apply_idempotency_record(
            identity=identity,
            route_path=_PRODUCT_PROMOTION_WORKFLOW_ROUTE,
            idempotency_key=normalized_key,
            request_fingerprint_value=payload_fingerprint,
            trace_id=trace_id,
            response=response,
        )
        try:
            record_store.enqueue_outbox_delivery_with_idempotency(
                OutboxWithIdempotencyRequest(
                    delivery=outbox_delivery,
                    idempotency_record=idempotency_record,
                )
            )
        except Exception as write_error:
            try:
                replay_response = replay_stored_apply_idempotency(
                    record_store=record_store,
                    identity=identity,
                    route_path=_PRODUCT_PROMOTION_WORKFLOW_ROUTE,
                    idempotency_key=normalized_key,
                    request_fingerprint_value=payload_fingerprint,
                    trace_id=trace_id,
                )
            except HTTPException:
                raise
            except Exception as read_error:
                raise write_error from read_error
            if replay_response is None:
                raise
            return ProductPromotionWorkflowDispatchResponse.model_validate(
                replay_response.model_dump(mode="json")
            )
        return response

    async def reencrypt_managed_secrets(
        request: Request,
        identity: Annotated[LaunchplaneIdentity, Depends(read_bearer_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> AcceptedEvidenceResponse:
        trace_id = next_trace_id()
        if isinstance(identity, TerminalAgentIdentity):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Terminal agent credentials cannot rotate managed-secret roots.",
            )
        try:
            raw_payload = await request.json()
        except ValueError as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Managed-secret re-encryption request failed validation.",
            ) from error
        if not isinstance(raw_payload, dict):
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Managed-secret re-encryption request failed validation.",
            )
        request_payload = raw_payload
        try:
            reencryption_request = SecretReencryptionRequest.model_validate(request_payload)
        except ValidationError as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Managed-secret re-encryption request failed validation.",
            ) from error
        if reencryption_request.mode == "apply":
            raise _launchplane_http_error(
                status_code=409,
                trace_id=trace_id,
                code="privileged_operation_approval_required",
                message="Legacy apply is permanently refused; approved privileged operations execute internally.",
            )
        raise _launchplane_http_error(
            status_code=409,
            trace_id=trace_id,
            code="privileged_operation_planning_required",
            message="Use POST /v1/privileged-operations/plans for managed-secret re-encryption planning.",
        )

    async def apply_product_onboarding(
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
                message="Request payload failed validation.",
            ) from error
        if not isinstance(raw_payload, dict):
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request payload failed validation.",
            )
        try:
            onboarding_request = ProductOnboardingApplyEnvelope.model_validate(raw_payload)
        except ValidationError as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request payload failed validation.",
            ) from error
        onboarding_action = (
            "generic_web_onboarding.plan"
            if onboarding_request.generic_web is not None and onboarding_request.mode == "dry_run"
            else "product_onboarding.apply"
        )
        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action=onboarding_action,
            product=onboarding_request.product,
            context=_LAUNCHPLANE_SERVICE_CONTEXT,
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Workflow cannot plan or apply Launchplane product onboarding.",
            )
        database_store = require_product_onboarding_database_store(
            record_store=record_store,
            trace_id=trace_id,
        )
        (
            normalized_idempotency_key,
            payload_fingerprint,
            replay_response,
        ) = await replay_apply_idempotency(
            request=request,
            record_store=database_store,
            identity=identity,
            route_path=_PRODUCT_ONBOARDING_APPLY_ROUTE,
            idempotency_key=idempotency_key,
            trace_id=trace_id,
            check_replay=bool(idempotency_key.strip()),
        )
        if replay_response is not None:
            return replay_response
        onboarding_manifest = onboarding_request.manifest
        generic_web_plan_sha256 = ""
        if onboarding_request.generic_web is not None:
            try:
                existing_profile = database_store.read_product_profile_record(
                    onboarding_request.generic_web.product
                )
            except (FileNotFoundError, KeyError):
                existing_profile = None
            try:
                validate_generic_web_onboarding_is_new_product(
                    intent=onboarding_request.generic_web,
                    existing_profile=existing_profile,
                )
            except ValueError as error:
                raise _launchplane_http_error(
                    status_code=400,
                    trace_id=trace_id,
                    code="invalid_product_onboarding_manifest",
                    message=str(error),
                ) from error
            generic_web_plan_sha256 = generic_web_onboarding_plan_sha256(
                onboarding_request.generic_web
            )
            if onboarding_request.mode == "dry_run":
                try:
                    planned_manifest = build_generic_web_onboarding_manifest(
                        intent=onboarding_request.generic_web,
                        target_id="planned-provider-target",
                    )
                except ValueError as error:
                    raise _launchplane_http_error(
                        status_code=400,
                        trace_id=trace_id,
                        code="invalid_product_onboarding_manifest",
                        message=str(error),
                    ) from error
                return accepted_evidence_response(
                    trace_id=trace_id,
                    records={
                        "provider_target_count": str(len(planned_manifest.provider_targets)),
                        "provider_target_id_count": str(len(planned_manifest.provider_targets)),
                        "runtime_environment_record_count": str(
                            len(planned_manifest.runtime_environments)
                        ),
                        "secret_binding_count": str(len(planned_manifest.secret_bindings)),
                    },
                    result={
                        "mode": "dry_run",
                        "product": onboarding_request.generic_web.product,
                        "repository": onboarding_request.generic_web.repository,
                        "repository_id": onboarding_request.generic_web.repository_id,
                        "repository_owner_id": onboarding_request.generic_web.repository_owner_id,
                        "default_branch": onboarding_request.generic_web.default_branch,
                        "testing_context": onboarding_request.generic_web.testing_context,
                        "preview_context": onboarding_request.generic_web.preview_context,
                        "preview_base_url": onboarding_request.generic_web.preview_base_url,
                        "target_operation": "create-application",
                        "plan_sha256": generic_web_plan_sha256,
                    },
                )
            if onboarding_request.reviewed_plan_sha256 != generic_web_plan_sha256:
                raise _launchplane_http_error(
                    status_code=409,
                    trace_id=trace_id,
                    code="product_onboarding_plan_mismatch",
                    message="Generic-web onboarding inputs no longer match the reviewed dry-run.",
                )
            try:
                onboarding_manifest = build_generic_web_onboarding_manifest(
                    intent=onboarding_request.generic_web,
                    target_id=onboarding_request.resolved_target_id,
                )
            except ValueError as error:
                raise _launchplane_http_error(
                    status_code=400,
                    trace_id=trace_id,
                    code="invalid_product_onboarding_manifest",
                    message=str(error),
                ) from error
        assert onboarding_manifest is not None
        try:
            onboarding_result, authority_bundle = plan_product_onboarding_authority_bundle(
                record_store=database_store,
                manifest=onboarding_manifest,
            )
        except ValueError as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_product_onboarding_manifest",
                message=str(error),
            ) from error
        result, driver_result = (
            control_plane_product_onboarding_service.build_product_onboarding_service_result(
                onboarding_result
            )
        )
        if generic_web_plan_sha256:
            driver_result = {
                **driver_result,
                "mode": "apply",
                "plan_sha256": generic_web_plan_sha256,
            }
        onboarding_response = accepted_evidence_response(
            trace_id=trace_id,
            records={
                "product_profile": string_value(result["product_profile"]),
                "provider_target_count": string_value(result["provider_target_count"]),
                "provider_target_id_count": string_value(result["provider_target_id_count"]),
                "runtime_environment_record_count": string_value(
                    result["runtime_environment_record_count"]
                ),
                "secret_binding_count": string_value(result["secret_binding_count"]),
            },
            result=driver_result,
        )
        database_store.write_product_authority_bundle(
            authority_bundle_with_apply_idempotency(
                bundle=authority_bundle,
                identity=identity,
                route_path=_PRODUCT_ONBOARDING_APPLY_ROUTE,
                idempotency_key=normalized_idempotency_key,
                request_fingerprint_value=payload_fingerprint,
                trace_id=trace_id,
                response=onboarding_response,
            )
        )
        return onboarding_response

    async def import_merge_train_policy(
        request: Request,
        identity: Annotated[LaunchplaneIdentity, Depends(read_browser_mutation_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> AcceptedEvidenceResponse:
        trace_id = next_trace_id()
        if isinstance(identity, TerminalAgentIdentity):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Terminal agent credentials can only read redacted Launchplane context.",
            )
        try:
            raw_payload = await request.json()
        except ValueError as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request payload failed validation.",
            ) from error
        if not isinstance(raw_payload, dict):
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request payload failed validation.",
            )
        try:
            policy_import_request = MergeTrainPolicyImportEnvelope.model_validate(raw_payload)
        except ValidationError as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request payload failed validation.",
            ) from error
        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action="merge_train.policy_import",
            product=policy_import_request.product,
            context=_LAUNCHPLANE_SERVICE_CONTEXT,
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Workflow cannot write Launchplane merge train policies.",
            )
        database_store = require_merge_train_policy_database_store(
            record_store=record_store,
            trace_id=trace_id,
        )
        normalized_idempotency_key = idempotency_key.strip()
        payload_fingerprint = build_request_fingerprint(raw_payload)
        if policy_import_request.mode == "apply":
            (
                normalized_idempotency_key,
                payload_fingerprint,
                replay_response,
            ) = await replay_apply_idempotency(
                request=request,
                record_store=database_store,
                identity=identity,
                route_path=_MERGE_TRAIN_POLICY_IMPORT_ROUTE,
                idempotency_key=normalized_idempotency_key,
                trace_id=trace_id,
                check_replay=bool(normalized_idempotency_key),
            )
            if replay_response is not None:
                return replay_response
            database_store.write_merge_train_policy_record(policy_import_request.record)
        result: dict[str, object] = {
            "mode": policy_import_request.mode,
            "record": {
                "record_id": policy_import_request.record.record_id,
                "status": policy_import_request.record.status,
                "source": policy_import_request.record.source,
                "updated_at": policy_import_request.record.updated_at,
                "policy_sha256": policy_import_request.record.policy_sha256,
                "repository_count": len(policy_import_request.record.policy.policies),
                "policy_keys": [
                    repository_policy.policy_key
                    for repository_policy in policy_import_request.record.policy.policies
                ],
            },
        }
        policy_import_response = accepted_evidence_response(
            trace_id=trace_id,
            records={},
            result=result,
        )
        if policy_import_request.mode == "apply":
            store_apply_idempotency(
                record_store=database_store,
                identity=identity,
                route_path=_MERGE_TRAIN_POLICY_IMPORT_ROUTE,
                idempotency_key=normalized_idempotency_key,
                request_fingerprint_value=payload_fingerprint,
                trace_id=trace_id,
                response=policy_import_response,
            )
        return policy_import_response

    def authz_policy_route_records(result: dict[str, object]) -> dict[str, str]:
        record_id = result.get("authz_policy_record_id")
        if record_id is None:
            return {}
        return {"authz_policy_record_id": string_value(record_id)}

    async def plan_generic_web_preview_authz(
        planning_request: GenericWebPreviewAuthzPlanRequest,
        identity: Annotated[LaunchplaneIdentity, Depends(read_write_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
    ) -> AcceptedEvidenceResponse:
        trace_id = next_trace_id()
        database_store = require_authz_policy_database_store(
            record_store=record_store,
            trace_id=trace_id,
            message="Generic-web preview authz planning requires Launchplane database storage.",
        )
        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action="generic_web_preview_authz.plan",
            product=planning_request.product,
            context=_LAUNCHPLANE_SERVICE_CONTEXT,
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Workflow cannot plan generic-web preview authorization.",
            )
        active_records = database_store.list_authz_policy_records(status="active", limit=2)
        if not active_records:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="authz_policy_unavailable",
                message="Launchplane active authz policy is unavailable.",
            )
        if len(active_records) > 1:
            raise _launchplane_http_error(
                status_code=409,
                trace_id=trace_id,
                code="authz_policy_conflict",
                message="Multiple active Launchplane authz policy records exist.",
            )
        observed_record = active_records[0]
        try:
            profile = None
            if planning_request.operation == "retire":
                profile_store = require_product_profile_read_store(database_store)
                try:
                    profile = profile_store.read_product_profile_record(
                        planning_request.target_product
                    )
                except (FileNotFoundError, KeyError):
                    profile = None
            reconcile_plan = plan_generic_web_preview_authz_reconcile(
                current_policy=observed_record.policy,
                request=planning_request,
                profile=profile,
            )
            reconcile_request = reconcile_plan.reconcile_request
            (
                _,
                current_record,
                _,
                diff,
            ) = control_plane_authz_grant_service.plan_managed_authz_policy_reconcile(
                record_store=database_store,
                request=reconcile_request,
            )
        except control_plane_authz_grant_service.AuthzPolicyConflictError as error:
            raise _launchplane_http_error(
                status_code=409,
                trace_id=trace_id,
                code="authz_policy_conflict",
                message=str(error),
            ) from error
        except ValueError as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_generic_web_preview_authz_plan",
                message=str(error),
            ) from error
        if current_record.policy_sha256 != observed_record.policy_sha256:
            raise _launchplane_http_error(
                status_code=409,
                trace_id=trace_id,
                code="authz_policy_conflict",
                message="Launchplane active authz policy changed while planning.",
            )
        target_rule_count = sum(
            planning_request.target_product in rule.products
            for rule in reconcile_request.desired_policy.github_actions
        )
        plan_result = GenericWebPreviewAuthzPlanResult(
            operation=planning_request.operation,
            target_product=planning_request.target_product,
            target_rule_count=target_rule_count,
            desired_rule_count=len(reconcile_request.desired_policy.github_actions),
            plan_sha256=diff.plan_sha256,
            configuration=reconcile_request.model_dump(mode="json"),
            diff=diff.model_dump(mode="json"),
            retirement_authority=(
                reconcile_plan.retirement_authority.evidence
                if reconcile_plan.retirement_authority is not None
                else None
            ),
        )
        return accepted_evidence_response(
            trace_id=trace_id,
            records={
                "managed_set_id": diff.managed_set_id,
                "desired_rule_count": str(plan_result.desired_rule_count),
                "target_rule_count": str(plan_result.target_rule_count),
            },
            result=plan_result.model_dump(mode="json"),
        )

    def validate_managed_authz_policy_payload(
        *,
        raw_payload: object,
        trace_id: str,
    ) -> control_plane_authz_grant_service.AuthzManagedPolicyReconcileEnvelope:
        if not isinstance(raw_payload, dict):
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request payload failed validation.",
            )
        try:
            return control_plane_authz_grant_service.AuthzManagedPolicyReconcileEnvelope.model_validate(
                raw_payload
            )
        except ValidationError as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request payload failed validation.",
            ) from error

    async def apply_managed_authz_policy_route(
        *,
        request: Request,
        identity: LaunchplaneIdentity,
        record_store: object,
        idempotency_key: str,
    ) -> AcceptedEvidenceResponse:
        trace_id = next_trace_id()
        if not isinstance(identity, GitHubActionsIdentity):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message=(
                    "Managed authz policy reconciliation requires GitHub Actions workload "
                    "transport."
                ),
            )
        try:
            raw_payload = await request.json()
        except ValueError as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request payload failed validation.",
            ) from error
        authz_request = validate_managed_authz_policy_payload(
            raw_payload=raw_payload,
            trace_id=trace_id,
        )
        database_store = require_authz_policy_database_store(
            record_store=record_store,
            trace_id=trace_id,
            message="Managed authz policy reconciliation requires Launchplane database storage.",
        )
        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action="authz_policy_grant.write",
            product=authz_request.product,
            context=_LAUNCHPLANE_SERVICE_CONTEXT,
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Workflow cannot reconcile Launchplane managed authz policy rules.",
            )
        route_path = _AUTHZ_POLICY_MANAGED_RECONCILE_ROUTE
        normalized_idempotency_key = idempotency_key.strip()
        payload_fingerprint = build_request_fingerprint(raw_payload)
        if authz_request.mode == "apply" and not normalized_idempotency_key:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="idempotency_key_required",
                message="Managed authz policy reconciliation apply requires an Idempotency-Key.",
            )
        if authz_request.mode == "apply":
            (
                normalized_idempotency_key,
                payload_fingerprint,
                replay_response,
            ) = await replay_apply_idempotency(
                request=request,
                record_store=database_store,
                identity=identity,
                route_path=route_path,
                idempotency_key=normalized_idempotency_key,
                trace_id=trace_id,
                check_replay=False,
            )
            if replay_response is not None:
                return replay_response
        try:
            route_result = control_plane_authz_grant_service.execute_managed_authz_policy_reconcile(
                record_store=database_store,
                request=authz_request,
                identity=identity,
                trace_id=trace_id,
                now_timestamp=authz_policy_record_timestamp,
                authorized_policy_sha256=resolved_authz_policy_runtime.policy_sha256,
            )
        except control_plane_authz_grant_service.AuthzPolicyRequestError as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message=str(error),
            ) from error
        except control_plane_authz_grant_service.AuthzPolicySafetyError as error:
            raise _launchplane_http_error(
                status_code=409,
                trace_id=trace_id,
                code=error.code,
                message=str(error),
            ) from error
        except control_plane_authz_grant_service.AuthzPolicyConflictError as error:
            if authz_request.mode == "apply" and normalized_idempotency_key:
                replay_response = replay_stored_apply_idempotency(
                    record_store=database_store,
                    identity=identity,
                    route_path=route_path,
                    idempotency_key=normalized_idempotency_key,
                    request_fingerprint_value=payload_fingerprint,
                    trace_id=trace_id,
                )
                if replay_response is not None:
                    return replay_response
            raise _launchplane_http_error(
                status_code=409,
                trace_id=trace_id,
                code="authz_policy_conflict",
                message=(
                    "Launchplane active authz policy changed during this request. "
                    "Refresh the policy state and retry."
                ),
            ) from error
        except ValueError as error:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="authz_policy_unavailable",
                message="Launchplane active authz policy is unavailable.",
            ) from error
        if authz_request.mode == "apply":
            mutation_scope = idempotency_scope(identity)
            idempotency_record_digest = hashlib.sha256(
                "\x1f".join((mutation_scope, route_path, normalized_idempotency_key)).encode()
            ).hexdigest()
            audit_context: dict[str, object] = {
                "request_fingerprint": payload_fingerprint,
                "idempotency_scope_sha256": hashlib.sha256(mutation_scope.encode()).hexdigest(),
                "idempotency_record_id": f"mutation-reservation-{idempotency_record_digest}",
                "idempotency_key_sha256": hashlib.sha256(
                    normalized_idempotency_key.encode()
                ).hexdigest(),
            }
            route_result.authz_policy_record.audit.update(audit_context)
            response_audit = route_result.driver_result.get("audit")
            if isinstance(response_audit, dict):
                response_audit.update(audit_context)
        response = accepted_evidence_response(
            trace_id=trace_id,
            records=authz_policy_route_records(route_result.result),
            result=route_result.driver_result,
        )
        if authz_request.mode != "apply":
            return response
        mutation = DbOnlyMutationRequest(
            scope=idempotency_scope(identity),
            route_path=route_path,
            idempotency_key=normalized_idempotency_key,
            request_fingerprint=payload_fingerprint,
            lease_owner=trace_id,
            response_status_code=202,
            response_trace_id=trace_id,
            response_payload=response.model_dump(mode="json", exclude_none=True),
            lease_seconds=int(_DB_ONLY_MUTATION_LEASE.total_seconds()),
        )
        write_result = database_store.compare_and_write_authz_policy_record(
            expected_record=route_result.previous_authz_policy_record,
            replacement_record=(route_result.authz_policy_record if route_result.changed else None),
            mutation=mutation,
        )
        if write_result.status == "replayed":
            if write_result.idempotency_record is None:
                raise RuntimeError("Replayed authz policy write requires evidence.")
            return replay_idempotent_response(
                trace_id=trace_id,
                stored_record=write_result.idempotency_record,
                route_path=route_path,
            )
        if write_result.status == "idempotency_conflict":
            raise _launchplane_http_error(
                status_code=409,
                trace_id=trace_id,
                code="idempotency_key_reused",
                message=(
                    "Idempotency-Key was already used for a different Launchplane request "
                    "payload on this route."
                ),
            )
        if write_result.status == "reservation_in_progress":
            raise _launchplane_http_error(
                status_code=409,
                trace_id=trace_id,
                code="mutation_in_progress",
                message=(
                    "A matching managed authz policy reconciliation is already running. "
                    "Retry with the same Idempotency-Key."
                ),
            )
        if write_result.status == "reconciliation_required":
            raise _launchplane_http_error(
                status_code=409,
                trace_id=trace_id,
                code="mutation_reconciliation_required",
                message=(
                    "The managed authz policy reconciliation requires reconciliation before retry."
                ),
            )
        if write_result.status == "stale":
            raise _launchplane_http_error(
                status_code=409,
                trace_id=trace_id,
                code="authz_policy_plan_stale",
                message="The reviewed managed authz policy plan is stale.",
            )
        if write_result.status == "ambiguous_active":
            raise _launchplane_http_error(
                status_code=409,
                trace_id=trace_id,
                code="active_authz_policy_ambiguous",
                message="Multiple active Launchplane authz policy records were found.",
            )
        if write_result.status == "missing":
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="authz_policy_unavailable",
                message="Launchplane active authz policy is unavailable.",
            )
        resolved_authz_policy_runtime.update(
            route_result.updated_policy,
            policy_sha256=route_result.authz_policy_record.policy_sha256,
            source="db",
            record_id=route_result.authz_policy_record.record_id,
            revision=route_result.authz_policy_record.revision,
        )
        reconcile_all_manager_preview_approvals_best_effort(
            record_store=database_store,
            control_plane_root=resolved_control_plane_root,
        )
        return response

    async def read_active_authz_policy(
        identity: Annotated[LaunchplaneIdentity, Depends(read_browser_mutation_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
    ) -> LaunchplaneActiveAuthzPolicyResponse:
        trace_id = next_trace_id()
        if isinstance(
            identity, TerminalAgentIdentity
        ) or not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action="authz_policy_grant.write",
            product="launchplane",
            context=_LAUNCHPLANE_SERVICE_CONTEXT,
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Identity cannot read Launchplane authz policy administration state.",
            )
        database_store = require_authz_policy_database_store(
            record_store=record_store,
            trace_id=trace_id,
            message="Active authz policy reads require Launchplane database storage.",
        )
        active_records = database_store.list_authz_policy_records(status="active", limit=2)
        if not active_records:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="authz_policy_unavailable",
                message="Launchplane active authz policy is unavailable.",
            )
        if len(active_records) > 1:
            raise _launchplane_http_error(
                status_code=409,
                trace_id=trace_id,
                code="active_authz_policy_ambiguous",
                message="Multiple active Launchplane authz policy records were found.",
            )
        return LaunchplaneActiveAuthzPolicyResponse(
            trace_id=trace_id,
            policy=control_plane_authz_grant_service.summarize_active_authz_policy_record(
                active_records[0]
            ),
        )

    def read_single_active_authz_policy_record(
        *,
        database_store: PostgresRecordStore,
        trace_id: str,
    ) -> LaunchplaneAuthzPolicyRecord:
        active_records = database_store.list_authz_policy_records(status="active", limit=2)
        if not active_records:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="authz_policy_unavailable",
                message="Launchplane active authz policy is unavailable.",
            )
        if len(active_records) > 1:
            raise _launchplane_http_error(
                status_code=409,
                trace_id=trace_id,
                code="active_authz_policy_ambiguous",
                message="Multiple active Launchplane authz policy records were found.",
            )
        return active_records[0]

    async def read_authz_policy_health(
        identity: Annotated[LaunchplaneIdentity, Depends(read_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
    ) -> AuthzPolicyHealthResponse:
        trace_id = next_trace_id()
        is_administrator = isinstance(identity, LocalAdminIdentity) or (
            isinstance(identity, GitHubHumanIdentity) and identity.role == "admin"
        )
        preflight_authorized = resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action=AUTHZ_POLICY_HEALTH_READ_ACTION,
            product="launchplane",
            context=_LAUNCHPLANE_SERVICE_CONTEXT,
        )
        if not is_administrator or not preflight_authorized:
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Identity cannot read Launchplane authz policy health.",
            )
        database_store = require_authz_policy_database_store(
            record_store=record_store,
            trace_id=trace_id,
            message="Authz policy health reads require Launchplane database storage.",
        )
        active_record = read_single_active_authz_policy_record(
            database_store=database_store,
            trace_id=trace_id,
        )
        if not active_record.policy.allows(
            identity=identity,
            action=AUTHZ_POLICY_HEALTH_READ_ACTION,
            product="launchplane",
            context=_LAUNCHPLANE_SERVICE_CONTEXT,
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Identity cannot read Launchplane authz policy health.",
                authz_policy_provenance=AuthzPolicyProvenance.from_record(active_record),
            )
        snapshot = control_plane_authz_grant_service.summarize_active_authz_policy_health_record(
            record=active_record,
            caller_identity=identity,
        )
        return AuthzPolicyHealthResponse(
            trace_id=trace_id,
            **snapshot.model_dump(),
        )

    async def read_authz_activation_preflight_self(
        request: Request,
        response: Response,
        session: Annotated[
            LaunchplaneHumanSession, Depends(read_authz_activation_preflight_session)
        ],
        record_store: Annotated[object, Depends(get_record_store)],
    ) -> AuthzActivationPreflightSelfResponse:
        trace_id = next_trace_id()
        response.headers["Cache-Control"] = "no-store"
        try:
            if request.query_params or await request.body():
                raise control_plane_authz_activation_preflight.ActivationPreflightFailure(
                    "activation_preflight_parameters_not_allowed",
                    "Activation preflight does not accept request parameters.",
                    status_code=400,
                )
            database_store = require_authz_policy_database_store(
                record_store=record_store,
                trace_id=trace_id,
                message="Activation preflight reads require Launchplane database storage.",
            )
            active_record = read_single_active_authz_policy_record(
                database_store=database_store,
                trace_id=trace_id,
            )
            return (
                control_plane_authz_activation_preflight.build_activation_preflight_self_response(
                    trace_id=trace_id,
                    session=session,
                    active_record=active_record,
                    now=datetime.now(timezone.utc),
                )
            )
        except control_plane_authz_activation_preflight.ActivationPreflightFailure as error:
            raise _launchplane_http_error(
                status_code=error.status_code,
                trace_id=trace_id,
                code=error.code,
                message=str(error),
                headers={"Cache-Control": "no-store"},
            ) from error
        except LaunchplaneHTTPException as error:
            error.headers = {**(error.headers or {}), "Cache-Control": "no-store"}
            raise
        except Exception as error:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="activation_preflight_unavailable",
                message="Activation preflight evidence is unavailable.",
                headers={"Cache-Control": "no-store"},
            ) from error

    async def read_authz_repository_scope(
        scope_request: AuthzRepositoryScopeReadRequest,
        response: Response,
        identity: Annotated[
            LaunchplaneIdentity,
            Depends(read_nonpersisting_sensitive_identity),
        ],
        record_store: Annotated[object, Depends(get_record_store)],
    ) -> AuthzRepositoryScopeResponse:
        trace_id = next_trace_id()
        eligible_reader = isinstance(
            identity,
            GitHubHumanIdentity | LocalOperatorIdentity | LocalAdminIdentity,
        )
        preflight = resolved_authz_policy_runtime.policy.evaluate(
            identity=identity,
            action=AUTHZ_REPOSITORY_SCOPE_READ_ACTION,
            product="launchplane",
            context=_LAUNCHPLANE_SERVICE_CONTEXT,
        )
        if not eligible_reader or preflight.decision != "allowed":
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Identity cannot read Launchplane authorization repository scope.",
                headers={"Cache-Control": "no-store"},
            )
        try:
            database_store = require_authz_policy_database_store(
                record_store=record_store,
                trace_id=trace_id,
                message=(
                    "Authorization repository scope reads require Launchplane database storage."
                ),
            )
            active_record = read_single_active_authz_policy_record(
                database_store=database_store,
                trace_id=trace_id,
            )
        except LaunchplaneHTTPException as error:
            error.headers = {**(error.headers or {}), "Cache-Control": "no-store"}
            raise
        except (TypeError, ValueError) as error:
            raise _launchplane_http_error(
                status_code=409,
                trace_id=trace_id,
                code="authz_repository_scope_invalid",
                message="Authorization repository scope evidence is invalid.",
                headers={"Cache-Control": "no-store"},
            ) from error
        except (OSError, SQLAlchemyError) as error:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="authz_repository_scope_unavailable",
                message="Authorization repository scope evidence is unavailable.",
                headers={"Cache-Control": "no-store"},
            ) from error
        active_policy_evaluation = active_record.policy.evaluate(
            identity=identity,
            action=AUTHZ_REPOSITORY_SCOPE_READ_ACTION,
            product="launchplane",
            context=_LAUNCHPLANE_SERVICE_CONTEXT,
        )
        if active_policy_evaluation.decision != "allowed":
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Identity cannot read Launchplane authorization repository scope.",
                authz_policy_provenance=AuthzPolicyProvenance.from_record(active_record),
                headers={"Cache-Control": "no-store"},
            )
        try:
            result = control_plane_authz_repository_scope.build_authz_repository_scope_response(
                trace_id=trace_id,
                generated_at=datetime.now(timezone.utc).isoformat(),
                request=scope_request,
                active_policy_record=active_record,
                store=database_store,
            )
        except click.ClickException as error:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="authz_repository_scope_handle_unavailable",
                message="Authorization repository scope handles are unavailable.",
                headers={"Cache-Control": "no-store"},
            ) from error
        except (TypeError, ValueError) as error:
            raise _launchplane_http_error(
                status_code=409,
                trace_id=trace_id,
                code="authz_repository_scope_invalid",
                message="Authorization repository scope evidence is invalid.",
                headers={"Cache-Control": "no-store"},
            ) from error
        except (OSError, RuntimeError, SQLAlchemyError) as error:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="authz_repository_scope_unavailable",
                message="Authorization repository scope evidence is unavailable.",
                headers={"Cache-Control": "no-store"},
            ) from error
        response.headers["Cache-Control"] = "no-store"
        return result

    async def preview_authz_candidate_policy(
        preview_request: AuthzPolicyCandidatePreviewRequest,
        identity: Annotated[
            LaunchplaneIdentity,
            Depends(read_nonpersisting_sensitive_identity),
        ],
        record_store: Annotated[object, Depends(get_record_store)],
    ) -> AuthzPolicyCandidatePreviewResponse:
        trace_id = next_trace_id()
        is_administrator = isinstance(identity, LocalAdminIdentity) or (
            isinstance(identity, GitHubHumanIdentity) and identity.role == "admin"
        )
        preflight_authorized = (
            resolved_authz_policy_runtime.policy.evaluate(
                identity=identity,
                action=AUTHZ_POLICY_CANDIDATE_PREVIEW_READ_ACTION,
                product="launchplane",
                context=_LAUNCHPLANE_SERVICE_CONTEXT,
                record_context=False,
            ).decision
            == "allowed"
        )
        if not is_administrator or not preflight_authorized:
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Identity cannot preview Launchplane authz candidate policy.",
            )
        database_store = require_authz_policy_database_store(
            record_store=record_store,
            trace_id=trace_id,
            message="Authz candidate policy previews require Launchplane database storage.",
        )
        active_record = read_single_active_authz_policy_record(
            database_store=database_store,
            trace_id=trace_id,
        )
        active_policy_authorized = (
            active_record.policy.evaluate(
                identity=identity,
                action=AUTHZ_POLICY_CANDIDATE_PREVIEW_READ_ACTION,
                product="launchplane",
                context=_LAUNCHPLANE_SERVICE_CONTEXT,
                record_context=False,
            ).decision
            == "allowed"
        )
        if not active_policy_authorized:
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Identity cannot preview Launchplane authz candidate policy.",
                authz_policy_provenance=AuthzPolicyProvenance.from_record(active_record),
            )
        try:
            return control_plane_authz_grant_service.preview_authz_candidate_policy(
                active_record=active_record,
                caller_identity=identity,
                request=preview_request,
                trace_id=trace_id,
            )
        except (
            control_plane_authz_grant_service.AuthzPolicyRequestError,
            ValueError,
        ) as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="authz_candidate_policy_invalid",
                message="Authorization candidate policy is invalid.",
            ) from error

    async def evaluate_effective_access(
        evaluation_request: EffectiveAccessEvaluateRequest,
        identity: Annotated[LaunchplaneIdentity, Depends(read_browser_mutation_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
    ) -> EffectiveAccessEvaluateResponse:
        trace_id = next_trace_id()
        is_administrator = isinstance(identity, LocalAdminIdentity) or (
            isinstance(identity, GitHubHumanIdentity) and identity.role == "admin"
        )
        preflight_authorized = resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action=EFFECTIVE_ACCESS_READ_ACTION,
            product="launchplane",
            context=_LAUNCHPLANE_SERVICE_CONTEXT,
        )
        if not is_administrator or not preflight_authorized:
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Identity cannot evaluate Launchplane effective access.",
            )
        database_store = require_authz_policy_database_store(
            record_store=record_store,
            trace_id=trace_id,
            message="Effective access reads require Launchplane database storage.",
        )
        active_record = read_single_active_authz_policy_record(
            database_store=database_store,
            trace_id=trace_id,
        )
        if not active_record.policy.allows(
            identity=identity,
            action=EFFECTIVE_ACCESS_READ_ACTION,
            product="launchplane",
            context=_LAUNCHPLANE_SERVICE_CONTEXT,
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Identity cannot evaluate Launchplane effective access.",
                authz_policy_provenance=AuthzPolicyProvenance.from_record(active_record),
            )
        target = (
            AuthorizationTarget(scope="instance", instances=(evaluation_request.instance,))
            if evaluation_request.target_scope == "instance"
            else AuthorizationTarget(scope="context")
        )
        evaluation = active_record.policy.evaluate(
            identity=evaluation_request.identity(),
            action=evaluation_request.action,
            product=evaluation_request.product,
            context=evaluation_request.context,
            target=target,
            record_context=False,
        )
        return EffectiveAccessEvaluateResponse(
            trace_id=trace_id,
            policy_record_id=active_record.record_id,
            policy_revision=active_record.revision,
            policy_sha256=active_record.policy_sha256,
            request=EffectiveAccessRequestSummary(
                principal_type=evaluation_request.principal.principal_type,
                action=evaluation_request.action,
                product=evaluation_request.product,
                context=evaluation_request.context,
                target_scope=evaluation_request.target_scope,
                instance=evaluation_request.instance,
            ),
            evaluation=EffectiveAccessDecision(
                decision=evaluation.decision,
                reason_code=evaluation.reason_code,
            ),
        )

    async def explain_authz_denial(
        trace_id: str,
        identity: Annotated[LaunchplaneIdentity, Depends(read_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
    ) -> AuthzDenialExplanationResponse:
        request_trace_id = next_trace_id()
        is_support_reader = isinstance(
            identity,
            GitHubHumanIdentity | LocalOperatorIdentity | LocalAdminIdentity,
        )
        preflight_authorized = resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action=AUTHZ_DENIAL_EXPLANATION_READ_ACTION,
            product="launchplane",
            context=_LAUNCHPLANE_SERVICE_CONTEXT,
        )
        if not is_support_reader or not preflight_authorized:
            raise _launchplane_http_error(
                status_code=403,
                trace_id=request_trace_id,
                code="authorization_denied",
                message="Identity cannot explain Launchplane authorization denials.",
            )
        database_store = require_authz_policy_database_store(
            record_store=record_store,
            trace_id=request_trace_id,
            message="Authorization denial explanations require Launchplane database storage.",
        )
        active_record = read_single_active_authz_policy_record(
            database_store=database_store,
            trace_id=request_trace_id,
        )
        if not active_record.policy.allows(
            identity=identity,
            action=AUTHZ_DENIAL_EXPLANATION_READ_ACTION,
            product="launchplane",
            context=_LAUNCHPLANE_SERVICE_CONTEXT,
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=request_trace_id,
                code="authorization_denied",
                message="Identity cannot explain Launchplane authorization denials.",
                authz_policy_provenance=AuthzPolicyProvenance.from_record(active_record),
            )
        denial_record = database_store.read_authz_denial_record(
            trace_id=trace_id,
            observed_at=datetime.now(timezone.utc).isoformat(),
        )
        if denial_record is None:
            raise _launchplane_http_error(
                status_code=404,
                trace_id=request_trace_id,
                code="authz_denial_not_found",
                message="Authorization denial evidence was not found.",
            )
        return AuthzDenialExplanationResponse(
            trace_id=denial_record.trace_id,
            recorded_at=denial_record.recorded_at,
            route_path=denial_record.route_path,
            principal_type=denial_record.principal_type,
            action=denial_record.action,
            product=denial_record.product,
            context=denial_record.context,
            target_scope=denial_record.target_scope,
            instance_specified=denial_record.instance_specified,
            reason_code=denial_record.reason_code,
            policy_record_id=denial_record.policy_record_id,
            policy_revision=denial_record.policy_revision,
            policy_sha256=denial_record.policy_sha256,
        )

    async def evaluate_github_actions_authz_diagnostic(
        diagnostic_request: control_plane_authz_diagnostics.AuthzDiagnosticEvaluateEnvelope,
        identity: Annotated[LaunchplaneIdentity, Depends(read_bearer_identity)],
    ) -> control_plane_authz_diagnostics.AuthzDiagnosticEvaluateResponse:
        trace_id = next_trace_id()
        if not isinstance(identity, GitHubActionsIdentity) or not (
            resolved_authz_policy_runtime.policy.allows(
                identity=identity,
                action="authz_diagnostic.evaluate",
                product=diagnostic_request.product,
                context=diagnostic_request.context,
                target=diagnostic_request.target,
            )
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Workflow cannot evaluate its Launchplane authorization.",
            )
        return control_plane_authz_diagnostics.AuthzDiagnosticEvaluateResponse(
            trace_id=trace_id,
            policy_record_id=resolved_authz_policy_runtime.record_id,
            policy_revision=resolved_authz_policy_runtime.revision,
            policy_sha256=resolved_authz_policy_runtime.policy_sha256,
            evaluation=control_plane_authz_diagnostics.evaluate_github_actions_authz(
                policy=resolved_authz_policy_runtime.policy,
                identity=identity,
                request=diagnostic_request,
            ),
        )

    async def reconcile_managed_authz_policy(
        request: Request,
        identity: Annotated[LaunchplaneIdentity, Depends(read_bearer_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> AcceptedEvidenceResponse:
        return await apply_managed_authz_policy_route(
            request=request,
            identity=identity,
            record_store=record_store,
            idempotency_key=idempotency_key,
        )

    async def apply_live_target_runtime(
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
            live_target_runtime_request = (
                control_plane_live_target_runtime.LiveTargetRuntimeApplyEnvelope.model_validate(
                    raw_payload
                )
            )
        except ValidationError as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request could not be completed.",
            ) from error
        action = (
            "live_target_runtime.apply"
            if live_target_runtime_request.apply_changes
            else "live_target_runtime.plan"
        )
        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action=action,
            product=live_target_runtime_request.product,
            context=live_target_runtime_request.context,
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message=(
                    "Workflow cannot plan or apply live target runtime for the requested"
                    " product/context."
                ),
            )
        (
            normalized_idempotency_key,
            payload_fingerprint,
            replay_response,
        ) = await replay_apply_idempotency(
            request=request,
            record_store=record_store,
            identity=identity,
            route_path=_LIVE_TARGET_RUNTIME_APPLY_ROUTE,
            idempotency_key=idempotency_key,
            trace_id=trace_id,
            check_replay=bool(idempotency_key.strip()),
        )
        if replay_response is not None:
            return replay_response
        live_target_runtime_store = require_live_target_runtime_database_store(
            record_store=record_store,
            trace_id=trace_id,
        )
        try:
            driver_result = control_plane_live_target_runtime.apply_live_target_runtime_environment(
                control_plane_root=resolved_control_plane_root,
                database_url=live_target_runtime_store.database_url,
                product_name=live_target_runtime_request.product,
                context_name=live_target_runtime_request.context,
                instance_name=live_target_runtime_request.instance,
                apply_changes=live_target_runtime_request.apply_changes,
                deploy=live_target_runtime_request.deploy,
                no_cache=live_target_runtime_request.no_cache,
                deploy_timeout_seconds=live_target_runtime_request.deploy_timeout_seconds,
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
            raise _launchplane_http_error(
                status_code=status_code,
                trace_id=trace_id,
                code=error.code,
                message=str(error),
            ) from error
        tracked_target = driver_result.get("tracked_target")
        records: dict[str, str] = {}
        if isinstance(tracked_target, dict):
            records = {
                "target_id": str(tracked_target.get("target_id", "")),
                "target_type": str(tracked_target.get("target_type", "")),
            }
        live_target_runtime_response = accepted_evidence_response(
            trace_id=trace_id,
            records=records,
            result=driver_result,
        )
        store_apply_idempotency(
            record_store=record_store,
            identity=identity,
            route_path=_LIVE_TARGET_RUNTIME_APPLY_ROUTE,
            idempotency_key=normalized_idempotency_key,
            request_fingerprint_value=payload_fingerprint,
            trace_id=trace_id,
            response=live_target_runtime_response,
        )
        return live_target_runtime_response

    async def run_provider_target_operations(
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
                message="Request payload failed validation.",
            ) from error
        if not isinstance(raw_payload, dict):
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request payload failed validation.",
            )
        try:
            provider_target_request = ProviderTargetOperationEnvelope.model_validate(raw_payload)
        except ValidationError as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request payload failed validation.",
            ) from error
        provider_target_store = require_provider_target_operation_database_store(
            record_store=record_store,
            trace_id=trace_id,
        )
        if provider_target_operation_requires_reason(
            identity=identity,
            request=provider_target_request,
        ):
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="reason_required",
                message="Local operator provider-target backfill apply requires a reason.",
            )
        if not provider_target_operation_authorized(
            authz_policy=resolved_authz_policy_runtime.policy,
            identity=identity,
            request=provider_target_request,
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Workflow cannot run Launchplane provider-target operations.",
            )
        normalized_idempotency_key = idempotency_key.strip()
        payload_fingerprint = build_request_fingerprint(raw_payload)
        if provider_target_request.mode == "backfill-apply":
            if not normalized_idempotency_key:
                raise _launchplane_http_error(
                    status_code=400,
                    trace_id=trace_id,
                    code="idempotency_key_required",
                    message=(
                        "Provider-target backfill apply requests require an Idempotency-Key header."
                    ),
                )
            (
                normalized_idempotency_key,
                payload_fingerprint,
                replay_response,
            ) = await replay_apply_idempotency(
                request=request,
                record_store=provider_target_store,
                identity=identity,
                route_path=PROVIDER_TARGET_OPERATIONS_ROUTE,
                idempotency_key=normalized_idempotency_key,
                trace_id=trace_id,
                check_replay=True,
            )
            if replay_response is not None:
                return replay_response
        provider_target_result = execute_provider_target_operation_route(
            record_store=provider_target_store,
            request=provider_target_request,
        )
        provider_target_response = accepted_evidence_response(
            trace_id=trace_id,
            records={},
            result=provider_target_result.driver_result,
        )
        if provider_target_request.mode == "backfill-apply":
            store_apply_idempotency(
                record_store=provider_target_store,
                identity=identity,
                route_path=PROVIDER_TARGET_OPERATIONS_ROUTE,
                idempotency_key=normalized_idempotency_key,
                request_fingerprint_value=payload_fingerprint,
                trace_id=trace_id,
                response=provider_target_response,
            )
        return provider_target_response

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

    def plan_instance_scoped_existing_ingress_route(
        *,
        ingress_provider: IngressProvider,
        request: NpmplusIngressApplyRequest,
    ) -> tuple[NpmplusIngressApplyRequest, NpmplusIngressApplyResult]:
        if request.expected_host_id is None or not request.require_exact_expected_host_domains:
            raise ValueError(
                "Instance-scoped shared-host planning requires an exact expected provider host."
            )
        inspection_request = request.model_copy(
            update={
                "mode": "dry-run",
                "require_exact_expected_host_domains": False,
                "allow_create": False,
                "allow_update": True,
                "allow_enable_disable": True,
            }
        )
        inspection_result = ingress_provider.apply_route(request=inspection_request)
        existing_host = inspection_result.proxy_host
        if existing_host is None or existing_host.id != request.expected_host_id:
            raise click.ClickException(
                "Instance-scoped ingress review could not resolve the expected provider host."
            )
        requested_domains = frozenset(request.route.domain_names)
        existing_domains = frozenset(existing_host.domain_names)
        if not requested_domains.issubset(existing_domains):
            raise click.ClickException(
                "Instance-scoped ingress domains are not all present on the expected provider host."
            )
        if requested_domains == existing_domains:
            return request, inspection_result

        provider_route = request.route.model_copy(
            update={"domain_names": existing_host.domain_names}
        )
        provider_request = request.model_copy(update={"route": provider_route})
        comparison_result = ingress_provider.apply_route(
            request=provider_request.model_copy(
                update={
                    "mode": "dry-run",
                    "allow_create": False,
                    "allow_update": True,
                    "allow_enable_disable": True,
                }
            )
        )
        return provider_request, comparison_result

    def scope_instance_ingress_result(
        *,
        result: NpmplusIngressApplyResult,
        requested_domains: tuple[str, ...],
    ) -> NpmplusIngressApplyResult:
        scoped_operations = tuple(
            operation.model_copy(update={"domain_names": requested_domains})
            for operation in result.operations
        )
        scoped_host = (
            result.proxy_host.model_copy(update={"domain_names": requested_domains})
            if result.proxy_host is not None
            else None
        )
        return result.model_copy(
            update={
                "operations": scoped_operations,
                "proxy_host": scoped_host,
            }
        )

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
        certificate_id = (
            result.proxy_host.certificate_id
            if result.proxy_host is not None
            else request.route.certificate_id
        )
        tls_owner: IngressRouteTlsOwner = "none" if certificate_id == 0 else "provider"
        provider_certificate_ref = "" if certificate_id == 0 else str(certificate_id)
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
            tls_owner=tls_owner,
            provider_certificate_ref=provider_certificate_ref,
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
        certificate_id = request.route.certificate_id
        tls_owner: IngressRouteTlsOwner = "none" if certificate_id == 0 else "provider"
        provider_certificate_ref = "" if certificate_id == 0 else str(certificate_id)
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
            tls_owner=tls_owner,
            provider_certificate_ref=provider_certificate_ref,
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

    async def reconcile_external_route_binding(
        request: Request,
        binding_request: ExternalRouteBindingReconcileEnvelope,
        identity: Annotated[LaunchplaneIdentity, Depends(read_write_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> AcceptedEvidenceResponse:
        trace_id = next_trace_id()
        authorization_action = (
            "route_binding.external.apply"
            if binding_request.mode == "apply"
            else "route_binding.external.plan"
        )
        ensure_route_binding_allowed(
            identity=identity,
            trace_id=trace_id,
            action=authorization_action,
            product=binding_request.product,
            context_name=binding_request.context,
            instance_name=binding_request.instance,
            message="Workflow cannot reconcile external route bindings for the requested lane.",
        )
        if binding_request.mode == "apply" and not idempotency_key.strip():
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="idempotency_key_required",
                message="External route binding reconcile apply requires an Idempotency-Key header.",
            )
        try:
            route_binding_store = require_external_route_binding_reconcile_store(record_store)
        except TypeError as error:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message=str(error),
            ) from error
        mutation_store: _ExternalRouteBindingMutationStore | None = None
        normalized_key = idempotency_key.strip()
        payload_fingerprint = ""
        if binding_request.mode == "apply":
            try:
                mutation_store = require_external_route_binding_mutation_store(record_store)
            except TypeError as error:
                raise _launchplane_http_error(
                    status_code=503,
                    trace_id=trace_id,
                    code="database_storage_required",
                    message=str(error),
                ) from error
            raw_payload = await request.json()
            payload_fingerprint = idempotency_request_fingerprint(
                route_path=_EXTERNAL_ROUTE_BINDING_RECONCILE_ROUTE,
                payload=raw_payload,
            )
            preflight = mutation_store.prepare_db_only_mutation(
                scope=idempotency_scope(identity),
                route_path=_EXTERNAL_ROUTE_BINDING_RECONCILE_ROUTE,
                idempotency_key=normalized_key,
                request_fingerprint=payload_fingerprint,
            )
            if preflight.status not in {"missing", "released"}:
                if preflight.record is None:
                    raise RuntimeError(
                        "External route binding mutation preflight requires evidence."
                    )
                if preflight.status == "replayed":
                    return replay_idempotent_response(
                        trace_id=trace_id,
                        stored_record=preflight.record,
                        route_path=_EXTERNAL_ROUTE_BINDING_RECONCILE_ROUTE,
                    )
                if preflight.status == "conflict":
                    raise _launchplane_http_error(
                        status_code=409,
                        trace_id=trace_id,
                        code="idempotency_key_reused",
                        message=(
                            "Idempotency-Key was already used for a different "
                            "Launchplane request payload on this route."
                        ),
                    )
                if preflight.status == "in_progress":
                    raise _launchplane_http_error(
                        status_code=409,
                        trace_id=trace_id,
                        code="mutation_in_progress",
                        message=(
                            "A matching external route binding mutation is already running. "
                            "Retry with the same Idempotency-Key."
                        ),
                    )
                if preflight.status == "reconcile_required":
                    raise _launchplane_http_error(
                        status_code=409,
                        trace_id=trace_id,
                        code="mutation_reconciliation_required",
                        message=(
                            "The external route binding mutation requires reconciliation "
                            "before retry."
                        ),
                    )
                raise RuntimeError(
                    "Unsupported external route binding mutation preflight status: "
                    f"{preflight.status}"
                )

        reconcile_plan = control_plane_route_binding_external_reconcile.plan_external_route_binding_reconcile(
            record_store=route_binding_store,
            request=control_plane_route_binding_external_reconcile.ExternalRouteBindingReconcileRequest(
                product=binding_request.product,
                context=binding_request.context,
                instance=binding_request.instance,
                expected_current=binding_request.expected_current,
                desired_status=binding_request.desired_status,
                source_label=binding_request.source_label,
                evaluated_at=utc_now_timestamp(),
            ),
        )

        def reconcile_response(*, route_binding_status: str) -> AcceptedEvidenceResponse:
            result_payload = reconcile_plan.model_dump(mode="json", exclude_none=True)
            result_payload["mode"] = binding_request.mode
            result_payload["actor"] = launchplane_identity_actor(identity)
            result_payload["reason"] = binding_request.reason
            result_payload["source_label"] = binding_request.source_label
            result_payload["desired_status"] = binding_request.desired_status
            result_payload["route_binding_status"] = route_binding_status
            if reconcile_plan.record is not None:
                result_payload["record"] = redacted_route_binding_record(
                    reconcile_plan.record
                ).model_dump(mode="json")
            return accepted_evidence_response(
                trace_id=trace_id,
                records={
                    "route_binding_status": route_binding_status,
                    "product": binding_request.product,
                    "context": binding_request.context,
                    "instance": binding_request.instance,
                },
                result=result_payload,
            )

        if reconcile_plan.status in {"blocked", "conflict"}:
            return reconcile_response(route_binding_status=reconcile_plan.status)
        if reconcile_plan.record is None:
            raise RuntimeError("External route binding reconcile plan requires a candidate record.")
        if reconcile_plan.status == "unchanged":
            expected_write_status = "unchanged"
        elif reconcile_plan.operation == "create":
            expected_write_status = "created"
        elif reconcile_plan.operation in {"refresh", "replace", "relinquish"}:
            expected_write_status = "refreshed"
        else:
            raise RuntimeError("Ready external route binding reconcile plan requires an operation.")
        response_status = (
            expected_write_status
            if binding_request.mode == "apply" or expected_write_status == "unchanged"
            else f"planned_{reconcile_plan.operation}"
        )
        response = reconcile_response(route_binding_status=response_status)
        if binding_request.mode == "dry-run":
            return response
        if mutation_store is None:
            raise RuntimeError("External route binding reconcile apply requires a mutation store.")
        mutation_result = mutation_store.reconcile_route_binding_record(
            expected_record=reconcile_plan.current_record,
            replacement_record=reconcile_plan.record,
            mutation=DbOnlyMutationRequest(
                scope=idempotency_scope(identity),
                route_path=_EXTERNAL_ROUTE_BINDING_RECONCILE_ROUTE,
                idempotency_key=normalized_key,
                request_fingerprint=payload_fingerprint,
                lease_owner=trace_id,
                response_status_code=202,
                response_trace_id=trace_id,
                response_payload=response.model_dump(mode="json", exclude_none=True),
                lease_seconds=int(_DB_ONLY_MUTATION_LEASE.total_seconds()),
            ),
        )
        if mutation_result.status in {"created", "refreshed", "unchanged"}:
            if mutation_result.status != expected_write_status:
                raise RuntimeError(
                    "External route binding reconcile write did not match the reviewed plan."
                )
            return response
        if mutation_result.status == "replayed":
            if mutation_result.idempotency_record is None:
                raise RuntimeError("Replayed external route binding reconcile requires evidence.")
            return replay_idempotent_response(
                trace_id=trace_id,
                stored_record=mutation_result.idempotency_record,
                route_path=_EXTERNAL_ROUTE_BINDING_RECONCILE_ROUTE,
            )
        if mutation_result.status == "idempotency_conflict":
            raise _launchplane_http_error(
                status_code=409,
                trace_id=trace_id,
                code="idempotency_key_reused",
                message=(
                    "Idempotency-Key was already used for a different "
                    "Launchplane request payload on this route."
                ),
            )
        if mutation_result.status == "reconciliation_required":
            raise _launchplane_http_error(
                status_code=409,
                trace_id=trace_id,
                code="mutation_reconciliation_required",
                message=(
                    "The external route binding mutation requires reconciliation before retry."
                ),
            )
        if mutation_result.status in {"changed", "missing"}:
            raise _launchplane_http_error(
                status_code=409,
                trace_id=trace_id,
                code="route_binding_changed",
                message=(
                    "The route-binding record changed after planning. Read the current record "
                    "and reconcile again with its SHA-256 digest."
                ),
            )
        raise _launchplane_http_error(
            status_code=409,
            trace_id=trace_id,
            code="mutation_in_progress",
            message=(
                "A matching external route binding reconcile is already running. "
                "Retry with the same Idempotency-Key."
            ),
        )

    async def reconcile_route_binding(
        request: Request,
        binding_request: RouteBindingReconcileEnvelope,
        identity: Annotated[LaunchplaneIdentity, Depends(read_write_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> AcceptedEvidenceResponse:
        trace_id = next_trace_id()
        authorization_action = (
            "route_binding.apply" if binding_request.mode == "apply" else "route_binding.read"
        )
        ensure_route_binding_allowed(
            identity=identity,
            trace_id=trace_id,
            action=authorization_action,
            product=binding_request.product,
            context_name=binding_request.context,
            instance_name=binding_request.instance,
            message="Workflow cannot reconcile route bindings for the requested product/context.",
        )
        if binding_request.mode == "apply" and not idempotency_key.strip():
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="idempotency_key_required",
                message="Route binding reconcile apply requires an Idempotency-Key header.",
            )
        try:
            route_binding_store = require_route_binding_reconcile_store(record_store)
        except TypeError as error:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message=str(error),
            ) from error
        mutation_store: _RouteBindingMutationStore | None = None
        normalized_key = idempotency_key.strip()
        payload_fingerprint = ""
        if binding_request.mode == "apply":
            try:
                mutation_store = require_route_binding_mutation_store(record_store)
            except TypeError as error:
                raise _launchplane_http_error(
                    status_code=503,
                    trace_id=trace_id,
                    code="database_storage_required",
                    message=str(error),
                ) from error
            raw_payload = await request.json()
            payload_fingerprint = idempotency_request_fingerprint(
                route_path=_ROUTE_BINDING_RECONCILE_ROUTE,
                payload=raw_payload,
            )
            preflight = mutation_store.prepare_db_only_mutation(
                scope=idempotency_scope(identity),
                route_path=_ROUTE_BINDING_RECONCILE_ROUTE,
                idempotency_key=normalized_key,
                request_fingerprint=payload_fingerprint,
            )
            if preflight.status not in {"missing", "released"}:
                if preflight.record is None:
                    raise RuntimeError("Route binding mutation preflight requires evidence.")
                if preflight.status == "replayed":
                    return replay_idempotent_response(
                        trace_id=trace_id,
                        stored_record=preflight.record,
                        route_path=_ROUTE_BINDING_RECONCILE_ROUTE,
                    )
                if preflight.status == "conflict":
                    raise _launchplane_http_error(
                        status_code=409,
                        trace_id=trace_id,
                        code="idempotency_key_reused",
                        message=(
                            "Idempotency-Key was already used for a different "
                            "Launchplane request payload on this route."
                        ),
                    )
                if preflight.status == "in_progress":
                    raise _launchplane_http_error(
                        status_code=409,
                        trace_id=trace_id,
                        code="mutation_in_progress",
                        message=(
                            "A matching route binding mutation is already running. "
                            "Retry with the same Idempotency-Key."
                        ),
                    )
                if preflight.status == "reconcile_required":
                    raise _launchplane_http_error(
                        status_code=409,
                        trace_id=trace_id,
                        code="mutation_reconciliation_required",
                        message=(
                            "The route binding mutation requires reconciliation before retry."
                        ),
                    )
                raise RuntimeError(
                    f"Unsupported route binding mutation preflight status: {preflight.status}"
                )

        reconcile_plan = control_plane_route_binding_reconcile.plan_route_binding_reconcile(
            record_store=route_binding_store,
            request=control_plane_route_binding_reconcile.RouteBindingReconcileRequest(
                product=binding_request.product,
                context=binding_request.context,
                instance=binding_request.instance,
                expected_current=binding_request.expected_current,
                source_label=binding_request.source_label,
                evaluated_at=utc_now_timestamp(),
            ),
        )

        def reconcile_response(*, route_binding_status: str) -> AcceptedEvidenceResponse:
            result_payload = reconcile_plan.model_dump(mode="json", exclude_none=True)
            result_payload["mode"] = binding_request.mode
            result_payload["route_binding_status"] = route_binding_status
            if reconcile_plan.record is not None:
                result_payload["record"] = redacted_route_binding_record(
                    reconcile_plan.record
                ).model_dump(mode="json")
            return accepted_evidence_response(
                trace_id=trace_id,
                records={
                    "route_binding_status": route_binding_status,
                    "product": binding_request.product,
                    "context": binding_request.context,
                    "instance": binding_request.instance,
                },
                result=result_payload,
            )

        if reconcile_plan.status in {"blocked", "conflict"}:
            return reconcile_response(route_binding_status=reconcile_plan.status)
        if reconcile_plan.record is None:
            raise RuntimeError("Route binding reconcile plan requires a candidate record.")
        if reconcile_plan.status == "unchanged":
            expected_write_status = "unchanged"
        elif reconcile_plan.operation == "create":
            expected_write_status = "created"
        elif reconcile_plan.operation == "refresh":
            expected_write_status = "refreshed"
        else:
            raise RuntimeError("Ready route binding reconcile plan requires an operation.")
        response_status = (
            expected_write_status
            if binding_request.mode == "apply" or expected_write_status == "unchanged"
            else f"planned_{reconcile_plan.operation}"
        )
        response = reconcile_response(route_binding_status=response_status)
        if binding_request.mode == "dry-run":
            return response
        if mutation_store is None:
            raise RuntimeError("Route binding reconcile apply requires a mutation store.")
        mutation_result = mutation_store.reconcile_route_binding_record(
            expected_record=reconcile_plan.current_record,
            replacement_record=reconcile_plan.record,
            mutation=DbOnlyMutationRequest(
                scope=idempotency_scope(identity),
                route_path=_ROUTE_BINDING_RECONCILE_ROUTE,
                idempotency_key=normalized_key,
                request_fingerprint=payload_fingerprint,
                lease_owner=trace_id,
                response_status_code=202,
                response_trace_id=trace_id,
                response_payload=response.model_dump(mode="json", exclude_none=True),
                lease_seconds=int(_DB_ONLY_MUTATION_LEASE.total_seconds()),
            ),
        )
        if mutation_result.status in {"created", "refreshed", "unchanged"}:
            if mutation_result.status != expected_write_status:
                raise RuntimeError("Route binding reconcile write did not match the reviewed plan.")
            return response
        if mutation_result.status == "replayed":
            if mutation_result.idempotency_record is None:
                raise RuntimeError("Replayed route binding reconcile requires evidence.")
            return replay_idempotent_response(
                trace_id=trace_id,
                stored_record=mutation_result.idempotency_record,
                route_path=_ROUTE_BINDING_RECONCILE_ROUTE,
            )
        if mutation_result.status == "idempotency_conflict":
            raise _launchplane_http_error(
                status_code=409,
                trace_id=trace_id,
                code="idempotency_key_reused",
                message=(
                    "Idempotency-Key was already used for a different "
                    "Launchplane request payload on this route."
                ),
            )
        if mutation_result.status == "reconciliation_required":
            raise _launchplane_http_error(
                status_code=409,
                trace_id=trace_id,
                code="mutation_reconciliation_required",
                message="The route binding mutation requires reconciliation before retry.",
            )
        if mutation_result.status in {"changed", "missing"}:
            raise _launchplane_http_error(
                status_code=409,
                trace_id=trace_id,
                code="route_binding_changed",
                message=(
                    "The route-binding record changed after planning. Read the current record "
                    "and reconcile again with its SHA-256 digest."
                ),
            )
        raise _launchplane_http_error(
            status_code=409,
            trace_id=trace_id,
            code="mutation_in_progress",
            message=(
                "A matching route binding reconcile is already running. "
                "Retry with the same Idempotency-Key."
            ),
        )

    async def run_odoo_testing_route_binding_refresh(
        request: Request,
        refresh_request: OdooTestingRouteBindingRefreshEnvelope,
        identity: Annotated[LaunchplaneIdentity, Depends(read_write_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> AcceptedEvidenceResponse:
        trace_id = next_trace_id()
        controller_action = (
            "route_binding.odoo_testing_refresh.apply"
            if refresh_request.mode == "apply"
            else "route_binding.odoo_testing_refresh.plan"
        )
        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action=controller_action,
            product="launchplane",
            context=_LAUNCHPLANE_SERVICE_CONTEXT,
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Workflow cannot run the Odoo testing route binding refresh controller.",
            )
        if refresh_request.mode == "apply" and not idempotency_key.strip():
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="idempotency_key_required",
                message=(
                    "Odoo testing route binding refresh apply requires an Idempotency-Key header."
                ),
            )
        (
            normalized_key,
            payload_fingerprint,
            _,
        ) = await replay_apply_idempotency(
            request=request,
            record_store=record_store,
            identity=identity,
            route_path=_ODOO_TESTING_ROUTE_BINDING_REFRESH_ROUTE,
            idempotency_key=idempotency_key,
            trace_id=trace_id,
            check_replay=False,
        )
        try:
            controller_store = require_route_binding_refresh_controller_read_store(record_store)
            mutation_store = (
                require_route_binding_refresh_controller_store(record_store)
                if refresh_request.mode == "apply"
                else None
            )
        except TypeError as error:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message=str(error),
            ) from error

        try:
            controller_plan = control_plane_route_binding_refresh_controller.plan_odoo_testing_route_binding_refresh(
                record_store=controller_store,
                evaluated_at=utc_now_timestamp(),
                target_limit=_ODOO_TESTING_ROUTE_BINDING_REFRESH_TARGET_LIMIT,
            )
        except (
            control_plane_route_binding_refresh_controller.RouteBindingRefreshTargetLimitExceeded
        ) as error:
            raise _launchplane_http_error(
                status_code=409,
                trace_id=trace_id,
                code="route_binding_refresh_target_limit_exceeded",
                message=str(error),
            ) from error
        except (
            control_plane_route_binding_refresh_controller.RouteBindingRefreshTargetInvariantError
        ) as error:
            raise _launchplane_http_error(
                status_code=409,
                trace_id=trace_id,
                code="route_binding_refresh_target_invalid",
                message=str(error),
            ) from error
        binding_action = (
            "route_binding.apply" if refresh_request.mode == "apply" else "route_binding.read"
        )
        for outcome in controller_plan.outcomes:
            ensure_route_binding_allowed(
                identity=identity,
                trace_id=trace_id,
                action=binding_action,
                product=outcome.product,
                context_name=outcome.context,
                instance_name=outcome.instance,
                message="Workflow cannot refresh a discovered Odoo testing route binding.",
            )

        controller_reservation: LaunchplaneIdempotencyRecord | None = None
        if refresh_request.mode == "apply":
            if mutation_store is None:
                raise RuntimeError(
                    "Odoo testing route binding refresh apply requires a mutation store."
                )
            preflight = mutation_store.prepare_db_only_mutation(
                scope=idempotency_scope(identity),
                route_path=_ODOO_TESTING_ROUTE_BINDING_REFRESH_ROUTE,
                idempotency_key=normalized_key,
                request_fingerprint=payload_fingerprint,
            )
            if preflight.status not in {"missing", "released"}:
                if preflight.record is None:
                    raise RuntimeError(
                        "Odoo testing route binding refresh preflight requires evidence."
                    )
                if preflight.status == "replayed":
                    return replay_idempotent_response(
                        trace_id=trace_id,
                        stored_record=preflight.record,
                        route_path=_ODOO_TESTING_ROUTE_BINDING_REFRESH_ROUTE,
                    )
                if preflight.status == "conflict":
                    raise _launchplane_http_error(
                        status_code=409,
                        trace_id=trace_id,
                        code="idempotency_key_reused",
                        message=(
                            "Idempotency-Key was already used for a different "
                            "Launchplane request payload on this route."
                        ),
                    )
                if preflight.status == "in_progress":
                    raise _launchplane_http_error(
                        status_code=409,
                        trace_id=trace_id,
                        code="mutation_in_progress",
                        message=(
                            "A matching Odoo testing route-binding refresh is already "
                            "running. Retry with the same Idempotency-Key."
                        ),
                    )
                if preflight.status == "reconcile_required":
                    raise _launchplane_http_error(
                        status_code=409,
                        trace_id=trace_id,
                        code="mutation_reconciliation_required",
                        message=(
                            "The prior Odoo testing route-binding refresh requires "
                            "reconciliation before retry."
                        ),
                    )
                raise RuntimeError(
                    "Unsupported Odoo testing route-binding refresh preflight status: "
                    f"{preflight.status}"
                )
            reservation_result = mutation_store.reserve_mutation(
                scope=idempotency_scope(identity),
                route_path=_ODOO_TESTING_ROUTE_BINDING_REFRESH_ROUTE,
                idempotency_key=normalized_key,
                request_fingerprint=payload_fingerprint,
                lease_owner=f"{trace_id}:controller",
                lease_seconds=int(_DB_ONLY_MUTATION_LEASE.total_seconds()),
            )
            if reservation_result.status == "acquired":
                controller_reservation = reservation_result.record
            elif reservation_result.status == "replayed":
                return replay_idempotent_response(
                    trace_id=trace_id,
                    stored_record=reservation_result.record,
                    route_path=_ODOO_TESTING_ROUTE_BINDING_REFRESH_ROUTE,
                )
            elif reservation_result.status == "conflict":
                raise _launchplane_http_error(
                    status_code=409,
                    trace_id=trace_id,
                    code="idempotency_key_reused",
                    message=(
                        "Idempotency-Key was already used for a different "
                        "Launchplane request payload on this route."
                    ),
                )
            elif reservation_result.status in {"in_progress", "target_busy"}:
                raise _launchplane_http_error(
                    status_code=409,
                    trace_id=trace_id,
                    code="mutation_in_progress",
                    message=(
                        "A matching Odoo testing route-binding refresh is already running. "
                        "Retry with the same Idempotency-Key."
                    ),
                )
            elif reservation_result.status == "reconcile_required":
                raise _launchplane_http_error(
                    status_code=409,
                    trace_id=trace_id,
                    code="mutation_reconciliation_required",
                    message=(
                        "The prior Odoo testing route-binding refresh requires "
                        "reconciliation before retry."
                    ),
                )
            else:
                raise RuntimeError(
                    "Unsupported Odoo testing route-binding refresh reservation status: "
                    f"{reservation_result.status}"
                )

        result_outcomes: list[dict[str, object]] = []
        for outcome in controller_plan.outcomes:
            outcome_payload = outcome.model_dump(mode="json", exclude_none=True)
            outcome_payload.pop("reconcile_plan", None)
            if refresh_request.mode == "dry-run" or outcome.status != "planned_refresh":
                result_outcomes.append(outcome_payload)
                continue
            reconcile_plan = outcome.reconcile_plan
            if (
                reconcile_plan is None
                or reconcile_plan.current_record is None
                or reconcile_plan.record is None
                or reconcile_plan.operation != "refresh"
            ):
                outcome_payload["status"] = "conflict"
                outcome_payload["findings"] = [
                    {
                        "code": "route_binding_refresh_plan_invalid",
                        "detail": (
                            "Odoo testing refresh controller received a non-refresh plan for "
                            "an enrolled binding."
                        ),
                    }
                ]
                result_outcomes.append(outcome_payload)
                continue
            if mutation_store is None:
                raise RuntimeError(
                    "Odoo testing route binding refresh apply requires a mutation store."
                )
            binding_key = reconcile_plan.current_record.binding_key
            binding_token = hashlib.sha256(binding_key.encode()).hexdigest()[:16]
            binding_idempotency_key = f"{normalized_key}:{binding_token}"
            binding_response_payload: dict[str, object] = {
                "status": "accepted",
                "trace_id": trace_id,
                "records": {
                    "product": outcome.product,
                    "context": outcome.context,
                    "instance": outcome.instance,
                    "route_binding_status": "refreshed",
                },
                "result": outcome_payload,
            }
            mutation_result = mutation_store.reconcile_route_binding_record(
                expected_record=reconcile_plan.current_record,
                replacement_record=reconcile_plan.record,
                mutation=DbOnlyMutationRequest(
                    scope=idempotency_scope(identity),
                    route_path=_ODOO_TESTING_ROUTE_BINDING_REFRESH_ROUTE,
                    idempotency_key=binding_idempotency_key,
                    request_fingerprint=idempotency_request_fingerprint(
                        route_path=_ODOO_TESTING_ROUTE_BINDING_REFRESH_ROUTE,
                        payload={
                            "controller_request_fingerprint": payload_fingerprint,
                            "binding_key": binding_key,
                            "current_record_sha256": outcome.current_record_sha256,
                            "candidate_record_sha256": outcome.candidate_record_sha256,
                        },
                    ),
                    lease_owner=f"{trace_id}:{binding_token}",
                    response_status_code=202,
                    response_trace_id=trace_id,
                    response_payload=binding_response_payload,
                    lease_seconds=int(_DB_ONLY_MUTATION_LEASE.total_seconds()),
                ),
            )
            if mutation_result.status == "refreshed":
                outcome_payload["status"] = "refreshed"
            elif mutation_result.status == "unchanged":
                outcome_payload["status"] = "unchanged"
            elif mutation_result.status == "replayed":
                outcome_payload["status"] = "replayed"
            else:
                outcome_payload["status"] = "conflict"
                outcome_payload["findings"] = [
                    {
                        "code": f"route_binding_refresh_{mutation_result.status}",
                        "detail": (
                            "The route binding changed or another refresh owns the current "
                            "mutation. Read current authority and retry after it settles with "
                            "a new Idempotency-Key."
                        ),
                    }
                ]
            result_outcomes.append(outcome_payload)

        attention_statuses = {"blocked", "conflict"}
        response_status = (
            "empty"
            if not result_outcomes
            else (
                "attention"
                if any(
                    string_value(outcome.get("status") or "") in attention_statuses
                    for outcome in result_outcomes
                )
                else "ok"
            )
        )
        response = accepted_evidence_response(
            trace_id=trace_id,
            records={
                "route_binding_refresh_status": response_status,
                "route_binding_refresh_target_count": len(result_outcomes),
            },
            result={
                "status": response_status,
                "mode": refresh_request.mode,
                "evaluated_at": controller_plan.evaluated_at,
                "target_count": len(result_outcomes),
                "unchanged_count": sum(
                    1 for outcome in result_outcomes if outcome.get("status") == "unchanged"
                ),
                "planned_refresh_count": sum(
                    1 for outcome in result_outcomes if outcome.get("status") == "planned_refresh"
                ),
                "refreshed_count": sum(
                    1 for outcome in result_outcomes if outcome.get("status") == "refreshed"
                ),
                "replayed_count": sum(
                    1 for outcome in result_outcomes if outcome.get("status") == "replayed"
                ),
                "blocked_count": sum(
                    1 for outcome in result_outcomes if outcome.get("status") == "blocked"
                ),
                "conflict_count": sum(
                    1 for outcome in result_outcomes if outcome.get("status") == "conflict"
                ),
                "outcomes": result_outcomes,
            },
        )
        if refresh_request.mode == "apply":
            if mutation_store is None or controller_reservation is None:
                raise RuntimeError(
                    "Odoo testing route binding refresh apply requires a parent reservation."
                )
            completion_result = mutation_store.complete_mutation_reservation(
                completion=complete_launchplane_mutation_reservation(
                    controller_reservation,
                    response_status_code=202,
                    response_trace_id=trace_id,
                    completed_at=utc_now_timestamp(),
                    response_payload=response.model_dump(mode="json", exclude_none=True),
                )
            )
            if completion_result.status == "completed":
                return response
            if completion_result.status == "replayed":
                if completion_result.record is None:
                    raise RuntimeError(
                        "Replayed Odoo testing route-binding refresh requires evidence."
                    )
                return replay_idempotent_response(
                    trace_id=trace_id,
                    stored_record=completion_result.record,
                    route_path=_ODOO_TESTING_ROUTE_BINDING_REFRESH_ROUTE,
                )
            if completion_result.status == "conflict":
                raise _launchplane_http_error(
                    status_code=409,
                    trace_id=trace_id,
                    code="idempotency_key_reused",
                    message=(
                        "Idempotency-Key was already used for a different "
                        "Launchplane request payload on this route."
                    ),
                )
            if completion_result.status == "reconcile_required":
                raise _launchplane_http_error(
                    status_code=409,
                    trace_id=trace_id,
                    code="mutation_reconciliation_required",
                    message=(
                        "The Odoo testing route-binding refresh requires reconciliation "
                        "before retry."
                    ),
                )
            raise _launchplane_http_error(
                status_code=409,
                trace_id=trace_id,
                code="mutation_completion_conflict",
                message=(
                    "The Odoo testing route-binding refresh could not complete its parent "
                    "reservation. Retry with the same Idempotency-Key."
                ),
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
            native_routes.native_driver_route_authz_action(apply_ingress_route)
            if route_request.ingress.mode == "apply"
            else native_routes.native_driver_route_alternate_authz_action(apply_ingress_route)
        )
        instance_name = route_request.instance.strip()
        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action=authz_action,
            product=route_request.product,
            context=route_request.context,
            target=(
                AuthorizationTarget(scope="instance", instances=(instance_name,))
                if instance_name
                else AuthorizationTarget(scope="context")
            ),
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message=(
                    "Workflow cannot plan or apply the ingress route for the requested "
                    + ("product/context/instance." if instance_name else "product/context.")
                ),
            )
        if instance_name:
            try:
                profile_store = require_product_profile_read_store(record_store)
                profile = profile_store.read_product_profile_record(route_request.product)
                control_plane_ingress_route_scope.validate_ingress_route_instance_scope(
                    profile=profile,
                    context=route_request.context,
                    instance=instance_name,
                    requested_domains=route_request.ingress.route.domain_names,
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
                    status_code=400,
                    trace_id=trace_id,
                    code="invalid_ingress_instance_scope",
                    message="Ingress route instance scope requires an existing product profile.",
                ) from error
            except control_plane_ingress_route_scope.IngressRouteInstanceScopeError as error:
                raise _launchplane_http_error(
                    status_code=400,
                    trace_id=trace_id,
                    code=error.code,
                    message=error.message,
                ) from error
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

        audit_ingress_request = resolved_ingress_request
        provider_ingress_request = resolved_ingress_request
        preflight_result: NpmplusIngressApplyResult | None = None
        guarded_instance_apply = False
        try:
            ingress_provider = resolved_ingress_provider_factory()
            if instance_name and resolved_ingress_request.mode == "apply":
                if resolved_ingress_request.expected_host_id is None:
                    raise _launchplane_http_error(
                        status_code=400,
                        trace_id=trace_id,
                        code="instance_scoped_ingress_expected_host_required",
                        message="Instance-scoped ingress apply requires expected_host_id.",
                    )
                if resolved_ingress_request.allow_create:
                    raise _launchplane_http_error(
                        status_code=400,
                        trace_id=trace_id,
                        code="instance_scoped_ingress_create_forbidden",
                        message="Instance-scoped ingress apply cannot create a provider route.",
                    )
                if not resolved_ingress_request.require_exact_expected_host_domains:
                    raise _launchplane_http_error(
                        status_code=400,
                        trace_id=trace_id,
                        code="instance_scoped_ingress_exact_domains_required",
                        message=(
                            "Instance-scoped ingress apply requires exact expected-host domains."
                        ),
                    )
                if not resolved_ingress_request.route.edge_endpoint_key.strip():
                    raise _launchplane_http_error(
                        status_code=400,
                        trace_id=trace_id,
                        code="instance_scoped_ingress_edge_endpoint_required",
                        message="Instance-scoped ingress apply requires a DB-backed edge endpoint.",
                    )
            if (
                instance_name
                and resolved_ingress_request.expected_host_id is not None
                and resolved_ingress_request.require_exact_expected_host_domains
            ):
                provider_ingress_request, preflight_result = (
                    plan_instance_scoped_existing_ingress_route(
                        ingress_provider=ingress_provider,
                        request=resolved_ingress_request,
                    )
                )
            if instance_name and resolved_ingress_request.mode == "apply":
                if preflight_result is None:
                    preflight_result = ingress_provider.apply_route(
                        request=provider_ingress_request.model_copy(update={"mode": "dry-run"})
                    )
                if preflight_result.status != "unchanged" or any(
                    operation.requires_apply for operation in preflight_result.operations
                ):
                    raise _launchplane_http_error(
                        status_code=409,
                        trace_id=trace_id,
                        code="instance_scoped_ingress_change_forbidden",
                        message=(
                            "Instance-scoped ingress apply can record only a reviewed no-op route."
                        ),
                    )
                provider_ingress_request = provider_ingress_request.model_copy(
                    update={
                        "allow_create": False,
                        "allow_update": False,
                        "allow_enable_disable": False,
                    }
                )
            if resolved_ingress_request.mode == "apply":
                write_ingress_route_pending_audit_record(
                    ingress_store=ingress_store,
                    trace_id=trace_id,
                    product=route_request.product,
                    context=route_request.context,
                    provider=ingress_provider.provider_id,
                    request=audit_ingress_request,
                    idempotency_key=normalized_key,
                )
                guarded_instance_apply = bool(instance_name)
            if resolved_ingress_request.mode == "dry-run" and preflight_result is not None:
                ingress_result = preflight_result
            else:
                ingress_result = ingress_provider.apply_route(request=provider_ingress_request)
        except (ValueError, click.ClickException) as error:
            if guarded_instance_apply:
                raise _launchplane_http_error(
                    status_code=409,
                    trace_id=trace_id,
                    code="instance_scoped_ingress_changed",
                    message=(
                        "The provider route changed after review; repeat the exact-instance dry run."
                    ),
                ) from error
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request could not be completed.",
            ) from error
        response_ingress_result = ingress_result
        response_records: dict[str, object] = {
            "ingress_provider": ingress_provider.provider_id,
        }
        if instance_name:
            provider_domain_count = (
                len(ingress_result.proxy_host.domain_names)
                if ingress_result.proxy_host is not None
                else 0
            )
            comparison_scope = (
                "full_expected_host"
                if frozenset(provider_ingress_request.route.domain_names)
                != frozenset(audit_ingress_request.route.domain_names)
                else "requested_domains"
            )
            response_records.update(
                {
                    "ingress_provider_domain_comparison": comparison_scope,
                    "ingress_provider_domain_count": provider_domain_count,
                }
            )
            response_ingress_result = scope_instance_ingress_result(
                result=ingress_result,
                requested_domains=audit_ingress_request.route.domain_names,
            )
        ingress_audit_record = write_ingress_route_audit_record(
            ingress_store=ingress_store,
            trace_id=trace_id,
            product=route_request.product,
            context=route_request.context,
            provider=ingress_provider.provider_id,
            request=audit_ingress_request,
            result=response_ingress_result,
            idempotency_key=normalized_key,
        )
        response_records["ingress_route_audit_record_id"] = ingress_audit_record.record_id
        response = accepted_evidence_response(
            trace_id=trace_id,
            records=response_records,
            result=response_ingress_result.model_dump(mode="json"),
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

    async def apply_preview_lifecycle_plan(
        request: Request,
        plan_request: PreviewLifecyclePlanEnvelope,
        identity: Annotated[LaunchplaneIdentity, Depends(read_write_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> AcceptedEvidenceResponse:
        trace_id = next_trace_id()
        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action="preview_lifecycle.plan",
            product=plan_request.product,
            context=plan_request.context,
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message=(
                    "Workflow cannot plan preview lifecycle for the requested product/context."
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
            route_path=_PREVIEW_LIFECYCLE_PLAN_ROUTE,
            idempotency_key=idempotency_key,
            trace_id=trace_id,
            check_replay=True,
        )
        if replayed_response is not None:
            return replayed_response
        try:
            plan_store = require_preview_lifecycle_plan_apply_store(record_store)
        except TypeError as error:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message=str(error),
            ) from error
        inventory_scans = plan_store.list_preview_inventory_scan_records(
            context_name=plan_request.context,
            limit=1,
        )
        plan_record = build_preview_lifecycle_plan(
            product=plan_request.product,
            context=plan_request.context,
            planned_at=utc_now_timestamp(),
            source=plan_request.source,
            desired_previews=plan_request.desired_previews,
            desired_state_id=plan_request.desired_state_id,
            latest_inventory_scan=next(iter(inventory_scans), None),
        )
        plan_store.write_preview_lifecycle_plan_record(plan_record)
        response = accepted_evidence_response(
            trace_id=trace_id,
            records={"preview_lifecycle_plan_id": plan_record.plan_id},
            result=plan_record.model_dump(mode="json"),
        )
        store_apply_idempotency(
            record_store=record_store,
            identity=identity,
            route_path=_PREVIEW_LIFECYCLE_PLAN_ROUTE,
            idempotency_key=normalized_key,
            request_fingerprint_value=payload_fingerprint,
            trace_id=trace_id,
            response=response,
        )
        return response

    async def apply_preview_lifecycle_cleanup(
        request: Request,
        cleanup_request: PreviewLifecycleCleanupEnvelope,
        identity: Annotated[LaunchplaneIdentity, Depends(read_write_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> AcceptedEvidenceResponse:
        trace_id = next_trace_id()
        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action="preview_lifecycle.cleanup",
            product=cleanup_request.product,
            context=cleanup_request.context,
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message=(
                    "Workflow cannot clean preview lifecycle for the requested product/context."
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
            route_path=_PREVIEW_LIFECYCLE_CLEANUP_ROUTE,
            idempotency_key=idempotency_key,
            trace_id=trace_id,
            check_replay=True,
        )
        if replayed_response is not None:
            return replayed_response
        try:
            cleanup_store = require_preview_lifecycle_cleanup_apply_store(record_store)
            cleanup_mutation_store = (
                require_preview_lifecycle_cleanup_mutation_store(record_store)
                if cleanup_request.apply
                else cleanup_store
            )
        except TypeError as error:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message=str(error),
            ) from error
        plan = latest_preview_lifecycle_plan(
            record_store=cleanup_store,
            context_name=cleanup_request.context,
            plan_id=cleanup_request.plan_id,
        )
        if plan is None or plan.product != cleanup_request.product:
            raise _launchplane_http_error(
                status_code=404,
                trace_id=trace_id,
                code="not_found",
                message=(
                    "Preview lifecycle cleanup requires an existing plan for the requested"
                    " product/context."
                ),
            )
        cleanup_driver_id, cleanup_slug_template = preview_lifecycle_cleanup_profile_settings(
            record_store=record_store,
            product=cleanup_request.product,
        )
        cleanup_record = build_preview_lifecycle_cleanup_record(
            plan=plan,
            requested_at=utc_now_timestamp(),
            source=cleanup_request.source,
            apply=cleanup_request.apply,
            destroy_reason=cleanup_request.destroy_reason,
            control_plane_root=resolved_control_plane_root,
            record_store=cleanup_mutation_store,
            timeout_seconds=cleanup_request.timeout_seconds,
            driver_id=cleanup_driver_id,
            preview_slug_template=cleanup_slug_template,
        )
        preview_lifecycle_cleanup_id = write_preview_lifecycle_cleanup_apply_record(
            record_store=cleanup_store,
            record=cleanup_record,
        )
        response = accepted_evidence_response(
            trace_id=trace_id,
            records={"preview_lifecycle_cleanup_id": preview_lifecycle_cleanup_id},
            result=cleanup_record.model_dump(mode="json"),
        )
        store_apply_idempotency(
            record_store=record_store,
            identity=identity,
            route_path=_PREVIEW_LIFECYCLE_CLEANUP_ROUTE,
            idempotency_key=normalized_key,
            request_fingerprint_value=payload_fingerprint,
            trace_id=trace_id,
            response=response,
        )
        return response

    async def apply_preview_lifecycle_sweep(
        request: Request,
        sweep_request: PreviewLifecycleSweepEnvelope,
        identity: Annotated[LaunchplaneIdentity, Depends(read_write_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> AcceptedEvidenceResponse:
        trace_id = next_trace_id()
        try:
            sweep_store = require_preview_lifecycle_sweep_store(record_store)
            requested_sweep_profiles = preview_lifecycle_sweep_profiles(
                record_store=sweep_store,
                product=sweep_request.product,
            )
        except TypeError as error:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message=str(error),
            ) from error
        if sweep_request.product.strip() and not requested_sweep_profiles:
            raise _launchplane_http_error(
                status_code=404,
                trace_id=trace_id,
                code="not_found",
                message=(
                    "Preview lifecycle sweep found no enabled preview profile for the"
                    " requested product."
                ),
            )
        denied_profile: LaunchplaneProductProfileRecord | None = None
        denied_action = ""
        for profile in requested_sweep_profiles:
            for action in ("preview_lifecycle.plan", "preview_lifecycle.cleanup"):
                if not resolved_authz_policy_runtime.policy.allows(
                    identity=identity,
                    action=action,
                    product=profile.product,
                    context=profile.preview.context,
                ):
                    denied_profile = profile
                    denied_action = action
                    break
            if denied_profile is not None:
                break
        if denied_profile is not None:
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message=(
                    "Workflow cannot sweep preview lifecycle for one or more enabled"
                    " product profiles."
                ),
                authz=_authz_diagnostic_payload(
                    identity=identity,
                    authz_policy_sha256_value=resolved_authz_policy_runtime.policy_sha256,
                    authz_policy_source=resolved_authz_policy_runtime.source,
                    action=denied_action,
                    product=denied_profile.product,
                    context=denied_profile.preview.context,
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
            route_path=_PREVIEW_LIFECYCLE_SWEEP_ROUTE,
            idempotency_key=idempotency_key,
            trace_id=trace_id,
            check_replay=True,
        )
        if replayed_response is not None:
            return replayed_response
        sweep_result = build_preview_lifecycle_sweep(
            control_plane_root=resolved_control_plane_root,
            record_store=sweep_store,
            request=sweep_request,
        )
        response = accepted_evidence_response(
            trace_id=trace_id,
            records={},
            result=sweep_result,
        )
        store_apply_idempotency(
            record_store=record_store,
            identity=identity,
            route_path=_PREVIEW_LIFECYCLE_SWEEP_ROUTE,
            idempotency_key=normalized_key,
            request_fingerprint_value=payload_fingerprint,
            trace_id=trace_id,
            response=response,
        )
        return response

    async def execute_product_retirement(
        request: Request,
        retirement_request: ProductRetirementRequest,
        identity: Annotated[LaunchplaneIdentity, Depends(read_write_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> AcceptedEvidenceResponse:
        trace_id = next_trace_id()
        normalized_key = idempotency_key.strip()
        if not normalized_key:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="idempotency_key_required",
                message="Product retirement requires Idempotency-Key.",
            )
        action = (
            "product_retirement.plan"
            if retirement_request.mode == "plan"
            else "product_retirement.apply"
        )
        if not resolved_authz_policy_runtime.policy.allows_product_instance_preflight(
            identity=identity,
            action=action,
            product=retirement_request.product,
            instance=retirement_request.instance,
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Identity is not authorized for product retirement.",
            )
        bound: control_plane_product_retirement.BoundProductRetirement | None = None
        try:
            retirement_store = cast(
                control_plane_product_retirement.ProductRetirementStore,
                require_provider_operation_store(
                    record_store=record_store,
                    trace_id=trace_id,
                ),
            )
            if retirement_request.mode == "apply":
                plan_record = retirement_store.read_product_retirement_record(
                    retirement_request.reviewed_plan_record_id
                )
                control_plane_product_retirement.validate_reviewed_product_retirement_plan(
                    request=retirement_request,
                    plan=plan_record,
                )
                effective_context = plan_record.context
            else:
                bound = control_plane_product_retirement.bind_product_retirement_authority(
                    record_store=retirement_store,
                    request=retirement_request,
                )
                effective_context = bound.context
                plan_record = None
        except FileNotFoundError as error:
            raise _launchplane_http_error(
                status_code=404,
                trace_id=trace_id,
                code="product_retirement_authority_not_found",
                message=str(error),
            ) from error
        except TypeError as error:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message=str(error),
            ) from error
        except (ValueError, click.ClickException) as error:
            raise _launchplane_http_error(
                status_code=409,
                trace_id=trace_id,
                code="product_retirement_blocked",
                message=str(error),
            ) from error

        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action=action,
            product=retirement_request.product,
            context=effective_context,
            target=AuthorizationTarget(
                scope="instance",
                instances=(retirement_request.instance,),
            ),
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Identity is not authorized for exact product retirement.",
                authz=_authz_diagnostic_payload(
                    identity=identity,
                    authz_policy_sha256_value=resolved_authz_policy_runtime.policy_sha256,
                    authz_policy_source=resolved_authz_policy_runtime.source,
                    action=action,
                    product=retirement_request.product,
                    context=effective_context,
                ),
            )

        actor_identity = product_retirement_identity(identity)
        requested_at = utc_now_timestamp()
        if retirement_request.mode == "plan":
            if bound is None:
                raise RuntimeError("Product retirement plan authority was not bound.")
            existing_plans = retirement_store.list_product_retirement_records(
                product=retirement_request.product,
                actor=actor_identity.actor,
                mode="plan",
                idempotency_key=normalized_key,
                limit=1,
            )
            if existing_plans:
                existing_plan = existing_plans[0]
                if existing_plan.continuity_sha256 != retirement_request.continuity_sha256:
                    raise _launchplane_http_error(
                        status_code=409,
                        trace_id=trace_id,
                        code="idempotency_key_reused",
                        message=(
                            "Idempotency-Key was already used for a different product "
                            "retirement plan."
                        ),
                    )
                return AcceptedEvidenceResponse.model_validate(
                    control_plane_product_retirement.redacted_product_retirement_response(
                        existing_plan
                    )
                )
            try:
                observation = control_plane_product_retirement.observe_tracked_dokploy_application(
                    control_plane_root=resolved_control_plane_root,
                    target_id=bound.provider_target.target_id,
                    observed_at=requested_at,
                )
                plan = control_plane_product_retirement.build_product_retirement_plan_record(
                    request=retirement_request,
                    identity=actor_identity,
                    trace_id=trace_id,
                    idempotency_key=normalized_key,
                    requested_at=requested_at,
                    bound=bound,
                    observation=observation,
                )
                retirement_store.write_product_retirement_record(plan)
                persisted_plans = retirement_store.list_product_retirement_records(
                    product=retirement_request.product,
                    actor=actor_identity.actor,
                    mode="plan",
                    idempotency_key=normalized_key,
                    limit=1,
                )
                if not persisted_plans:
                    raise RuntimeError("Product retirement plan reservation was not persisted.")
                plan = persisted_plans[0]
            except FileNotFoundError as error:
                raise _launchplane_http_error(
                    status_code=404,
                    trace_id=trace_id,
                    code="product_retirement_authority_not_found",
                    message=str(error),
                ) from error
            except (ValueError, click.ClickException) as error:
                raise _launchplane_http_error(
                    status_code=409,
                    trace_id=trace_id,
                    code="product_retirement_blocked",
                    message=str(error),
                ) from error
            return AcceptedEvidenceResponse.model_validate(
                control_plane_product_retirement.redacted_product_retirement_response(plan)
            )

        if plan_record is None:
            raise RuntimeError("Product retirement apply requires a reviewed plan record.")
        (
            normalized_key,
            payload_fingerprint,
            replayed_response,
        ) = await replay_apply_idempotency(
            request=request,
            record_store=retirement_store,
            identity=identity,
            route_path=_PRODUCT_RETIREMENT_ROUTE,
            idempotency_key=normalized_key,
            trace_id=trace_id,
            check_replay=True,
            request_payload=retirement_request.model_dump(mode="json"),
        )
        if replayed_response is not None:
            return replayed_response
        adapter = control_plane_product_retirement.DokployProductRetirementAdapter(
            control_plane_root=resolved_control_plane_root,
            record_store=retirement_store,
            request=retirement_request,
            plan=plan_record,
            identity=actor_identity,
            trace_id=trace_id,
            idempotency_key=normalized_key,
            requested_at=requested_at,
        )
        provider_operation_key = adapter.provider_operation_key(
            scope=idempotency_scope(identity),
            route_path=_PRODUCT_RETIREMENT_ROUTE,
            fingerprint=payload_fingerprint,
        )
        try:
            return await run_provider_mutation(
                record_store=retirement_store,
                identity=identity,
                route_path=_PRODUCT_RETIREMENT_ROUTE,
                idempotency_key=normalized_key,
                request_fingerprint=payload_fingerprint,
                trace_id=trace_id,
                adapter=adapter,
                in_progress_message=(
                    "A matching product retirement is already running. Retry with the same "
                    "Idempotency-Key."
                ),
                reconcile_message=(
                    "Product retirement requires reconciliation before retrying the same "
                    "Idempotency-Key."
                ),
            )
        except HTTPException as error:
            if not adapter.started:
                raise
            detail = error.detail if isinstance(error.detail, Mapping) else {}
            code = str(detail.get("code") or "product_retirement_failed")
            outcome = "reconcile_required" if "reconciliation" in code else "failed"
            terminal = adapter.terminal_record(
                outcome=outcome,
                provider_operation_key=provider_operation_key,
                error_code=code,
                error_message=str(detail.get("message") or ""),
            )
            retirement_store.write_product_retirement_record(terminal)
            raise
        except (ValueError, click.ClickException) as error:
            if not adapter.started:
                raise _launchplane_http_error(
                    status_code=409,
                    trace_id=trace_id,
                    code="product_retirement_blocked",
                    message=str(error),
                ) from error
            terminal = adapter.terminal_record(
                outcome="failed",
                provider_operation_key=provider_operation_key,
                error_code="product_retirement_blocked",
                error_message=str(error),
            )
            retirement_store.write_product_retirement_record(terminal)
            raise _launchplane_http_error(
                status_code=409,
                trace_id=trace_id,
                code="product_retirement_blocked",
                message=str(error),
            ) from error

    async def execute_detached_application_retirement(
        request: Request,
        retirement_request: DetachedApplicationRetirementRequest,
        identity: Annotated[LaunchplaneIdentity, Depends(read_write_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> AcceptedEvidenceResponse:
        trace_id = next_trace_id()
        normalized_key = idempotency_key.strip()
        if not normalized_key:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="idempotency_key_required",
                message="Detached application retirement requires Idempotency-Key.",
            )
        action = (
            "detached_application_retirement.plan"
            if retirement_request.mode == "plan"
            else "detached_application_retirement.apply"
        )
        if not detached_application_retirement_identity_allowed(identity):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message=(
                    "Detached application retirement requires the exact reusable worker "
                    "or a local admin identity."
                ),
            )
        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action=action,
            product=_LAUNCHPLANE_SERVICE_CONTEXT,
            context=_LAUNCHPLANE_SERVICE_CONTEXT,
            target=AuthorizationTarget(scope="global"),
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Identity is not authorized for detached application retirement.",
                authz=_authz_diagnostic_payload(
                    identity=identity,
                    authz_policy_sha256_value=resolved_authz_policy_runtime.policy_sha256,
                    authz_policy_source=resolved_authz_policy_runtime.source,
                    action=action,
                    product=_LAUNCHPLANE_SERVICE_CONTEXT,
                    context=_LAUNCHPLANE_SERVICE_CONTEXT,
                ),
            )
        try:
            retirement_store = cast(
                control_plane_detached_application_retirement.DetachedApplicationRetirementStore,
                cast(
                    object,
                    require_provider_operation_store(
                        record_store=record_store,
                        trace_id=trace_id,
                    ),
                ),
            )
            plan_record = None
            if retirement_request.mode == "apply":
                plan_record = retirement_store.read_detached_application_retirement_record(
                    retirement_request.reviewed_plan_record_id
                )
                control_plane_detached_application_retirement.validate_reviewed_detached_application_retirement_plan(
                    request=retirement_request,
                    plan=plan_record,
                )
        except FileNotFoundError as error:
            raise _launchplane_http_error(
                status_code=404,
                trace_id=trace_id,
                code="detached_application_retirement_plan_not_found",
                message="Reviewed detached application retirement plan was not found.",
            ) from error
        except TypeError as error:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message=str(error),
            ) from error
        except (ValueError, click.ClickException) as error:
            safe_message = control_plane_detached_application_retirement.redacted_detached_application_retirement_error(
                error
            )
            raise _launchplane_http_error(
                status_code=409,
                trace_id=trace_id,
                code="detached_application_retirement_blocked",
                message=safe_message,
            ) from error
        actor_identity = detached_application_retirement_identity(identity)
        requested_at = utc_now_timestamp()
        if retirement_request.mode == "plan":
            existing_plans = retirement_store.list_detached_application_retirement_records(
                candidate_target_sha256=retirement_request.candidate_target_sha256,
                actor=actor_identity.actor,
                mode="plan",
                idempotency_key=normalized_key,
                limit=1,
            )
            if existing_plans:
                existing_plan = existing_plans[0]
                if existing_plan.continuity_sha256 != retirement_request.continuity_sha256:
                    raise _launchplane_http_error(
                        status_code=409,
                        trace_id=trace_id,
                        code="idempotency_key_reused",
                        message=(
                            "Idempotency-Key was already used for a different detached "
                            "application retirement plan."
                        ),
                    )
                return AcceptedEvidenceResponse.model_validate(
                    control_plane_detached_application_retirement.redacted_detached_application_retirement_response(
                        existing_plan
                    )
                )
            try:
                discovery = (
                    control_plane_detached_application_retirement.discover_detached_application(
                        control_plane_root=resolved_control_plane_root,
                        request=retirement_request,
                        observed_at=requested_at,
                    )
                )
                absence_proof = control_plane_detached_application_retirement.prove_detached_application_authority_absence(
                    record_store=retirement_store,
                    candidate_target_id=discovery.candidate.application_id,
                    candidate_application_name=retirement_request.application_name,
                )
                plan = control_plane_detached_application_retirement.build_detached_application_retirement_plan_record(
                    request=retirement_request,
                    identity=actor_identity,
                    trace_id=trace_id,
                    idempotency_key=normalized_key,
                    requested_at=requested_at,
                    discovery=discovery,
                    authority_absence_proof=absence_proof,
                )
                retirement_store.write_detached_application_retirement_record(plan)
                persisted_plans = retirement_store.list_detached_application_retirement_records(
                    candidate_target_sha256=retirement_request.candidate_target_sha256,
                    actor=actor_identity.actor,
                    mode="plan",
                    idempotency_key=normalized_key,
                    limit=1,
                )
                if not persisted_plans:
                    raise RuntimeError(
                        "Detached application retirement plan reservation was not persisted."
                    )
                plan = persisted_plans[0]
            except (
                FileNotFoundError,
                OSError,
                TimeoutError,
                ValueError,
                click.ClickException,
            ) as error:
                safe_message = control_plane_detached_application_retirement.redacted_detached_application_retirement_error(
                    error
                )
                raise _launchplane_http_error(
                    status_code=409,
                    trace_id=trace_id,
                    code="detached_application_retirement_blocked",
                    message=safe_message,
                ) from error
            return AcceptedEvidenceResponse.model_validate(
                control_plane_detached_application_retirement.redacted_detached_application_retirement_response(
                    plan
                )
            )
        if plan_record is None:
            raise RuntimeError(
                "Detached application retirement apply requires a reviewed plan record."
            )
        (
            normalized_key,
            payload_fingerprint,
            replayed_response,
        ) = await replay_apply_idempotency(
            request=request,
            record_store=retirement_store,
            identity=identity,
            route_path=_DETACHED_APPLICATION_RETIREMENT_ROUTE,
            idempotency_key=normalized_key,
            trace_id=trace_id,
            check_replay=True,
            request_payload=retirement_request.model_dump(mode="json"),
        )
        if replayed_response is not None:
            return replayed_response
        adapter = control_plane_detached_application_retirement.DokployDetachedApplicationRetirementAdapter(
            control_plane_root=resolved_control_plane_root,
            record_store=retirement_store,
            request=retirement_request,
            plan=plan_record,
            identity=actor_identity,
            trace_id=trace_id,
            idempotency_key=normalized_key,
            requested_at=requested_at,
        )
        provider_operation_key = adapter.provider_operation_key(
            scope=idempotency_scope(identity),
            route_path=_DETACHED_APPLICATION_RETIREMENT_ROUTE,
            fingerprint=payload_fingerprint,
        )
        try:
            return await run_provider_mutation(
                record_store=retirement_store,
                identity=identity,
                route_path=_DETACHED_APPLICATION_RETIREMENT_ROUTE,
                idempotency_key=normalized_key,
                request_fingerprint=payload_fingerprint,
                trace_id=trace_id,
                adapter=adapter,
                in_progress_message=(
                    "A matching detached application retirement is already running. "
                    "Retry with the same Idempotency-Key."
                ),
                reconcile_message=(
                    "Detached application retirement requires reconciliation before retrying "
                    "the same Idempotency-Key."
                ),
            )
        except HTTPException as error:
            if not adapter.started:
                raise
            detail = error.detail if isinstance(error.detail, Mapping) else {}
            code = str(detail.get("code") or "detached_application_retirement_failed")
            outcome: Literal["reconcile_required", "failed"] = (
                "reconcile_required" if "reconciliation" in code else "failed"
            )
            terminal = adapter.terminal_record(
                outcome=outcome,
                provider_operation_key=provider_operation_key,
                error_code=code,
                error_message=str(detail.get("message") or ""),
            )
            retirement_store.write_detached_application_retirement_record(terminal)
            raise
        except (ValueError, click.ClickException) as error:
            safe_message = control_plane_detached_application_retirement.redacted_detached_application_retirement_error(
                error
            )
            if not adapter.started:
                raise _launchplane_http_error(
                    status_code=409,
                    trace_id=trace_id,
                    code="detached_application_retirement_blocked",
                    message=safe_message,
                ) from error
            terminal = adapter.terminal_record(
                outcome="failed",
                provider_operation_key=provider_operation_key,
                error_code="detached_application_retirement_blocked",
                error_message=safe_message,
            )
            retirement_store.write_detached_application_retirement_record(terminal)
            raise _launchplane_http_error(
                status_code=409,
                trace_id=trace_id,
                code="detached_application_retirement_blocked",
                message=safe_message,
            ) from error

    async def remediate_preview_pr_feedback(
        request: Request,
        remediation_request: PreviewPrFeedbackRemediationRequest,
        identity: Annotated[LaunchplaneIdentity, Depends(read_write_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> AcceptedEvidenceResponse:
        trace_id = next_trace_id()
        if not isinstance(identity, LocalOperatorIdentity | LocalAdminIdentity):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Preview PR feedback remediation requires a local operator identity.",
            )
        action = (
            "preview_pr_feedback_remediation.plan"
            if remediation_request.mode == "dry-run"
            else "preview_pr_feedback_remediation.apply"
        )
        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action=action,
            product=remediation_request.product,
            context=remediation_request.context,
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Identity is not authorized for preview PR feedback remediation.",
            )
        if not idempotency_key.strip():
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="idempotency_key_required",
                message="Preview PR feedback remediation requires Idempotency-Key.",
            )
        try:
            remediation_store = require_preview_pr_feedback_remediation_write_store(record_store)
            target = bind_preview_pr_feedback_target(
                record_store=remediation_store,
                request=remediation_request,
            )
            token = resolve_remediation_token(
                control_plane_root=resolved_control_plane_root,
                context=remediation_request.context,
            )
            observation = observe_managed_preview_pr_feedback(
                target=target,
                token=token,
            )
        except FileNotFoundError as error:
            raise _launchplane_http_error(
                status_code=404,
                trace_id=trace_id,
                code="product_profile_not_found",
                message=str(error),
            ) from error
        except (TypeError, ValueError, click.ClickException) as error:
            raise _launchplane_http_error(
                status_code=409,
                trace_id=trace_id,
                code="preview_pr_feedback_remediation_observation_failed",
                message=str(error),
            ) from error

        actor = launchplane_identity_actor(identity)
        requested_at = utc_now_timestamp()
        if remediation_request.mode == "dry-run":
            remediation_record = build_remediation_record(
                request=remediation_request,
                target=target,
                actor=actor,
                trace_id=trace_id,
                idempotency_key=idempotency_key.strip(),
                requested_at=requested_at,
                observation=observation,
            )
            remediation_store.write_preview_pr_feedback_remediation_record(remediation_record)
            return accepted_evidence_response(
                trace_id=trace_id,
                records={"preview_pr_feedback_remediation_id": remediation_record.remediation_id},
                result=remediation_record.model_dump(mode="json"),
            )

        (
            normalized_key,
            payload_fingerprint,
            replayed_response,
        ) = await replay_apply_idempotency(
            request=request,
            record_store=record_store,
            identity=identity,
            route_path=_PREVIEW_PR_FEEDBACK_REMEDIATION_ROUTE,
            idempotency_key=idempotency_key,
            trace_id=trace_id,
            check_replay=True,
        )
        if replayed_response is not None:
            return replayed_response
        dry_run_record = matching_dry_run(
            record_store=remediation_store,
            actor=actor,
            idempotency_key=normalized_key,
            continuity_sha256=remediation_request.continuity_sha256,
        )
        if dry_run_record is None:
            raise _launchplane_http_error(
                status_code=409,
                trace_id=trace_id,
                code="matching_dry_run_required",
                message="Preview PR feedback remediation apply requires a prior matching dry-run.",
            )
        if (dry_run_record.observation.state == "absent" and observation.state != "absent") or (
            dry_run_record.observation.state == "present"
            and observation.state == "present"
            and dry_run_record.observation.digest_sha256 != observation.digest_sha256
        ):
            raise _launchplane_http_error(
                status_code=409,
                trace_id=trace_id,
                code="preview_pr_feedback_remediation_observation_changed",
                message="Managed preview feedback changed after the reviewed dry-run.",
            )
        try:
            outcome, mutation_evidence, feedback_record = apply_remediation(
                request=remediation_request,
                target=target,
                token=token,
                observation=observation,
                requested_at=requested_at,
            )
        except PreviewPrFeedbackRemediationApplyError as error:
            failed_record = build_remediation_record(
                request=remediation_request,
                target=target,
                actor=actor,
                trace_id=trace_id,
                idempotency_key=normalized_key,
                requested_at=requested_at,
                observation=observation,
                outcome="failed",
                mutation_evidence=error.mutation_evidence,
            )
            failed_record.mutation_evidence.error_message = str(error)
            remediation_store.write_preview_pr_feedback_remediation_record(failed_record)
            raise _launchplane_http_error(
                status_code=409,
                trace_id=trace_id,
                code="preview_pr_feedback_remediation_apply_failed",
                message=str(error),
            ) from error
        remediation_record = build_remediation_record(
            request=remediation_request,
            target=target,
            actor=actor,
            trace_id=trace_id,
            idempotency_key=normalized_key,
            requested_at=requested_at,
            observation=observation,
            outcome=outcome,
            mutation_evidence=mutation_evidence,
            companion_feedback_id=feedback_record.feedback_id,
        )
        response = accepted_evidence_response(
            trace_id=trace_id,
            records={
                "preview_pr_feedback_remediation_id": remediation_record.remediation_id,
                "preview_pr_feedback_id": feedback_record.feedback_id,
            },
            result=remediation_record.model_dump(mode="json"),
        )
        idempotency_record = LaunchplaneIdempotencyRecord(
            record_id=build_launchplane_idempotency_record_id(response_trace_id=trace_id),
            scope=idempotency_scope(identity),
            route_path=_PREVIEW_PR_FEEDBACK_REMEDIATION_ROUTE,
            idempotency_key=normalized_key,
            request_fingerprint=payload_fingerprint,
            response_status_code=202,
            response_trace_id=trace_id,
            recorded_at=requested_at,
            response_payload=response.model_dump(mode="json", exclude_none=True),
        )
        remediation_store.write_preview_pr_feedback_remediation_bundle(
            remediation_record=remediation_record,
            feedback_record=feedback_record,
            idempotency_record=idempotency_record,
        )
        return response

    async def apply_preview_pr_feedback(
        request: Request,
        feedback_request: PreviewPrFeedbackEnvelope,
        identity: Annotated[LaunchplaneIdentity, Depends(read_write_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> AcceptedEvidenceResponse:
        trace_id = next_trace_id()
        try:
            effective_context = resolve_preview_pr_feedback_context(
                record_store=record_store,
                product=feedback_request.product,
                context=feedback_request.context,
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
        except ValueError as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message=str(error),
            ) from error
        if not allows_preview_pr_feedback_write(
            authz_policy=resolved_authz_policy_runtime.policy,
            identity=identity,
            product=feedback_request.product,
            context=effective_context,
            status=feedback_request.status,
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message=(
                    "Workflow cannot write preview PR feedback for the requested product/context."
                ),
            )
        if feedback_request.dry_run:
            preview_pr_feedback_dry_run_result: dict[str, object] = {
                "dry_run": True,
                "preview_pr_feedback": "authorized",
                "product": feedback_request.product,
                "context": effective_context,
                "status": feedback_request.status,
                "anchor_pr_number": feedback_request.anchor_pr_number,
            }
            return accepted_evidence_response(
                trace_id=trace_id,
                records={},
                result=preview_pr_feedback_dry_run_result,
            )
        (
            normalized_key,
            payload_fingerprint,
            replayed_response,
        ) = await replay_apply_idempotency(
            request=request,
            record_store=record_store,
            identity=identity,
            route_path=_PREVIEW_PR_FEEDBACK_ROUTE,
            idempotency_key=idempotency_key,
            trace_id=trace_id,
            check_replay=True,
        )
        if replayed_response is not None:
            return replayed_response
        try:
            feedback_store = require_preview_pr_feedback_write_store(record_store)
        except TypeError as error:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message=str(error),
            ) from error
        every_code_record_store = (
            cast(EveryCodeWorkRequestReadStore, record_store)
            if supports_every_code_work_requests(record_store)
            else None
        )
        owner_review_status: OwnerAcceptanceDecisionStatus | None = None
        owner_review_url = ""
        if feedback_request.status == "ready":
            owner_target = ChangeImpactTargetReference(
                repository=feedback_request.repository,
                pull_request_number=feedback_request.anchor_pr_number,
            )
            try:
                projection_outcome = await run_in_threadpool(
                    owner_acceptance_projection_service.reconcile_if_required,
                    store=record_store,
                    target=owner_target,
                    source_event_id=normalized_key,
                )
                owner_review_status = projection_outcome.decision.status
                if projection_outcome.result is not None and human_session_manager is not None:
                    owner_review_url = owner_acceptance_workbench_reference_url(
                        public_origin=human_session_manager.public_origin,
                        repository=feedback_request.repository,
                        pull_request_number=feedback_request.anchor_pr_number,
                    )
            except (OSError, RequestException, RuntimeError, ValueError):
                owner_review_status = "unavailable"
                owner_review_url = ""
                _LOGGER.exception(
                    "Owner acceptance GitHub projection refresh failed during preview feedback."
                )
        try:
            feedback_record = build_preview_pr_feedback_record(
                control_plane_root=resolved_control_plane_root,
                product=feedback_request.product,
                context=effective_context,
                source=feedback_request.source,
                requested_at=utc_now_timestamp(),
                repository=feedback_request.repository,
                anchor_repo=feedback_request.anchor_repo,
                anchor_pr_number=feedback_request.anchor_pr_number,
                anchor_pr_url=feedback_request.anchor_pr_url,
                status=feedback_request.status,
                marker=feedback_request.marker,
                preview_url=feedback_request.preview_url,
                immutable_image_reference=feedback_request.immutable_image_reference,
                refresh_image_reference=feedback_request.refresh_image_reference,
                revision=feedback_request.revision,
                run_url=feedback_request.run_url,
                failure_summary=feedback_request.failure_summary,
                every_code_record_store=every_code_record_store,
                preview_record_store=(
                    cast(PreviewPrFeedbackPreviewReadStore, record_store)
                    if callable(getattr(record_store, "list_preview_records", None))
                    else None
                ),
                owner_review_status=owner_review_status,
                owner_review_url=owner_review_url,
            )
        except click.ClickException as error:
            raise _launchplane_http_error(
                status_code=409,
                trace_id=trace_id,
                code="preview_url_unavailable",
                message=str(error),
            ) from error
        feedback_store.write_preview_pr_feedback_record(feedback_record)
        notification_attempts = deliver_preview_pr_feedback_notifications(
            record_store=record_store,
            feedback=feedback_record,
            attempted_at=feedback_record.requested_at,
            discord_sender=preview_pr_feedback_discord_sender,
        )
        result_records: dict[str, str] = {"preview_pr_feedback_id": feedback_record.feedback_id}
        response_result: dict[str, object] = feedback_record.model_dump(mode="json")
        if notification_attempts:
            response_result["notification_attempt_count"] = len(notification_attempts)
            response_result["notifications"] = [
                attempt.model_dump(mode="json") for attempt in notification_attempts
            ]
        response = accepted_evidence_response(
            trace_id=trace_id,
            records=result_records,
            result=response_result,
        )
        store_apply_idempotency(
            record_store=record_store,
            identity=identity,
            route_path=_PREVIEW_PR_FEEDBACK_ROUTE,
            idempotency_key=normalized_key,
            request_fingerprint_value=payload_fingerprint,
            trace_id=trace_id,
            response=response,
        )
        return response

    async def apply_preview_desired_state(
        request: Request,
        desired_state_request: PreviewDesiredStateEnvelope,
        identity: Annotated[LaunchplaneIdentity, Depends(read_write_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> AcceptedEvidenceResponse:
        trace_id = next_trace_id()
        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action="preview_desired_state.discover",
            product=desired_state_request.product,
            context=desired_state_request.context,
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message=(
                    "Workflow cannot discover preview desired state for the requested "
                    "product/context."
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
            route_path=_PREVIEW_DESIRED_STATE_ROUTE,
            idempotency_key=idempotency_key,
            trace_id=trace_id,
            check_replay=True,
        )
        if replayed_response is not None:
            return replayed_response
        try:
            desired_state_store = require_preview_desired_state_write_store(record_store)
        except TypeError as error:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message=str(error),
            ) from error
        driver_result = discover_github_preview_desired_state(
            control_plane_root=resolved_control_plane_root,
            product=desired_state_request.product,
            context=desired_state_request.context,
            source=desired_state_request.source,
            discovered_at=utc_now_timestamp(),
            repository=desired_state_request.repository,
            label=desired_state_request.label,
            anchor_repo=desired_state_request.anchor_repo,
            preview_slug_prefix=desired_state_request.preview_slug_prefix,
            max_pages=desired_state_request.max_pages,
        )
        desired_state_store.write_preview_desired_state_record(driver_result)
        response = accepted_evidence_response(
            trace_id=trace_id,
            records={"preview_desired_state_id": driver_result.desired_state_id},
            result=driver_result.model_dump(mode="json"),
        )
        if driver_result.status != "fail":
            store_apply_idempotency(
                record_store=record_store,
                identity=identity,
                route_path=_PREVIEW_DESIRED_STATE_ROUTE,
                idempotency_key=normalized_key,
                request_fingerprint_value=payload_fingerprint,
                trace_id=trace_id,
                response=response,
            )
        return response

    def resolve_verireel_route_authorization(
        *,
        record_store: object,
        product: str,
        context: str,
        instance: str = "",
    ) -> tuple[str, str]:
        resolved_context = resolve_verireel_driver_context(
            record_store=record_store,
            product=product,
            context=context,
            instance=instance,
        )
        authorization_product = (
            resolved_context.profile.product if resolved_context.profile is not None else product
        )
        authorization_context = context.strip()
        if not authorization_context and resolved_context.lane is not None:
            authorization_context = resolved_context.lane.context.strip()
        return authorization_product, authorization_context

    def handle_verireel_route_dependency_error(*, trace_id: str, route_path: str) -> JSONResponse:
        return driver_route_dependency_not_found_response(
            trace_id=trace_id,
            route_path=route_path,
        )

    def raise_verireel_product_mismatch_error(
        *, trace_id: str, error: VeriReelProductMismatchError
    ) -> NoReturn:
        raise _launchplane_http_error(
            status_code=403,
            trace_id=trace_id,
            code="product_driver_mismatch",
            message="Product is not configured for the requested driver route.",
        ) from error

    def raise_verireel_invalid_request_error(
        *, trace_id: str, error: ValueError | click.ClickException
    ) -> NoReturn:
        raise _launchplane_http_error(
            status_code=400,
            trace_id=trace_id,
            code="invalid_request",
            message="Request could not be completed.",
        ) from error

    def raise_verireel_unexpected_driver_error(*, trace_id: str, error: Exception) -> NoReturn:
        _LOGGER.exception("Unexpected Launchplane service error", extra={"trace_id": trace_id})
        raise _launchplane_http_error(
            status_code=500,
            trace_id=trace_id,
            code="internal_error",
            message="Unexpected Launchplane service error. Use trace_id to inspect service logs.",
        ) from error

    async def apply_verireel_prod_deploy(
        request: Request,
        deploy_request: VeriReelProdDeployEnvelope,
        identity: Annotated[LaunchplaneIdentity, Depends(read_write_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> AcceptedEvidenceResponse | JSONResponse:
        trace_id = next_trace_id()
        try:
            authorization_product, authorization_context = resolve_verireel_route_authorization(
                record_store=record_store,
                product=deploy_request.product,
                context=deploy_request.deploy.context,
                instance=deploy_request.deploy.instance,
            )
        except VeriReelRouteDependencyError:
            return handle_verireel_route_dependency_error(
                trace_id=trace_id,
                route_path=_VERIREEL_PROD_DEPLOY_ROUTE,
            )
        except VeriReelProductMismatchError as error:
            raise_verireel_product_mismatch_error(trace_id=trace_id, error=error)
        except (ValueError, click.ClickException) as error:
            raise_verireel_invalid_request_error(trace_id=trace_id, error=error)
        if not native_routes.native_driver_route_authorization_allows(
            endpoint=apply_verireel_prod_deploy,
            authorization_allows=resolved_authz_policy_runtime.policy.allows,
            identity=identity,
            product=authorization_product,
            context=authorization_context,
            instances=(deploy_request.deploy.instance,),
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message=(
                    "Workflow cannot execute the VeriReel prod deploy driver"
                    " for the requested product/context."
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
            route_path=_VERIREEL_PROD_DEPLOY_ROUTE,
            idempotency_key=idempotency_key,
            trace_id=trace_id,
            check_replay=bool(idempotency_key.strip()),
        )
        if replayed_response is not None:
            return replayed_response
        try:
            records, result = apply_verireel_prod_deploy_result(
                control_plane_root=resolved_control_plane_root,
                record_store=record_store,
                request=deploy_request,
            )
        except FileNotFoundError as error:
            raise _launchplane_http_error(
                status_code=404,
                trace_id=trace_id,
                code="not_found",
                message=f"No Launchplane route for {_VERIREEL_PROD_DEPLOY_ROUTE}.",
            ) from error
        except (ValueError, click.ClickException) as error:
            raise_verireel_invalid_request_error(trace_id=trace_id, error=error)
        except Exception as error:
            raise_verireel_unexpected_driver_error(trace_id=trace_id, error=error)
        response = accepted_evidence_response(trace_id=trace_id, records=records, result=result)
        if should_store_verireel_prod_result_idempotency(result):
            store_apply_idempotency(
                record_store=record_store,
                identity=identity,
                route_path=_VERIREEL_PROD_DEPLOY_ROUTE,
                idempotency_key=normalized_key,
                request_fingerprint_value=payload_fingerprint,
                trace_id=trace_id,
                response=response,
            )
        return response

    async def apply_verireel_prod_backup_gate(
        request: Request,
        backup_gate_request: VeriReelProdBackupGateEnvelope,
        identity: Annotated[LaunchplaneIdentity, Depends(read_write_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> AcceptedEvidenceResponse | JSONResponse:
        trace_id = next_trace_id()
        try:
            authorization_product, authorization_context = resolve_verireel_route_authorization(
                record_store=record_store,
                product=backup_gate_request.product,
                context=backup_gate_request.backup_gate.context,
                instance=backup_gate_request.backup_gate.instance,
            )
        except VeriReelRouteDependencyError:
            return handle_verireel_route_dependency_error(
                trace_id=trace_id,
                route_path=_VERIREEL_PROD_BACKUP_GATE_ROUTE,
            )
        except VeriReelProductMismatchError as error:
            raise_verireel_product_mismatch_error(trace_id=trace_id, error=error)
        except (ValueError, click.ClickException) as error:
            raise_verireel_invalid_request_error(trace_id=trace_id, error=error)
        if not native_routes.native_driver_route_authorization_allows(
            endpoint=apply_verireel_prod_backup_gate,
            authorization_allows=resolved_authz_policy_runtime.policy.allows,
            identity=identity,
            product=authorization_product,
            context=authorization_context,
            instances=(backup_gate_request.backup_gate.instance,),
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message=(
                    "Workflow cannot execute the VeriReel prod backup gate driver"
                    " for the requested product/context."
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
            route_path=_VERIREEL_PROD_BACKUP_GATE_ROUTE,
            idempotency_key=idempotency_key,
            trace_id=trace_id,
            check_replay=bool(idempotency_key.strip()),
        )
        if replayed_response is not None:
            return replayed_response
        created_at = utc_now_timestamp()
        try:
            operation_authorization = capture_durable_operation_authorization(
                identity=identity,
                action=native_routes.native_driver_route_authz_action(
                    apply_verireel_prod_backup_gate
                ),
                product=authorization_product,
                context=authorization_context,
                instances=(backup_gate_request.backup_gate.instance,),
                policy_record=resolved_authz_policy_runtime.policy_record(updated_at=created_at),
                authorized_at=created_at,
            )
        except DurableOperationAuthorizationCaptureError as error:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="authorization_provenance_unavailable",
                message="Durable operation authorization provenance is unavailable.",
            ) from error
        try:
            records, result = apply_verireel_prod_backup_gate_result(
                record_store=record_store,
                request=backup_gate_request,
                authorization=operation_authorization,
            )
        except FileNotFoundError as error:
            raise _launchplane_http_error(
                status_code=404,
                trace_id=trace_id,
                code="not_found",
                message=f"No Launchplane route for {_VERIREEL_PROD_BACKUP_GATE_ROUTE}.",
            ) from error
        except (ValueError, click.ClickException) as error:
            raise_verireel_invalid_request_error(trace_id=trace_id, error=error)
        except Exception as error:
            raise_verireel_unexpected_driver_error(trace_id=trace_id, error=error)
        response = accepted_evidence_response(trace_id=trace_id, records=records, result=result)
        if should_store_verireel_prod_result_idempotency(result, skip_pending=True):
            store_apply_idempotency(
                record_store=record_store,
                identity=identity,
                route_path=_VERIREEL_PROD_BACKUP_GATE_ROUTE,
                idempotency_key=normalized_key,
                request_fingerprint_value=payload_fingerprint,
                trace_id=trace_id,
                response=response,
            )
        return response

    async def apply_verireel_prod_promotion(
        request: Request,
        promotion_request: VeriReelProdPromotionEnvelope,
        identity: Annotated[LaunchplaneIdentity, Depends(read_write_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> AcceptedEvidenceResponse | JSONResponse:
        trace_id = next_trace_id()
        try:
            authorization_product, authorization_context = resolve_verireel_route_authorization(
                record_store=record_store,
                product=promotion_request.product,
                context=promotion_request.promotion.context,
                instance=promotion_request.promotion.from_instance,
            )
            resolve_verireel_route_authorization(
                record_store=record_store,
                product=promotion_request.product,
                context=promotion_request.promotion.context,
                instance=promotion_request.promotion.to_instance,
            )
        except VeriReelRouteDependencyError:
            return handle_verireel_route_dependency_error(
                trace_id=trace_id,
                route_path=_VERIREEL_PROD_PROMOTION_ROUTE,
            )
        except VeriReelProductMismatchError as error:
            raise_verireel_product_mismatch_error(trace_id=trace_id, error=error)
        except (ValueError, click.ClickException) as error:
            raise_verireel_invalid_request_error(trace_id=trace_id, error=error)
        if not native_routes.native_driver_route_authorization_allows(
            endpoint=apply_verireel_prod_promotion,
            authorization_allows=resolved_authz_policy_runtime.policy.allows,
            identity=identity,
            product=authorization_product,
            context=authorization_context,
            instances=(
                promotion_request.promotion.from_instance,
                promotion_request.promotion.to_instance,
            ),
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message=(
                    "Workflow cannot execute the VeriReel prod promotion driver"
                    " for the requested product/context."
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
            route_path=_VERIREEL_PROD_PROMOTION_ROUTE,
            idempotency_key=idempotency_key,
            trace_id=trace_id,
            check_replay=bool(idempotency_key.strip()),
        )
        if replayed_response is not None:
            return replayed_response
        try:
            records, result = apply_verireel_prod_promotion_result(
                control_plane_root=resolved_control_plane_root,
                record_store=record_store,
                request=promotion_request,
            )
        except FileNotFoundError as error:
            raise _launchplane_http_error(
                status_code=404,
                trace_id=trace_id,
                code="not_found",
                message=f"No Launchplane route for {_VERIREEL_PROD_PROMOTION_ROUTE}.",
            ) from error
        except (ValueError, click.ClickException) as error:
            raise_verireel_invalid_request_error(trace_id=trace_id, error=error)
        except Exception as error:
            raise_verireel_unexpected_driver_error(trace_id=trace_id, error=error)
        response = accepted_evidence_response(trace_id=trace_id, records=records, result=result)
        if should_store_verireel_prod_result_idempotency(result):
            store_apply_idempotency(
                record_store=record_store,
                identity=identity,
                route_path=_VERIREEL_PROD_PROMOTION_ROUTE,
                idempotency_key=normalized_key,
                request_fingerprint_value=payload_fingerprint,
                trace_id=trace_id,
                response=response,
            )
        return response

    async def apply_verireel_prod_rollback(
        request: Request,
        rollback_request: VeriReelProdRollbackEnvelope,
        identity: Annotated[LaunchplaneIdentity, Depends(read_write_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> AcceptedEvidenceResponse | JSONResponse:
        trace_id = next_trace_id()
        try:
            authorization_product, authorization_context = resolve_verireel_route_authorization(
                record_store=record_store,
                product=rollback_request.product,
                context=rollback_request.rollback.context,
                instance=rollback_request.rollback.instance,
            )
        except VeriReelRouteDependencyError:
            return handle_verireel_route_dependency_error(
                trace_id=trace_id,
                route_path=_VERIREEL_PROD_ROLLBACK_ROUTE,
            )
        except VeriReelProductMismatchError as error:
            raise_verireel_product_mismatch_error(trace_id=trace_id, error=error)
        except (ValueError, click.ClickException) as error:
            raise_verireel_invalid_request_error(trace_id=trace_id, error=error)
        if not native_routes.native_driver_route_authorization_allows(
            endpoint=apply_verireel_prod_rollback,
            authorization_allows=resolved_authz_policy_runtime.policy.allows,
            identity=identity,
            product=authorization_product,
            context=authorization_context,
            instances=(rollback_request.rollback.instance,),
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message=(
                    "Workflow cannot execute the VeriReel prod rollback driver"
                    " for the requested product/context."
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
            route_path=_VERIREEL_PROD_ROLLBACK_ROUTE,
            idempotency_key=idempotency_key,
            trace_id=trace_id,
            check_replay=bool(idempotency_key.strip()),
        )
        if replayed_response is not None:
            return replayed_response
        try:
            records, result = apply_verireel_prod_rollback_result(
                control_plane_root=resolved_control_plane_root,
                record_store=record_store,
                request=rollback_request,
            )
        except FileNotFoundError as error:
            raise _launchplane_http_error(
                status_code=404,
                trace_id=trace_id,
                code="not_found",
                message=f"No Launchplane route for {_VERIREEL_PROD_ROLLBACK_ROUTE}.",
            ) from error
        except (ValueError, click.ClickException) as error:
            raise_verireel_invalid_request_error(trace_id=trace_id, error=error)
        except Exception as error:
            raise_verireel_unexpected_driver_error(trace_id=trace_id, error=error)
        response = accepted_evidence_response(trace_id=trace_id, records=records, result=result)
        if should_store_verireel_prod_result_idempotency(result):
            store_apply_idempotency(
                record_store=record_store,
                identity=identity,
                route_path=_VERIREEL_PROD_ROLLBACK_ROUTE,
                idempotency_key=normalized_key,
                request_fingerprint_value=payload_fingerprint,
                trace_id=trace_id,
                response=response,
            )
        return response

    async def apply_verireel_testing_deploy(
        request: Request,
        deploy_request: VeriReelTestingDeployEnvelope,
        identity: Annotated[LaunchplaneIdentity, Depends(read_write_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> AcceptedEvidenceResponse | JSONResponse:
        trace_id = next_trace_id()
        try:
            authorization_product, authorization_context = resolve_verireel_route_authorization(
                record_store=record_store,
                product=deploy_request.product,
                context=deploy_request.deploy.context,
                instance=deploy_request.deploy.instance,
            )
        except VeriReelRouteDependencyError:
            return driver_route_dependency_not_found_response(
                trace_id=trace_id,
                route_path=_VERIREEL_TESTING_DEPLOY_ROUTE,
            )
        except VeriReelProductMismatchError as error:
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="product_driver_mismatch",
                message="Product is not configured for the requested driver route.",
            ) from error
        except (ValueError, click.ClickException) as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request could not be completed.",
            ) from error
        if not native_routes.native_driver_route_authorization_allows(
            endpoint=apply_verireel_testing_deploy,
            authorization_allows=resolved_authz_policy_runtime.policy.allows,
            identity=identity,
            product=authorization_product,
            context=authorization_context,
            instances=(deploy_request.deploy.instance,),
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message=(
                    "Workflow cannot execute the VeriReel testing deploy driver"
                    " for the requested product/context."
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
            route_path=_VERIREEL_TESTING_DEPLOY_ROUTE,
            idempotency_key=idempotency_key,
            trace_id=trace_id,
            check_replay=bool(idempotency_key.strip()),
        )
        if replayed_response is not None:
            return replayed_response
        try:
            records, result = apply_verireel_testing_deploy_result(
                control_plane_root=resolved_control_plane_root,
                record_store=record_store,
                request=deploy_request,
            )
        except FileNotFoundError as error:
            raise _launchplane_http_error(
                status_code=404,
                trace_id=trace_id,
                code="not_found",
                message=f"No Launchplane route for {_VERIREEL_TESTING_DEPLOY_ROUTE}.",
            ) from error
        except (ValueError, click.ClickException) as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request could not be completed.",
            ) from error
        response = accepted_evidence_response(trace_id=trace_id, records=records, result=result)
        if should_store_verireel_result_idempotency(result):
            store_apply_idempotency(
                record_store=record_store,
                identity=identity,
                route_path=_VERIREEL_TESTING_DEPLOY_ROUTE,
                idempotency_key=normalized_key,
                request_fingerprint_value=payload_fingerprint,
                trace_id=trace_id,
                response=response,
            )
        return response

    async def apply_verireel_app_maintenance(
        request: Request,
        maintenance_request: VeriReelAppMaintenanceEnvelope,
        identity: Annotated[LaunchplaneIdentity, Depends(read_write_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> AcceptedEvidenceResponse | JSONResponse:
        trace_id = next_trace_id()
        try:
            authorization_product, authorization_context = resolve_verireel_route_authorization(
                record_store=record_store,
                product=maintenance_request.product,
                context=maintenance_request.maintenance.context,
                instance=maintenance_request.maintenance.instance,
            )
        except VeriReelRouteDependencyError:
            return driver_route_dependency_not_found_response(
                trace_id=trace_id,
                route_path=_VERIREEL_APP_MAINTENANCE_ROUTE,
            )
        except VeriReelProductMismatchError as error:
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="product_driver_mismatch",
                message="Product is not configured for the requested driver route.",
            ) from error
        if not native_routes.native_driver_route_authorization_allows(
            endpoint=apply_verireel_app_maintenance,
            authorization_allows=resolved_authz_policy_runtime.policy.allows,
            identity=identity,
            product=authorization_product,
            context=authorization_context,
            instances=(maintenance_request.maintenance.instance,),
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message=(
                    "Workflow cannot execute the VeriReel app maintenance driver"
                    " for the requested product/context."
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
            route_path=_VERIREEL_APP_MAINTENANCE_ROUTE,
            idempotency_key=idempotency_key,
            trace_id=trace_id,
            check_replay=bool(idempotency_key.strip()),
        )
        if replayed_response is not None:
            return replayed_response
        try:
            records, result = apply_verireel_app_maintenance_result(
                control_plane_root=resolved_control_plane_root,
                request=maintenance_request,
            )
        except FileNotFoundError as error:
            raise _launchplane_http_error(
                status_code=404,
                trace_id=trace_id,
                code="not_found",
                message=f"No Launchplane route for {_VERIREEL_APP_MAINTENANCE_ROUTE}.",
            ) from error
        except (ValueError, click.ClickException) as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request could not be completed.",
            ) from error
        response = accepted_evidence_response(trace_id=trace_id, records=records, result=result)
        if should_store_verireel_result_idempotency(result):
            store_apply_idempotency(
                record_store=record_store,
                identity=identity,
                route_path=_VERIREEL_APP_MAINTENANCE_ROUTE,
                idempotency_key=normalized_key,
                request_fingerprint_value=payload_fingerprint,
                trace_id=trace_id,
                response=response,
            )
        return response

    async def apply_verireel_testing_verification(
        request: Request,
        verification_request: VeriReelTestingVerificationEnvelope,
        identity: Annotated[LaunchplaneIdentity, Depends(read_write_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> AcceptedEvidenceResponse | JSONResponse:
        trace_id = next_trace_id()
        try:
            authorization_product, authorization_context = resolve_verireel_route_authorization(
                record_store=record_store,
                product=verification_request.product,
                context=verification_request.verification.context,
                instance=verification_request.verification.instance,
            )
        except VeriReelRouteDependencyError:
            return driver_route_dependency_not_found_response(
                trace_id=trace_id,
                route_path=_VERIREEL_TESTING_VERIFICATION_ROUTE,
            )
        except VeriReelProductMismatchError as error:
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="product_driver_mismatch",
                message="Product is not configured for the requested driver route.",
            ) from error
        except (ValueError, click.ClickException) as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request could not be completed.",
            ) from error
        if not native_routes.native_driver_route_authorization_allows(
            endpoint=apply_verireel_testing_verification,
            authorization_allows=resolved_authz_policy_runtime.policy.allows,
            identity=identity,
            product=authorization_product,
            context=authorization_context,
            instances=(verification_request.verification.instance,),
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Workflow cannot write VeriReel testing verification for the requested product/context.",
            )
        (
            normalized_key,
            payload_fingerprint,
            replayed_response,
        ) = await replay_apply_idempotency(
            request=request,
            record_store=record_store,
            identity=identity,
            route_path=_VERIREEL_TESTING_VERIFICATION_ROUTE,
            idempotency_key=idempotency_key,
            trace_id=trace_id,
            check_replay=bool(idempotency_key.strip()),
        )
        if replayed_response is not None:
            return replayed_response
        try:
            result = apply_verireel_testing_verification_result(
                record_store=record_store,
                request=verification_request,
            )
        except FileNotFoundError as error:
            raise _launchplane_http_error(
                status_code=404,
                trace_id=trace_id,
                code="not_found",
                message=f"No Launchplane route for {_VERIREEL_TESTING_VERIFICATION_ROUTE}.",
            ) from error
        except (ValueError, click.ClickException) as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request could not be completed.",
            ) from error
        response = accepted_evidence_response(
            trace_id=trace_id,
            records=verireel_testing_verification_response_records(result),
            result=result,
        )
        if should_store_verireel_result_idempotency(result):
            store_apply_idempotency(
                record_store=record_store,
                identity=identity,
                route_path=_VERIREEL_TESTING_VERIFICATION_ROUTE,
                idempotency_key=normalized_key,
                request_fingerprint_value=payload_fingerprint,
                trace_id=trace_id,
                response=response,
            )
        return response

    async def read_verireel_stable_environment(
        environment_request: VeriReelStableEnvironmentEnvelope,
        identity: Annotated[LaunchplaneIdentity, Depends(read_write_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
    ) -> AcceptedEvidenceResponse | JSONResponse:
        trace_id = next_trace_id()
        try:
            authorization_product, authorization_context = resolve_verireel_route_authorization(
                record_store=record_store,
                product=environment_request.product,
                context=environment_request.environment.context,
                instance=environment_request.environment.instance,
            )
        except VeriReelRouteDependencyError:
            return driver_route_dependency_not_found_response(
                trace_id=trace_id,
                route_path=_VERIREEL_STABLE_ENVIRONMENT_ROUTE,
            )
        except VeriReelProductMismatchError as error:
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="product_driver_mismatch",
                message="Product is not configured for the requested driver route.",
            ) from error
        if not native_routes.native_driver_route_authorization_allows(
            endpoint=read_verireel_stable_environment,
            authorization_allows=resolved_authz_policy_runtime.policy.allows,
            identity=identity,
            product=authorization_product,
            context=authorization_context,
            instances=(environment_request.environment.instance,),
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Workflow cannot read the VeriReel stable environment for the requested product/context.",
            )
        try:
            result = read_verireel_stable_environment_result(
                control_plane_root=resolved_control_plane_root,
                request=environment_request,
            )
        except FileNotFoundError as error:
            raise _launchplane_http_error(
                status_code=404,
                trace_id=trace_id,
                code="not_found",
                message=f"No Launchplane route for {_VERIREEL_STABLE_ENVIRONMENT_ROUTE}.",
            ) from error
        except (ValueError, click.ClickException) as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request could not be completed.",
            ) from error
        return accepted_evidence_response(trace_id=trace_id, records={}, result=result)

    async def run_verireel_runtime_verification(
        verification_request: VeriReelRuntimeVerificationEnvelope,
        identity: Annotated[LaunchplaneIdentity, Depends(read_write_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
    ) -> AcceptedEvidenceResponse | JSONResponse:
        trace_id = next_trace_id()
        try:
            authorization_product, authorization_context = resolve_verireel_route_authorization(
                record_store=record_store,
                product=verification_request.product,
                context=verification_request.verification.context,
                instance=verification_request.verification.instance,
            )
        except VeriReelRouteDependencyError:
            return driver_route_dependency_not_found_response(
                trace_id=trace_id,
                route_path=_VERIREEL_RUNTIME_VERIFICATION_ROUTE,
            )
        except VeriReelProductMismatchError as error:
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="product_driver_mismatch",
                message="Product is not configured for the requested driver route.",
            ) from error
        if not native_routes.native_driver_route_authorization_allows(
            endpoint=run_verireel_runtime_verification,
            authorization_allows=resolved_authz_policy_runtime.policy.allows,
            identity=identity,
            product=authorization_product,
            context=authorization_context,
            instances=(verification_request.verification.instance,),
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message=(
                    "Workflow cannot execute the VeriReel runtime verification driver"
                    " for the requested product/context."
                ),
            )
        try:
            result = run_verireel_runtime_verification_result(
                control_plane_root=resolved_control_plane_root,
                request=verification_request,
            )
        except FileNotFoundError as error:
            raise _launchplane_http_error(
                status_code=404,
                trace_id=trace_id,
                code="not_found",
                message=f"No Launchplane route for {_VERIREEL_RUNTIME_VERIFICATION_ROUTE}.",
            ) from error
        except (ValueError, click.ClickException) as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request could not be completed.",
            ) from error
        return accepted_evidence_response(trace_id=trace_id, records={}, result=result)

    async def read_verireel_preview_inventory(
        inventory_request: VeriReelPreviewInventoryEnvelope,
        identity: Annotated[LaunchplaneIdentity, Depends(read_write_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
    ) -> AcceptedEvidenceResponse | JSONResponse:
        trace_id = next_trace_id()
        try:
            authorization_product, authorization_context = resolve_verireel_route_authorization(
                record_store=record_store,
                product=inventory_request.product,
                context="",
            )
        except VeriReelRouteDependencyError:
            return driver_route_dependency_not_found_response(
                trace_id=trace_id,
                route_path=_VERIREEL_PREVIEW_INVENTORY_ROUTE,
            )
        except VeriReelProductMismatchError as error:
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="product_driver_mismatch",
                message="Product is not configured for the requested driver route.",
            ) from error
        authorization_context = inventory_request.inventory.context.strip() or authorization_context
        if not native_routes.native_driver_route_authorization_allows(
            endpoint=read_verireel_preview_inventory,
            authorization_allows=resolved_authz_policy_runtime.policy.allows,
            identity=identity,
            product=authorization_product,
            context=authorization_context,
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Workflow cannot read the VeriReel preview inventory for the requested product/context.",
            )
        try:
            records, result = apply_verireel_preview_inventory_result(
                control_plane_root=resolved_control_plane_root,
                record_store=record_store,
                request=inventory_request,
            )
        except FileNotFoundError as error:
            raise _launchplane_http_error(
                status_code=404,
                trace_id=trace_id,
                code="not_found",
                message=f"No Launchplane route for {_VERIREEL_PREVIEW_INVENTORY_ROUTE}.",
            ) from error
        except (ValueError, click.ClickException) as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request could not be completed.",
            ) from error
        return accepted_evidence_response(trace_id=trace_id, records=records, result=result)

    async def apply_verireel_preview_refresh(
        request: Request,
        refresh_request: VeriReelPreviewRefreshEnvelope,
        identity: Annotated[LaunchplaneIdentity, Depends(read_write_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> AcceptedEvidenceResponse | JSONResponse:
        trace_id = next_trace_id()
        try:
            authorization_product, authorization_context = resolve_verireel_route_authorization(
                record_store=record_store,
                product=refresh_request.product,
                context="",
            )
        except VeriReelRouteDependencyError:
            return driver_route_dependency_not_found_response(
                trace_id=trace_id,
                route_path=_VERIREEL_PREVIEW_REFRESH_ROUTE,
            )
        except VeriReelProductMismatchError as error:
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="product_driver_mismatch",
                message="Product is not configured for the requested driver route.",
            ) from error
        authorization_context = refresh_request.refresh.context.strip() or authorization_context
        if not native_routes.native_driver_route_authorization_allows(
            endpoint=apply_verireel_preview_refresh,
            authorization_allows=resolved_authz_policy_runtime.policy.allows,
            identity=identity,
            product=authorization_product,
            context=authorization_context,
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message=(
                    "Workflow cannot execute the VeriReel preview refresh driver"
                    " for the requested product/context."
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
            route_path=_VERIREEL_PREVIEW_REFRESH_ROUTE,
            idempotency_key=idempotency_key,
            trace_id=trace_id,
            check_replay=bool(idempotency_key.strip()),
        )
        if replayed_response is not None:
            return replayed_response
        try:
            records, result = apply_verireel_preview_refresh_result(
                control_plane_root=resolved_control_plane_root,
                record_store=record_store,
                request=refresh_request,
            )
        except FileNotFoundError as error:
            raise _launchplane_http_error(
                status_code=404,
                trace_id=trace_id,
                code="not_found",
                message=f"No Launchplane route for {_VERIREEL_PREVIEW_REFRESH_ROUTE}.",
            ) from error
        except VeriReelPreviewRefreshTransportError as error:
            error_message = str(error).strip() or "VeriReel preview refresh backend request failed."
            raise _launchplane_http_error(
                status_code=502,
                trace_id=trace_id,
                code="preview_refresh_backend_unavailable",
                message=error_message,
            ) from error
        except (ValueError, click.ClickException) as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request could not be completed.",
            ) from error
        response = accepted_evidence_response(
            trace_id=trace_id,
            records=records,
            result=result,
        )
        if should_store_verireel_result_idempotency(result):
            store_apply_idempotency(
                record_store=record_store,
                identity=identity,
                route_path=_VERIREEL_PREVIEW_REFRESH_ROUTE,
                idempotency_key=normalized_key,
                request_fingerprint_value=payload_fingerprint,
                trace_id=trace_id,
                response=response,
            )
        return response

    async def apply_verireel_preview_destroy(
        request: Request,
        destroy_request: VeriReelPreviewDestroyEnvelope,
        identity: Annotated[LaunchplaneIdentity, Depends(read_write_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> AcceptedEvidenceResponse | JSONResponse:
        trace_id = next_trace_id()
        try:
            authorization_product, authorization_context = resolve_verireel_route_authorization(
                record_store=record_store,
                product=destroy_request.product,
                context="",
            )
        except VeriReelRouteDependencyError:
            return driver_route_dependency_not_found_response(
                trace_id=trace_id,
                route_path=_VERIREEL_PREVIEW_DESTROY_ROUTE,
            )
        except VeriReelProductMismatchError as error:
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="product_driver_mismatch",
                message="Product is not configured for the requested driver route.",
            ) from error
        authorization_context = destroy_request.destroy.context.strip() or authorization_context
        if not native_routes.native_driver_route_authorization_allows(
            endpoint=apply_verireel_preview_destroy,
            authorization_allows=resolved_authz_policy_runtime.policy.allows,
            identity=identity,
            product=authorization_product,
            context=authorization_context,
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message=(
                    "Workflow cannot execute the VeriReel preview destroy driver"
                    " for the requested product/context."
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
            route_path=_VERIREEL_PREVIEW_DESTROY_ROUTE,
            idempotency_key=idempotency_key,
            trace_id=trace_id,
            check_replay=bool(idempotency_key.strip()),
        )
        if replayed_response is not None:
            return replayed_response
        try:
            records, result = apply_verireel_preview_destroy_result(
                control_plane_root=resolved_control_plane_root,
                record_store=record_store,
                request=destroy_request,
            )
        except FileNotFoundError as error:
            raise _launchplane_http_error(
                status_code=404,
                trace_id=trace_id,
                code="not_found",
                message=f"No Launchplane route for {_VERIREEL_PREVIEW_DESTROY_ROUTE}.",
            ) from error
        except (ValueError, click.ClickException) as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request could not be completed.",
            ) from error
        response = accepted_evidence_response(
            trace_id=trace_id,
            records=records,
            result=result,
        )
        if should_store_verireel_result_idempotency(result):
            store_apply_idempotency(
                record_store=record_store,
                identity=identity,
                route_path=_VERIREEL_PREVIEW_DESTROY_ROUTE,
                idempotency_key=normalized_key,
                request_fingerprint_value=payload_fingerprint,
                trace_id=trace_id,
                response=response,
            )
        return response

    async def apply_verireel_preview_verification(
        request: Request,
        verification_request: VeriReelPreviewVerificationEnvelope,
        identity: Annotated[LaunchplaneIdentity, Depends(read_write_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> AcceptedEvidenceResponse | JSONResponse:
        trace_id = next_trace_id()
        try:
            authorization_product, authorization_context = resolve_verireel_route_authorization(
                record_store=record_store,
                product=verification_request.product,
                context="",
            )
        except VeriReelRouteDependencyError:
            return driver_route_dependency_not_found_response(
                trace_id=trace_id,
                route_path=_VERIREEL_PREVIEW_VERIFICATION_ROUTE,
            )
        except VeriReelProductMismatchError as error:
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="product_driver_mismatch",
                message="Product is not configured for the requested driver route.",
            ) from error
        authorization_context = (
            verification_request.verification.context.strip() or authorization_context
        )
        if not native_routes.native_driver_route_authorization_allows(
            endpoint=apply_verireel_preview_verification,
            authorization_allows=resolved_authz_policy_runtime.policy.allows,
            identity=identity,
            product=authorization_product,
            context=authorization_context,
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Workflow cannot write VeriReel preview verification for the requested product/context.",
            )
        (
            normalized_key,
            payload_fingerprint,
            replayed_response,
        ) = await replay_apply_idempotency(
            request=request,
            record_store=record_store,
            identity=identity,
            route_path=_VERIREEL_PREVIEW_VERIFICATION_ROUTE,
            idempotency_key=idempotency_key,
            trace_id=trace_id,
            check_replay=bool(idempotency_key.strip()),
        )
        if replayed_response is not None:
            return replayed_response
        try:
            result = apply_verireel_preview_verification_result(
                control_plane_root=resolved_control_plane_root,
                record_store=record_store,
                request=verification_request,
            )
        except FileNotFoundError as error:
            raise _launchplane_http_error(
                status_code=404,
                trace_id=trace_id,
                code="not_found",
                message=f"No Launchplane route for {_VERIREEL_PREVIEW_VERIFICATION_ROUTE}.",
            ) from error
        except (ValueError, click.ClickException) as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request could not be completed.",
            ) from error
        response = accepted_evidence_response(
            trace_id=trace_id,
            records=verireel_preview_verification_response_records(result),
            result=result,
        )
        if should_store_verireel_result_idempotency(result):
            store_apply_idempotency(
                record_store=record_store,
                identity=identity,
                route_path=_VERIREEL_PREVIEW_VERIFICATION_ROUTE,
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
                "runtime_key_safety_policy_record_id": string_value(
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
        can_setup_target = resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action="dokploy_target.setup",
            product=setup_request.product,
            context=_LAUNCHPLANE_SERVICE_CONTEXT,
        )
        can_repair_domain_authority = setup_request.operation == "repair-domain-authority" and (
            can_setup_target
            or resolved_authz_policy_runtime.policy.allows(
                identity=identity,
                action="dokploy_target.repair_domain_authority",
                product=setup_request.product,
                context=_LAUNCHPLANE_SERVICE_CONTEXT,
            )
        )
        can_plan_target = setup_request.mode == "dry-run" and (
            can_setup_target
            or resolved_authz_policy_runtime.policy.allows(
                identity=identity,
                action="dokploy_target.plan",
                product=setup_request.product,
                context=_LAUNCHPLANE_SERVICE_CONTEXT,
            )
        )
        if setup_request.operation == "repair-domain-authority":
            authorized = (
                can_repair_domain_authority
                if setup_request.mode == "apply"
                else (
                    can_repair_domain_authority
                    or resolved_authz_policy_runtime.policy.allows(
                        identity=identity,
                        action="dokploy_target.repair_domain_authority.plan",
                        product=setup_request.product,
                        context=_LAUNCHPLANE_SERVICE_CONTEXT,
                    )
                )
            )
        else:
            authorized = can_setup_target if setup_request.mode == "apply" else can_plan_target
        if not authorized:
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Workflow cannot plan or apply Launchplane Dokploy target setup.",
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
        payload_fingerprint = build_request_fingerprint(raw_payload)
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
        _AUTH_SESSION_ROUTE,
        read_auth_session,
        methods=["GET"],
        response_model=AuthSessionResponse,
        response_model_exclude_none=True,
        operation_id="read_human_auth_session",
        summary="Read human auth session",
        responses={401: {"model": AuthSessionRequiredResponse}},
    )

    app.add_api_route(
        _AUTH_GITHUB_LOGIN_ROUTE,
        login_github_oauth,
        methods=["GET"],
        status_code=302,
        operation_id="login_github_oauth",
        summary="Start GitHub OAuth login",
        response_class=RedirectResponse,
        response_model=None,
        responses={503: {"model": LaunchplaneErrorResponse}},
    )

    app.add_api_route(
        _AUTH_GITHUB_CALLBACK_ROUTE,
        complete_github_oauth_callback,
        methods=["GET"],
        status_code=302,
        operation_id="complete_github_oauth_callback",
        summary="Complete GitHub OAuth callback",
        response_class=RedirectResponse,
        response_model=None,
        responses={
            400: {"model": LaunchplaneErrorResponse},
            403: {"model": LaunchplaneErrorResponse},
            503: {"model": LaunchplaneErrorResponse},
        },
    )

    app.add_api_route(
        _AUTH_LOGOUT_ROUTE,
        logout_auth_session,
        methods=["POST"],
        response_model=AuthLogoutResponse,
        response_model_exclude_none=True,
        operation_id="logout_human_auth_session",
        summary="Logout human auth session",
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
        "/v1/service/verireel-workers/status",
        read_verireel_prod_backup_gate_operation_worker_status,
        methods=["GET"],
        response_model=VeriReelProdBackupGateOperationWorkerStatusResponse,
        operation_id="read_verireel_prod_backup_gate_operation_worker_status",
        summary="Read VeriReel prod backup gate operation worker status",
        responses={
            400: {"model": LaunchplaneErrorResponse},
            401: {"model": LaunchplaneErrorResponse},
            403: {"model": LaunchplaneErrorResponse},
            503: {"model": LaunchplaneErrorResponse},
        },
    )

    app.add_api_route(
        "/v1/service/verireel-workers/reconcile",
        reconcile_verireel_prod_backup_gate_operation_workers,
        methods=["POST"],
        response_model=VeriReelProdBackupGateOperationWorkerReconcileResponse,
        operation_id="reconcile_verireel_prod_backup_gate_operation_workers",
        summary="Reconcile stale VeriReel prod backup gate operation workers",
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
        _PREVIEW_PR_FEEDBACK_REMEDIATION_ROUTE,
        remediate_preview_pr_feedback,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        operation_id="remediate_preview_pr_feedback",
        summary="Remediate managed preview PR feedback",
        responses={
            400: {"model": LaunchplaneErrorResponse},
            401: {"model": LaunchplaneErrorResponse},
            403: {"model": LaunchplaneErrorResponse},
            409: {"model": LaunchplaneErrorResponse},
            503: {"model": LaunchplaneErrorResponse},
        },
    )

    app.add_api_route(
        _PRODUCT_RETIREMENT_ROUTE,
        execute_product_retirement,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        operation_id="execute_product_retirement",
        summary="Plan or apply audited generic-web product retirement",
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
        _DETACHED_APPLICATION_RETIREMENT_ROUTE,
        execute_detached_application_retirement,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        operation_id="execute_detached_application_retirement",
        summary="Plan or apply audited detached Dokploy application retirement",
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
        _PREVIEW_PR_FEEDBACK_ROUTE,
        apply_preview_pr_feedback,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        operation_id="apply_preview_pr_feedback",
        summary="Apply preview PR feedback",
        responses={
            400: {"model": LaunchplaneErrorResponse},
            401: {"model": LaunchplaneErrorResponse},
            403: {"model": LaunchplaneErrorResponse},
            409: {"model": LaunchplaneErrorResponse},
            503: {"model": LaunchplaneErrorResponse},
        },
    )

    app.add_api_route(
        _PREVIEW_DESIRED_STATE_ROUTE,
        apply_preview_desired_state,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        operation_id="apply_preview_desired_state",
        summary="Discover preview desired state",
        responses={
            400: {"model": LaunchplaneErrorResponse},
            401: {"model": LaunchplaneErrorResponse},
            403: {"model": LaunchplaneErrorResponse},
            409: {"model": LaunchplaneErrorResponse},
            503: {"model": LaunchplaneErrorResponse},
        },
    )

    app.add_api_route(
        _PREVIEW_LIFECYCLE_PLAN_ROUTE,
        apply_preview_lifecycle_plan,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        operation_id="apply_preview_lifecycle_plan",
        summary="Plan preview lifecycle",
        responses={
            400: {"model": LaunchplaneErrorResponse},
            401: {"model": LaunchplaneErrorResponse},
            403: {"model": LaunchplaneErrorResponse},
            409: {"model": LaunchplaneErrorResponse},
            503: {"model": LaunchplaneErrorResponse},
        },
    )

    app.add_api_route(
        _PREVIEW_LIFECYCLE_CLEANUP_ROUTE,
        apply_preview_lifecycle_cleanup,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        operation_id="apply_preview_lifecycle_cleanup",
        summary="Clean preview lifecycle",
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
        _PREVIEW_LIFECYCLE_SWEEP_ROUTE,
        apply_preview_lifecycle_sweep,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        operation_id="apply_preview_lifecycle_sweep",
        summary="Sweep preview lifecycle",
        responses={
            400: {"model": LaunchplaneErrorResponse},
            401: {"model": LaunchplaneErrorResponse},
            403: {"model": LaunchplaneErrorResponse},
            404: {"model": LaunchplaneErrorResponse},
            409: {"model": LaunchplaneErrorResponse},
            503: {"model": LaunchplaneErrorResponse},
        },
    )

    generic_web_write_route_dependencies = GenericWebWriteRouteDependencies(
        read_write_identity=read_write_identity,
        read_browser_mutation_identity=read_browser_mutation_identity,
        get_record_store=get_record_store,
        next_trace_id=next_trace_id,
        authorization_allows=resolved_authz_policy_runtime.allows,
        http_error=_launchplane_http_error,
        error_response_model=LaunchplaneErrorResponse,
        control_plane_root=resolved_control_plane_root,
        driver_dependency_response=driver_route_dependency_not_found_response,
        replay_apply_idempotency=replay_apply_idempotency,
        store_apply_idempotency=store_apply_idempotency,
        build_apply_idempotency_record=build_apply_idempotency_record,
        run_provider_mutation=run_provider_mutation,
        idempotency_request_fingerprint=idempotency_request_fingerprint,
        require_preview_desired_state_write_store=require_preview_desired_state_write_store,
        openapi_model_schema=_openapi_model_schema,
    )
    generic_web_write_route_handlers = build_generic_web_write_route_handlers(
        dependencies=generic_web_write_route_dependencies,
    )
    register_generic_web_write_routes(
        app,
        dependencies=generic_web_write_route_dependencies,
        handlers=generic_web_write_route_handlers,
    )
    inspect_generic_web_deploy_recovery_provider_evidence = (
        build_generic_web_deploy_recovery_provider_evidence_handler(
            dependencies=GenericWebDeployRecoveryDependencies(
                read_write_identity=generic_web_write_route_dependencies.read_write_identity,
                get_record_store=generic_web_write_route_dependencies.get_record_store,
                next_trace_id=generic_web_write_route_dependencies.next_trace_id,
                authorization_allows=generic_web_write_route_dependencies.authorization_allows,
                http_error=generic_web_write_route_dependencies.http_error,
                control_plane_root=generic_web_write_route_dependencies.control_plane_root,
                idempotency_request_fingerprint=(
                    generic_web_write_route_dependencies.idempotency_request_fingerprint
                ),
            )
        )
    )
    app.add_api_route(
        GENERIC_WEB_DEPLOY_RECOVERY_PROVIDER_EVIDENCE_ROUTE,
        inspect_generic_web_deploy_recovery_provider_evidence,
        methods=["POST"],
        response_model=GenericWebDeployRecoveryProviderEvidenceResponse,
        response_model_exclude_none=True,
        operation_id="inspect_generic_web_deploy_recovery_provider_evidence",
        summary="Inspect exact provider evidence for a generic web deploy reservation",
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
        _VERIREEL_TESTING_DEPLOY_ROUTE,
        apply_verireel_testing_deploy,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": _openapi_model_schema(VeriReelTestingDeployEnvelope)
                    }
                },
            }
        },
        operation_id="apply_verireel_testing_deploy",
        summary="Execute VeriReel testing deploy",
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
        _VERIREEL_PROD_DEPLOY_ROUTE,
        apply_verireel_prod_deploy,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": _openapi_model_schema(VeriReelProdDeployEnvelope)
                    }
                },
            }
        },
        operation_id="apply_verireel_prod_deploy",
        summary="Execute VeriReel prod deploy",
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
        _VERIREEL_PROD_BACKUP_GATE_ROUTE,
        apply_verireel_prod_backup_gate,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": _openapi_model_schema(VeriReelProdBackupGateEnvelope)
                    }
                },
            }
        },
        operation_id="apply_verireel_prod_backup_gate",
        summary="Execute VeriReel prod backup gate",
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
        _VERIREEL_PROD_PROMOTION_ROUTE,
        apply_verireel_prod_promotion,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": _openapi_model_schema(VeriReelProdPromotionEnvelope)
                    }
                },
            }
        },
        operation_id="apply_verireel_prod_promotion",
        summary="Execute VeriReel prod promotion",
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
        _VERIREEL_PROD_ROLLBACK_ROUTE,
        apply_verireel_prod_rollback,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": _openapi_model_schema(VeriReelProdRollbackEnvelope)
                    }
                },
            }
        },
        operation_id="apply_verireel_prod_rollback",
        summary="Execute VeriReel prod rollback",
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
        _VERIREEL_TESTING_VERIFICATION_ROUTE,
        apply_verireel_testing_verification,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": _openapi_model_schema(VeriReelTestingVerificationEnvelope)
                    }
                },
            }
        },
        operation_id="apply_verireel_testing_verification",
        summary="Write VeriReel testing verification",
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
        _VERIREEL_STABLE_ENVIRONMENT_ROUTE,
        read_verireel_stable_environment,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": _openapi_model_schema(VeriReelStableEnvironmentEnvelope)
                    }
                },
            }
        },
        operation_id="read_verireel_stable_environment",
        summary="Read VeriReel stable environment",
        responses={
            400: {"model": LaunchplaneErrorResponse},
            401: {"model": LaunchplaneErrorResponse},
            403: {"model": LaunchplaneErrorResponse},
            404: {"model": LaunchplaneErrorResponse},
            503: {"model": LaunchplaneErrorResponse},
        },
    )

    app.add_api_route(
        _VERIREEL_RUNTIME_VERIFICATION_ROUTE,
        run_verireel_runtime_verification,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": _openapi_model_schema(VeriReelRuntimeVerificationEnvelope)
                    }
                },
            }
        },
        operation_id="run_verireel_runtime_verification",
        summary="Run VeriReel runtime verification",
        responses={
            400: {"model": LaunchplaneErrorResponse},
            401: {"model": LaunchplaneErrorResponse},
            403: {"model": LaunchplaneErrorResponse},
            404: {"model": LaunchplaneErrorResponse},
            503: {"model": LaunchplaneErrorResponse},
        },
    )

    app.add_api_route(
        _VERIREEL_PREVIEW_INVENTORY_ROUTE,
        read_verireel_preview_inventory,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": _openapi_model_schema(VeriReelPreviewInventoryEnvelope)
                    }
                },
            }
        },
        operation_id="read_verireel_preview_inventory",
        summary="Read VeriReel preview inventory",
        responses={
            400: {"model": LaunchplaneErrorResponse},
            401: {"model": LaunchplaneErrorResponse},
            403: {"model": LaunchplaneErrorResponse},
            404: {"model": LaunchplaneErrorResponse},
            503: {"model": LaunchplaneErrorResponse},
        },
    )

    app.add_api_route(
        _VERIREEL_PREVIEW_REFRESH_ROUTE,
        apply_verireel_preview_refresh,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": _openapi_model_schema(VeriReelPreviewRefreshEnvelope)
                    }
                },
            }
        },
        operation_id="apply_verireel_preview_refresh",
        summary="Refresh VeriReel preview",
        responses={
            400: {"model": LaunchplaneErrorResponse},
            401: {"model": LaunchplaneErrorResponse},
            403: {"model": LaunchplaneErrorResponse},
            404: {"model": LaunchplaneErrorResponse},
            409: {"model": LaunchplaneErrorResponse},
            502: {"model": LaunchplaneErrorResponse},
            503: {"model": LaunchplaneErrorResponse},
        },
    )

    app.add_api_route(
        _VERIREEL_PREVIEW_DESTROY_ROUTE,
        apply_verireel_preview_destroy,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": _openapi_model_schema(VeriReelPreviewDestroyEnvelope)
                    }
                },
            }
        },
        operation_id="apply_verireel_preview_destroy",
        summary="Destroy VeriReel preview",
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
        _VERIREEL_APP_MAINTENANCE_ROUTE,
        apply_verireel_app_maintenance,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": _openapi_model_schema(VeriReelAppMaintenanceEnvelope)
                    }
                },
            }
        },
        operation_id="apply_verireel_app_maintenance",
        summary="Execute VeriReel app maintenance",
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
        _VERIREEL_PREVIEW_VERIFICATION_ROUTE,
        apply_verireel_preview_verification,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": _openapi_model_schema(VeriReelPreviewVerificationEnvelope)
                    }
                },
            }
        },
        operation_id="apply_verireel_preview_verification",
        summary="Write VeriReel preview verification",
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
        _ODOO_STABLE_BOOTSTRAP_ROUTE,
        write_odoo_stable_bootstrap,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        openapi_extra={
            "parameters": [
                {
                    "name": "Idempotency-Key",
                    "in": "header",
                    "required": True,
                    "schema": {"type": "string", "minLength": 1},
                    "description": "Required operation idempotency key.",
                }
            ],
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": _openapi_model_schema(OdooStableBootstrapEnvelope)
                    }
                },
            },
        },
        operation_id="write_odoo_stable_bootstrap",
        summary="Enqueue Odoo stable bootstrap operation",
        responses={
            400: {"model": LaunchplaneErrorResponse},
            401: {"model": LaunchplaneErrorResponse},
            403: {"model": LaunchplaneErrorResponse},
            409: {
                "content": {
                    "application/json": {
                        "schema": {
                            "anyOf": [
                                _openapi_model_schema(
                                    LaunchplaneErrorResponse,
                                    ref_template="#/components/schemas/{model}",
                                ),
                                _openapi_model_schema(
                                    OdooStableBootstrapOperationActiveResponse,
                                    ref_template="#/components/schemas/{model}",
                                ),
                            ]
                        }
                    }
                }
            },
            503: {"model": LaunchplaneErrorResponse},
        },
    )

    app.add_api_route(
        _ODOO_PROD_BACKUP_RESTORE_PLAN_ROUTE,
        write_odoo_prod_backup_restore_plan,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": _openapi_model_schema(OdooProdBackupRestorePlanEnvelope)
                    }
                },
            },
        },
        operation_id="write_odoo_prod_backup_restore_plan",
        summary="Build verified Odoo production backup restore plan",
        responses={
            400: {"model": LaunchplaneErrorResponse},
            401: {"model": LaunchplaneErrorResponse},
            403: {"model": LaunchplaneErrorResponse},
            503: {"model": LaunchplaneErrorResponse},
        },
    )

    app.add_api_route(
        _ODOO_PROD_BACKUP_RESTORE_APPLY_ROUTE,
        write_odoo_prod_backup_restore_apply,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": _openapi_model_schema(OdooProdBackupRestoreApplyEnvelope)
                    }
                },
            },
            "parameters": [
                {
                    "name": "Idempotency-Key",
                    "in": "header",
                    "required": True,
                    "schema": {"type": "string", "minLength": 1},
                    "description": "Required destructive restore operation idempotency key.",
                }
            ],
        },
        operation_id="write_odoo_prod_backup_restore_apply",
        summary="Enqueue verified Odoo production backup restore",
        responses={
            400: {"model": LaunchplaneErrorResponse},
            401: {"model": LaunchplaneErrorResponse},
            403: {"model": LaunchplaneErrorResponse},
            409: {"model": LaunchplaneErrorResponse},
            503: {"model": LaunchplaneErrorResponse},
        },
    )

    app.add_api_route(
        _ODOO_PROD_RETAINED_VOLUME_BACKUP_IMPORT_PLAN_ROUTE,
        write_odoo_prod_retained_volume_backup_import_plan,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": _openapi_model_schema(
                            OdooProdRetainedVolumeBackupImportPlanEnvelope
                        )
                    }
                },
            },
            "parameters": [
                {
                    "name": "Idempotency-Key",
                    "in": "header",
                    "required": True,
                    "schema": {"type": "string", "minLength": 1},
                    "description": "Required durable retained-volume inspection idempotency key.",
                }
            ],
        },
        operation_id="write_odoo_prod_retained_volume_backup_import_plan",
        summary="Enqueue Odoo retained-volume backup import plan",
        responses={
            400: {"model": LaunchplaneErrorResponse},
            401: {"model": LaunchplaneErrorResponse},
            403: {"model": LaunchplaneErrorResponse},
            409: {"model": LaunchplaneErrorResponse},
            503: {"model": LaunchplaneErrorResponse},
        },
    )

    app.add_api_route(
        _ODOO_PROD_RETAINED_VOLUME_BACKUP_IMPORT_APPLY_ROUTE,
        write_odoo_prod_retained_volume_backup_import_apply,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": _openapi_model_schema(
                            OdooProdRetainedVolumeBackupImportApplyEnvelope
                        )
                    }
                },
            },
            "parameters": [
                {
                    "name": "Idempotency-Key",
                    "in": "header",
                    "required": True,
                    "schema": {"type": "string", "minLength": 1},
                    "description": "Required retained-volume backup import apply idempotency key.",
                }
            ],
        },
        operation_id="write_odoo_prod_retained_volume_backup_import_apply",
        summary="Enqueue Odoo retained-volume backup import apply",
        responses={
            400: {"model": LaunchplaneErrorResponse},
            401: {"model": LaunchplaneErrorResponse},
            403: {"model": LaunchplaneErrorResponse},
            409: {"model": LaunchplaneErrorResponse},
            503: {"model": LaunchplaneErrorResponse},
        },
    )

    app.add_api_route(
        _ODOO_TARGET_REPLACEMENT_PLAN_ROUTE,
        write_odoo_target_replacement_plan,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": _openapi_model_schema(OdooTargetReplacementPlanEnvelope)
                    }
                },
            },
        },
        operation_id="write_odoo_target_replacement_plan",
        summary="Build Odoo target replacement plan",
        responses={
            400: {"model": LaunchplaneErrorResponse},
            401: {"model": LaunchplaneErrorResponse},
            403: {"model": LaunchplaneErrorResponse},
            503: {"model": LaunchplaneErrorResponse},
        },
    )

    app.add_api_route(
        _ODOO_TARGET_REPLACEMENT_APPLY_ROUTE,
        write_odoo_target_replacement_apply,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": _openapi_model_schema(OdooTargetReplacementApplyEnvelope)
                    }
                },
            },
            "parameters": [
                {
                    "name": "Idempotency-Key",
                    "in": "header",
                    "required": True,
                    "schema": {"type": "string", "minLength": 1},
                    "description": (
                        "Required operation idempotency key. Replays the same operation"
                        " for the same caller and payload; different payload reuse returns 409."
                    ),
                }
            ],
        },
        operation_id="write_odoo_target_replacement_apply",
        summary="Enqueue Odoo target replacement apply",
        responses={
            400: {"model": LaunchplaneErrorResponse},
            401: {"model": LaunchplaneErrorResponse},
            403: {"model": LaunchplaneErrorResponse},
            409: {"model": LaunchplaneErrorResponse},
            503: {"model": LaunchplaneErrorResponse},
        },
    )

    for registered_route_path, registered_endpoint, registered_operation_id, summary in (
        (
            _ODOO_STABLE_BOOTSTRAP_OPERATION_CANCEL_ROUTE,
            cancel_odoo_stable_bootstrap_operation,
            "cancel_odoo_stable_bootstrap_operation",
            "Cancel pending Odoo stable bootstrap operation",
        ),
        (
            _ODOO_TARGET_REPLACEMENT_OPERATION_CANCEL_ROUTE,
            cancel_odoo_target_replacement_operation,
            "cancel_odoo_target_replacement_operation",
            "Cancel pending Odoo target replacement operation",
        ),
        (
            _ODOO_PROD_BACKUP_RESTORE_OPERATION_CANCEL_ROUTE,
            cancel_odoo_prod_backup_restore_operation,
            "cancel_odoo_prod_backup_restore_operation",
            "Cancel pending Odoo production backup restore operation",
        ),
        (
            _ODOO_PROD_RETAINED_VOLUME_BACKUP_IMPORT_OPERATION_CANCEL_ROUTE,
            cancel_odoo_prod_retained_volume_backup_import_operation,
            "cancel_odoo_prod_retained_volume_backup_import_operation",
            "Cancel pending Odoo retained-volume backup import operation",
        ),
        (
            _VERIREEL_PROD_BACKUP_GATE_OPERATION_CANCEL_ROUTE,
            cancel_verireel_prod_backup_gate_operation,
            "cancel_verireel_prod_backup_gate_operation",
            "Cancel pending VeriReel prod backup gate operation",
        ),
    ):
        app.add_api_route(
            registered_route_path,
            registered_endpoint,
            methods=["POST"],
            response_model=DurableOperationCancellationResponse,
            response_model_exclude_none=True,
            operation_id=registered_operation_id,
            summary=summary,
            responses={
                400: {"model": LaunchplaneErrorResponse},
                401: {"model": LaunchplaneErrorResponse},
                403: {"model": LaunchplaneErrorResponse},
                404: {"model": LaunchplaneErrorResponse},
                409: {"model": LaunchplaneErrorResponse},
                503: {"model": LaunchplaneErrorResponse},
            },
        )

    register_operation_status_read_routes(app, dependencies=read_route_dependencies)
    register_protected_artifact_read_routes(
        app,
        dependencies=product_read_route_dependencies,
    )
    register_driver_descriptor_read_routes(app, dependencies=read_route_dependencies)

    app.add_api_route(
        _LAUNCHPLANE_SELF_DEPLOY_ROUTE,
        write_launchplane_self_deploy,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": _openapi_model_schema(LaunchplaneSelfDeployEnvelope)
                    }
                },
            }
        },
        operation_id="write_launchplane_self_deploy",
        summary="Deploy the Launchplane service image",
        responses={
            400: {"model": LaunchplaneErrorResponse},
            401: {"model": LaunchplaneErrorResponse},
            403: {"model": LaunchplaneErrorResponse},
            409: {"model": LaunchplaneErrorResponse},
        },
    )

    app.add_api_route(
        _ODOO_ARTIFACT_PUBLISH_ROUTE,
        write_odoo_artifact_publish,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": _openapi_model_schema(OdooArtifactPublishEnvelope)
                    }
                },
            }
        },
        operation_id="write_odoo_artifact_publish",
        summary="Write Odoo artifact publish evidence",
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
        _ODOO_ARTIFACT_PUBLISH_INPUTS_ROUTE,
        write_odoo_artifact_publish_inputs,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": _openapi_model_schema(OdooArtifactPublishInputsEnvelope)
                    }
                },
            }
        },
        operation_id="write_odoo_artifact_publish_inputs",
        summary="Resolve Odoo artifact publish inputs",
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
        _ODOO_PREVIEW_APPLY_INPUTS_ROUTE,
        write_odoo_preview_apply_inputs,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": _openapi_model_schema(OdooPreviewApplyInputsEnvelope)
                    }
                },
            }
        },
        operation_id="write_odoo_preview_apply_inputs",
        summary="Resolve Odoo preview apply inputs",
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
        _ODOO_PREVIEW_APPLY_ROUTE,
        write_odoo_preview_apply,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {"schema": _openapi_model_schema(OdooPreviewApplyEnvelope)}
                },
            }
        },
        operation_id="write_odoo_preview_apply",
        summary="Apply Odoo preview provider state",
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
        _ODOO_PROD_BACKUP_GATE_ROUTE,
        write_odoo_prod_backup_gate,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": _openapi_model_schema(OdooProdBackupGateEnvelope)
                    }
                },
            }
        },
        operation_id="write_odoo_prod_backup_gate",
        summary="Execute Odoo prod backup gate",
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
        _ODOO_PROD_BACKUP_VERIFICATION_ROUTE,
        write_odoo_prod_backup_verification,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": _openapi_model_schema(OdooProdBackupVerificationEnvelope)
                    }
                },
            }
        },
        operation_id="write_odoo_prod_backup_verification",
        summary="Verify Odoo prod backup integrity",
        responses={
            400: {"model": LaunchplaneErrorResponse},
            401: {"model": LaunchplaneErrorResponse},
            403: {"model": LaunchplaneErrorResponse},
            404: {"model": LaunchplaneErrorResponse},
            409: {"model": LaunchplaneErrorResponse},
            503: {"model": LaunchplaneErrorResponse},
        },
    )

    register_generic_web_rollback_write_routes(
        app,
        dependencies=generic_web_write_route_dependencies,
        handlers=generic_web_write_route_handlers,
    )

    app.add_api_route(
        _ODOO_PROD_ROLLBACK_ROUTE,
        write_odoo_prod_rollback,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {"schema": _openapi_model_schema(OdooProdRollbackEnvelope)}
                },
            }
        },
        operation_id="write_odoo_prod_rollback",
        summary="Execute Odoo prod rollback",
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
        _ODOO_PROD_PROMOTION_ROUTE,
        write_odoo_prod_promotion,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {"schema": _openapi_model_schema(OdooProdPromotionEnvelope)}
                },
            }
        },
        operation_id="write_odoo_prod_promotion",
        summary="Execute Odoo prod promotion route",
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
        _ODOO_PROD_PROMOTION_INPUTS_ROUTE,
        write_odoo_prod_promotion_inputs,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": _openapi_model_schema(OdooProdPromotionInputsEnvelope)
                    }
                },
            }
        },
        operation_id="write_odoo_prod_promotion_inputs",
        summary="Read Odoo prod promotion inputs",
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
        _ODOO_PROD_PROMOTION_RUN_ROUTE,
        write_odoo_prod_promotion_run,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": _openapi_model_schema(OdooProdPromotionRunEnvelope)
                    }
                },
            }
        },
        operation_id="write_odoo_prod_promotion_run",
        summary="Execute Odoo prod promotion run",
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
        _ODOO_POST_DEPLOY_ROUTE,
        write_odoo_post_deploy,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {"schema": _openapi_model_schema(OdooPostDeployEnvelope)}
                },
            }
        },
        operation_id="write_odoo_post_deploy",
        summary="Execute Odoo post-deploy maintenance",
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
        _ODOO_APP_MAINTENANCE_ROUTE,
        write_odoo_app_maintenance,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": _openapi_model_schema(OdooAppMaintenanceEnvelope)
                    }
                },
            }
        },
        operation_id="write_odoo_app_maintenance",
        summary="Execute Odoo app maintenance",
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
        _ODOO_CONFIG_PARAMETER_OVERRIDE_ROUTE,
        write_odoo_config_parameter_override,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": _openapi_model_schema(OdooConfigParameterOverrideEnvelope)
                    }
                },
            }
        },
        operation_id="write_odoo_config_parameter_override",
        summary="Write Odoo config-parameter override",
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
        _ODOO_WEBSITE_BOOTSTRAP_OVERRIDE_ROUTE,
        write_odoo_website_bootstrap_override,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": _openapi_model_schema(OdooWebsiteBootstrapOverrideEnvelope)
                    }
                },
            }
        },
        operation_id="write_odoo_website_bootstrap_override",
        summary="Write Odoo website-bootstrap override",
        responses={
            400: {"model": LaunchplaneErrorResponse},
            401: {"model": LaunchplaneErrorResponse},
            403: {"model": LaunchplaneErrorResponse},
            404: {"model": LaunchplaneErrorResponse},
            409: {"model": LaunchplaneErrorResponse},
            503: {"model": LaunchplaneErrorResponse},
        },
    )

    register_dokploy_target_inspect_read_routes(
        app,
        dependencies=driver_read_route_dependencies,
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

    register_tracked_target_log_read_routes(
        app,
        dependencies=driver_read_route_dependencies,
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
        _EXTERNAL_ROUTE_BINDING_RECONCILE_ROUTE,
        reconcile_external_route_binding,
        methods=["POST"],
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        status_code=202,
        operation_id="reconcile_external_route_binding",
        summary="Dry-run or apply one externally managed route-binding reconcile",
        responses={
            400: {"model": LaunchplaneErrorResponse},
            401: {"model": LaunchplaneErrorResponse},
            403: {"model": LaunchplaneErrorResponse},
            409: {"model": LaunchplaneErrorResponse},
            503: {"model": LaunchplaneErrorResponse},
        },
    )

    app.add_api_route(
        _ROUTE_BINDING_RECONCILE_ROUTE,
        reconcile_route_binding,
        methods=["POST"],
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        status_code=202,
        operation_id="reconcile_route_binding",
        summary="Dry-run or apply one provider-neutral route-binding reconcile",
        responses={
            400: {"model": LaunchplaneErrorResponse},
            401: {"model": LaunchplaneErrorResponse},
            403: {"model": LaunchplaneErrorResponse},
            409: {"model": LaunchplaneErrorResponse},
            503: {"model": LaunchplaneErrorResponse},
        },
    )

    app.add_api_route(
        _ODOO_TESTING_ROUTE_BINDING_REFRESH_ROUTE,
        run_odoo_testing_route_binding_refresh,
        methods=["POST"],
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        status_code=202,
        operation_id="run_odoo_testing_route_binding_refresh",
        summary="Plan or apply bounded Odoo testing route-binding evidence refresh",
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

    register_topology_read_routes(app, dependencies=read_route_dependencies)
    register_ingress_read_routes(app, dependencies=read_route_dependencies)
    register_runner_host_hygiene_read_routes(app, dependencies=read_route_dependencies)
    register_deployment_promotion_read_routes(app, dependencies=read_route_dependencies)

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
        _EVERY_CODE_GITHUB_WEBHOOK_ROUTE,
        handle_every_code_github_webhook,
        methods=["POST"],
        status_code=202,
        operation_id="handle_every_code_github_webhook",
        summary="Handle Every Code GitHub webhook",
        responses={
            400: {"model": LaunchplaneErrorResponse},
            401: {"model": LaunchplaneErrorResponse},
            404: {"model": LaunchplaneErrorResponse},
            413: {"model": LaunchplaneErrorResponse},
            503: {"model": LaunchplaneErrorResponse},
        },
    )
    app.add_api_route(
        MANAGER_PREVIEW_APPROVAL_WEBHOOK_ROUTE,
        handle_manager_preview_approval_github_webhook,
        methods=["POST"],
        status_code=202,
        operation_id="handle_manager_preview_approval_github_webhook",
        summary="Handle manager preview approval GitHub webhook",
        responses={
            400: {"model": LaunchplaneErrorResponse},
            401: {"model": LaunchplaneErrorResponse},
            404: {"model": LaunchplaneErrorResponse},
            413: {"model": LaunchplaneErrorResponse},
            503: {"model": LaunchplaneErrorResponse},
        },
    )
    app.add_api_route(
        MANAGER_PREVIEW_APPROVAL_RECONCILE_ROUTE,
        reconcile_manager_preview_approval,
        methods=["POST"],
        status_code=200,
        operation_id="reconcile_manager_preview_approval",
        summary="Reconcile manager preview approval projection",
        responses={
            403: {"model": LaunchplaneErrorResponse},
            404: {"model": LaunchplaneErrorResponse},
            413: {"model": LaunchplaneErrorResponse},
            503: {"model": LaunchplaneErrorResponse},
        },
    )

    register_product_owner_read_routes(app, dependencies=read_route_dependencies)
    register_change_impact_read_routes(
        app,
        dependencies=ChangeImpactReadRouteDependencies(
            common=read_route_dependencies,
            read_evaluation_identity=read_bearer_identity,
            repository_evidence_provider=resolved_change_impact_repository_evidence_provider,
        ),
    )
    register_owner_acceptance_routes(
        app,
        dependencies=OwnerAcceptanceRouteDependencies(
            common=read_route_dependencies,
            read_write_identity=read_write_identity,
            read_browser_mutation_identity=read_browser_mutation_identity,
            repository_evidence_provider=resolved_change_impact_repository_evidence_provider,
            github_app_token=lambda repository, repository_id: mint_repository_installation_token(
                identity=resolve_advisory_github_app_identity(
                    control_plane_root=resolved_control_plane_root
                ),
                repository=repository,
                repository_id=repository_id,
                api_request=injected_github_api_request,
            ),
            github_api=github_api_request,
            public_origin=(human_session_manager.public_origin if human_session_manager else None),
            projection_service=owner_acceptance_projection_service,
        ),
    )
    register_privileged_operation_routes(
        app,
        dependencies=privileged_operation_route_dependencies,
    )
    register_governance_projection_routes(
        app,
        dependencies=GovernanceProjectionRouteDependencies(
            common=read_route_dependencies,
            repository_evidence_provider=resolved_change_impact_repository_evidence_provider,
            current_readiness_provider=LiveGovernanceCurrentReadinessProvider(
                github_token=lambda env_var: os.environ.get(env_var, "").strip(),
            ),
            now=utc_now_timestamp,
        ),
    )

    register_preview_readiness_read_routes(
        app,
        dependencies=read_route_dependencies,
        read_identity=read_every_code_worker_read_identity,
    )
    register_preview_record_read_routes(app, dependencies=read_route_dependencies)

    register_inventory_operation_read_routes(app, dependencies=read_route_dependencies)

    register_agent_context_read_routes(
        app,
        dependencies=product_read_route_dependencies,
    )
    register_work_graph_snapshot_read_routes(
        app,
        dependencies=work_graph_read_route_dependencies,
    )

    app.add_api_route(
        "/v1/work-graph/rank",
        rank_work_graph_snapshot,
        methods=["POST"],
        status_code=202,
        response_model=WorkGraphRankResponse,
        operation_id="rank_work_graph_snapshot",
        summary="Rank Launchplane work graph snapshot",
        responses={
            400: {"model": LaunchplaneErrorResponse},
            401: {"model": LaunchplaneErrorResponse},
            403: {"model": LaunchplaneErrorResponse},
        },
    )

    register_work_graph_issue_inbox_read_routes(
        app,
        dependencies=work_graph_read_route_dependencies,
    )
    register_tenant_admission_read_routes(
        app,
        dependencies=TenantAdmissionReadRouteDependencies(
            common=read_route_dependencies,
            control_plane_root=resolved_control_plane_root,
            github_token=resolve_launchplane_github_token,
        ),
    )

    app.add_api_route(
        "/v1/work-graph/github/issues/reconcile",
        reconcile_work_graph_issue_inbox,
        methods=["POST"],
        status_code=202,
        response_model=WorkGraphIssueInboxReconcileResponse,
        operation_id="reconcile_work_graph_issue_inbox",
        summary="Reconcile Launchplane GitHub issue inbox",
        responses={
            400: {"model": LaunchplaneErrorResponse},
            401: {"model": LaunchplaneErrorResponse},
            403: {"model": LaunchplaneErrorResponse},
        },
    )

    register_merge_train_read_routes(app, dependencies=read_route_dependencies)

    app.add_api_route(
        _MERGE_TRAIN_BATCH_LANDING_RUN_ONCE_ROUTE,
        write_merge_train_batch_landing_run_once,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": _openapi_model_schema(MergeTrainBatchLandingRunOnceEnvelope)
                    }
                },
            }
        },
        operation_id="write_merge_train_batch_landing_run_once",
        summary="Run one merge train batch landing worker pass",
        responses={
            400: {"model": LaunchplaneErrorResponse},
            401: {"model": LaunchplaneErrorResponse},
            403: {"model": LaunchplaneErrorResponse},
            409: {"model": LaunchplaneErrorResponse},
            502: {"model": LaunchplaneErrorResponse},
            503: {"model": LaunchplaneErrorResponse},
        },
    )

    app.add_api_route(
        _MERGE_TRAIN_BATCH_CANDIDATE_RUN_ONCE_ROUTE,
        write_merge_train_batch_candidate_run_once,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": _openapi_model_schema(MergeTrainBatchCandidateRunOnceEnvelope)
                    }
                },
            }
        },
        operation_id="write_merge_train_batch_candidate_run_once",
        summary="Run one merge train batch candidate worker pass",
        responses={
            400: {"model": LaunchplaneErrorResponse},
            401: {"model": LaunchplaneErrorResponse},
            403: {"model": LaunchplaneErrorResponse},
            409: {"model": LaunchplaneErrorResponse},
            502: {"model": LaunchplaneErrorResponse},
            503: {"model": LaunchplaneErrorResponse},
        },
    )

    app.add_api_route(
        _MERGE_TRAIN_CONTROLLER_RUN_ONCE_ROUTE,
        write_merge_train_controller_run_once,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": _openapi_model_schema(MergeTrainControllerRunOnceEnvelope)
                    }
                },
            }
        },
        operation_id="write_merge_train_controller_run_once",
        summary="Run one merge train controller pass",
        responses={
            400: {"model": LaunchplaneErrorResponse},
            401: {"model": LaunchplaneErrorResponse},
            403: {"model": LaunchplaneErrorResponse},
            409: {"model": LaunchplaneErrorResponse},
            502: {"model": LaunchplaneErrorResponse},
            503: {"model": LaunchplaneErrorResponse},
        },
    )

    app.add_api_route(
        _MERGE_TRAIN_STACK_COLLAPSE_RUN_ONCE_ROUTE,
        write_merge_train_stack_collapse_run_once,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": _openapi_model_schema(MergeTrainStackCollapseRunOnceEnvelope)
                    }
                },
            }
        },
        operation_id="write_merge_train_stack_collapse_run_once",
        summary="Run one merge train stack collapse worker pass",
        responses={
            400: {"model": LaunchplaneErrorResponse},
            401: {"model": LaunchplaneErrorResponse},
            403: {"model": LaunchplaneErrorResponse},
            409: {"model": LaunchplaneErrorResponse},
            502: {"model": LaunchplaneErrorResponse},
            503: {"model": LaunchplaneErrorResponse},
        },
    )

    app.add_api_route(
        _MERGE_TRAIN_RUN_ONCE_ROUTE,
        write_merge_train_run_once,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {"schema": _openapi_model_schema(MergeTrainRunOnceEnvelope)}
                },
            }
        },
        operation_id="write_merge_train_run_once",
        summary="Run one merge train worker pass",
        responses={
            400: {"model": LaunchplaneErrorResponse},
            401: {"model": LaunchplaneErrorResponse},
            403: {"model": LaunchplaneErrorResponse},
            409: {"model": LaunchplaneErrorResponse},
            502: {"model": LaunchplaneErrorResponse},
            503: {"model": LaunchplaneErrorResponse},
        },
    )

    app.add_api_route(
        _MERGE_TRAIN_PR_FEEDBACK_ROUTE,
        write_merge_train_pr_feedback,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": _openapi_model_schema(MergeTrainPrFeedbackEnvelope)
                    }
                },
            }
        },
        operation_id="write_merge_train_pr_feedback",
        summary="Write merge train PR feedback",
        responses={
            400: {"model": LaunchplaneErrorResponse},
            401: {"model": LaunchplaneErrorResponse},
            403: {"model": LaunchplaneErrorResponse},
            409: {"model": LaunchplaneErrorResponse},
            502: {"model": LaunchplaneErrorResponse},
            503: {"model": LaunchplaneErrorResponse},
        },
    )

    register_every_code_work_request_read_routes(
        app,
        dependencies=read_route_dependencies,
        read_identity=read_every_code_worker_read_identity,
    )

    app.add_api_route(
        _AGENT_WRITE_INTENT_EVALUATE_ROUTE,
        evaluate_agent_write_intent_route,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        operation_id="evaluate_agent_write_intent",
        summary="Evaluate an agent write intent",
        responses={
            400: {"model": LaunchplaneErrorResponse},
            401: {"model": LaunchplaneErrorResponse},
            409: {"model": LaunchplaneErrorResponse},
            503: {"model": LaunchplaneErrorResponse},
        },
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
        "/v1/every-code/work-requests/claim",
        claim_every_code_work_request,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        operation_id="claim_every_code_work_request",
        summary="Claim Every Code work request",
        responses=every_code_worker_status_error_responses,
    )

    app.add_api_route(
        "/v1/every-code/work-requests/status",
        write_every_code_work_request_status,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        operation_id="write_every_code_work_request_status",
        summary="Write Every Code work request status",
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": _openapi_model_schema(EveryCodeWorkRequestStatusEnvelope)
                    }
                },
            }
        },
        responses=every_code_worker_status_error_responses,
    )

    app.add_api_route(
        _EVERY_CODE_WORK_REQUEST_HEARTBEAT_ROUTE,
        write_every_code_work_request_heartbeat,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        operation_id="write_every_code_work_request_heartbeat",
        summary="Heartbeat Every Code work request lease",
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": _openapi_model_schema(EveryCodeWorkRequestHeartbeatEnvelope)
                    }
                },
            }
        },
        responses={
            400: {"model": LaunchplaneErrorResponse},
            401: {"model": LaunchplaneErrorResponse},
            409: {"model": LaunchplaneErrorResponse},
            503: {"model": LaunchplaneErrorResponse},
        },
    )

    app.add_api_route(
        _EVERY_CODE_WORK_REQUEST_RECOVER_STALE_ROUTE,
        recover_stale_every_code_work_requests,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        operation_id="recover_stale_every_code_work_requests",
        summary="Recover stale Every Code work requests",
        responses={
            401: {"model": LaunchplaneErrorResponse},
            503: {"model": LaunchplaneErrorResponse},
        },
    )

    app.add_api_route(
        _EVERY_CODE_WORK_REQUEST_RERUN_ROUTE,
        rerun_every_code_work_request,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        operation_id="rerun_every_code_work_request",
        summary="Rerun Every Code work request",
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": _openapi_model_schema(EveryCodeWorkRequestRerunEnvelope)
                    }
                },
            }
        },
        responses=every_code_worker_status_error_responses,
    )

    register_every_code_feedback_read_routes(
        app,
        dependencies=read_route_dependencies,
        read_identity=read_every_code_worker_read_identity,
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

    register_every_code_preview_gate_read_routes(
        app,
        dependencies=read_route_dependencies,
        read_identity=read_every_code_worker_read_identity,
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

    register_every_code_notification_attempt_read_routes(
        app,
        dependencies=read_route_dependencies,
        read_identity=read_every_code_worker_read_identity,
    )

    register_engineering_review_routes(
        app,
        read_dependencies=read_route_dependencies,
        write_dependencies=engineering_review_write_route_dependencies,
        read_identity=read_every_code_worker_read_identity,
    )
    register_engineering_review_decision_routes(
        app,
        read_dependencies=read_route_dependencies,
        write_dependencies=engineering_review_decision_route_dependencies,
    )

    register_preview_notification_attempt_read_routes(
        app,
        dependencies=read_route_dependencies,
    )

    register_product_environment_read_routes(
        app,
        dependencies=product_read_route_dependencies,
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
        _PRODUCT_EXPECTED_CONFIG_APPLY_ROUTE,
        apply_product_expected_config,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": _openapi_model_schema(ProductExpectedConfigApplyEnvelope)
                    }
                },
            }
        },
        operation_id="apply_product_expected_config",
        summary="Add product expected config metadata",
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
        _PRODUCT_HEALTH_MONITORING_APPLY_ROUTE,
        apply_product_health_monitoring,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": _openapi_model_schema(
                            control_plane_product_health_monitoring.ProductHealthMonitoringApplyRequest
                        )
                    }
                },
            }
        },
        operation_id="apply_product_health_monitoring",
        summary="Plan or apply product health monitoring policy",
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
        _PRODUCT_PRELAUNCH_REBUILD_POLICY_APPLY_ROUTE,
        apply_product_prelaunch_rebuild_policy,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": _openapi_model_schema(
                            control_plane_product_prelaunch_rebuild_policy.ProductPrelaunchRebuildPolicyApplyRequest
                        )
                    }
                },
            }
        },
        operation_id="apply_product_prelaunch_rebuild_policy",
        summary="Plan or apply product prelaunch rebuild policy",
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
        _PRODUCT_PREVIEW_TLS_APPLY_ROUTE,
        apply_product_preview_tls,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": _openapi_model_schema(
                            control_plane_product_preview_tls.ProductPreviewTlsApplyRequest
                        )
                    }
                },
            }
        },
        operation_id="apply_product_preview_tls",
        summary="Plan or apply product preview TLS policy",
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
        _PRODUCT_STABLE_LANE_REPAIR_APPLY_ROUTE,
        apply_product_stable_lane_repair,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": _openapi_model_schema(
                            control_plane_product_stable_lane_repair.ProductStableLaneRepairRequest
                        )
                    }
                },
            }
        },
        operation_id="apply_product_stable_lane_repair",
        summary="Plan or apply a bounded product stable lane repair",
        responses={
            400: {"model": LaunchplaneErrorResponse},
            401: {"model": LaunchplaneErrorResponse},
            403: {"model": LaunchplaneErrorResponse},
            404: {"model": LaunchplaneErrorResponse},
            409: {"model": LaunchplaneErrorResponse},
            503: {"model": LaunchplaneErrorResponse},
        },
    )

    register_product_profile_read_routes(
        app,
        dependencies=product_read_route_dependencies,
    )

    app.add_api_route(
        _PRODUCT_CONFIG_APPLY_ROUTE,
        apply_product_config,
        methods=["POST"],
        status_code=202,
        response_model=ProductConfigApplyResponse,
        response_model_exclude_none=True,
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": _openapi_model_schema(ProductConfigApplyEnvelope)
                    }
                },
            }
        },
        operation_id="apply_product_config",
        summary="Plan or apply product config",
        responses={
            400: {"model": LaunchplaneErrorResponse},
            401: {"model": LaunchplaneErrorResponse},
            403: {"model": LaunchplaneErrorResponse},
            409: {"model": LaunchplaneErrorResponse},
            503: {"model": LaunchplaneErrorResponse},
        },
    )

    app.add_api_route(
        _PRODUCT_ENVIRONMENT_CONFIG_APPLY_ROUTE,
        apply_product_environment_config,
        methods=["POST"],
        status_code=202,
        response_model=ProductConfigApplyResponse,
        response_model_exclude_none=True,
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": _openapi_model_schema(ProductEnvironmentConfigApplyEnvelope)
                    }
                },
            }
        },
        operation_id="apply_product_environment_config",
        summary="Plan or apply product environment config",
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
        _PRODUCT_PROMOTION_DRY_RUN_ROUTE,
        dry_run_product_promotion,
        methods=["POST"],
        status_code=202,
        response_model=ProductPromotionDryRunResponse,
        response_model_exclude_none=True,
        operation_id="dry_run_product_promotion",
        summary="Dry-run product promotion from current runtime evidence",
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
        _PRODUCT_PROMOTION_WORKFLOW_ROUTE,
        dispatch_product_promotion_workflow,
        methods=["POST"],
        status_code=202,
        response_model=ProductPromotionWorkflowDispatchResponse,
        response_model_exclude_none=True,
        operation_id="dispatch_product_promotion_workflow",
        summary="Dispatch the reviewed product promotion workflow",
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
        _SECRET_REENCRYPT_ROUTE,
        reencrypt_managed_secrets,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {"schema": _openapi_model_schema(SecretReencryptionRequest)}
                },
            }
        },
        operation_id="reencrypt_managed_secrets",
        summary="Dry-run or apply managed-secret root re-encryption",
        responses={
            400: {"model": LaunchplaneErrorResponse},
            401: {"model": LaunchplaneErrorResponse},
            403: {"model": LaunchplaneErrorResponse},
            409: {"model": LaunchplaneErrorResponse},
            503: {"model": LaunchplaneErrorResponse},
        },
    )

    app.add_api_route(
        _PRODUCT_ONBOARDING_APPLY_ROUTE,
        apply_product_onboarding,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": _openapi_model_schema(ProductOnboardingApplyEnvelope)
                    }
                },
            }
        },
        operation_id="apply_product_onboarding",
        summary="Apply product onboarding records",
        responses={
            400: {"model": LaunchplaneErrorResponse},
            401: {"model": LaunchplaneErrorResponse},
            403: {"model": LaunchplaneErrorResponse},
            409: {"model": LaunchplaneErrorResponse},
            503: {"model": LaunchplaneErrorResponse},
        },
    )

    app.add_api_route(
        _MERGE_TRAIN_POLICY_IMPORT_ROUTE,
        import_merge_train_policy,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": _openapi_model_schema(MergeTrainPolicyImportEnvelope)
                    }
                },
            }
        },
        operation_id="import_merge_train_policy",
        summary="Import merge train policy records",
        responses={
            400: {"model": LaunchplaneErrorResponse},
            401: {"model": LaunchplaneErrorResponse},
            403: {"model": LaunchplaneErrorResponse},
            409: {"model": LaunchplaneErrorResponse},
            503: {"model": LaunchplaneErrorResponse},
        },
    )

    authz_policy_route_responses: dict[int | str, dict[str, Any]] = {
        400: {"model": LaunchplaneErrorResponse},
        401: {"model": LaunchplaneErrorResponse},
        403: {"model": LaunchplaneErrorResponse},
        409: {"model": LaunchplaneErrorResponse},
        503: {"model": LaunchplaneErrorResponse},
    }
    authz_diagnostic_route_responses: dict[int | str, dict[str, Any]] = {
        400: {"model": LaunchplaneErrorResponse},
        401: {"model": LaunchplaneErrorResponse},
        403: {"model": LaunchplaneErrorResponse},
        503: {"model": LaunchplaneErrorResponse},
    }

    app.add_api_route(
        _AUTHZ_POLICY_ACTIVE_ROUTE,
        read_active_authz_policy,
        methods=["GET"],
        response_model=LaunchplaneActiveAuthzPolicyResponse,
        response_model_exclude_none=True,
        operation_id="read_active_authz_policy",
        summary="Read redacted active authz policy administration state",
        responses={
            401: {"model": LaunchplaneErrorResponse},
            403: {"model": LaunchplaneErrorResponse},
            409: {"model": LaunchplaneErrorResponse},
            503: {"model": LaunchplaneErrorResponse},
        },
    )

    app.add_api_route(
        _AUTHZ_POLICY_HEALTH_ROUTE,
        read_authz_policy_health,
        methods=["GET"],
        response_model=AuthzPolicyHealthResponse,
        response_model_exclude_none=True,
        operation_id="read_authz_policy_health",
        summary="Read bounded active authorization policy health",
        responses={
            **authz_diagnostic_route_responses,
            409: {"model": LaunchplaneErrorResponse},
        },
    )

    app.add_api_route(
        _AUTHZ_ACTIVATION_PREFLIGHT_ROUTE,
        read_authz_activation_preflight_self,
        methods=["GET"],
        response_model=AuthzActivationPreflightSelfResponse,
        response_model_exclude_none=True,
        operation_id="read_authz_activation_preflight_self",
        summary="Read the signed Launchplane activation preflight self-check",
        responses={
            **authz_diagnostic_route_responses,
            409: {"model": LaunchplaneErrorResponse},
        },
    )

    app.add_api_route(
        _AUTHZ_REPOSITORY_SCOPE_READ_ROUTE,
        read_authz_repository_scope,
        methods=["POST"],
        response_model=AuthzRepositoryScopeResponse,
        response_model_exclude_none=True,
        operation_id="read_authz_repository_scope",
        summary="Read bounded authorization repository scope",
        responses={
            **authz_diagnostic_route_responses,
            409: {"model": LaunchplaneErrorResponse},
        },
    )

    app.add_api_route(
        _AUTHZ_POLICY_CANDIDATE_PREVIEW_ROUTE,
        preview_authz_candidate_policy,
        methods=["POST"],
        response_model=AuthzPolicyCandidatePreviewResponse,
        response_model_exclude_none=True,
        operation_id="preview_authz_candidate_policy",
        summary="Preview one exact authorization candidate policy",
        responses={
            **authz_diagnostic_route_responses,
            409: {"model": LaunchplaneErrorResponse},
        },
    )

    app.add_api_route(
        _AUTHZ_EFFECTIVE_ACCESS_EVALUATE_ROUTE,
        evaluate_effective_access,
        methods=["POST"],
        response_model=EffectiveAccessEvaluateResponse,
        response_model_exclude_none=True,
        operation_id="evaluate_effective_access",
        summary="Evaluate one principal and scope against active authorization",
        responses=authz_diagnostic_route_responses,
    )

    app.add_api_route(
        _AUTHZ_DENIAL_EXPLANATION_ROUTE,
        explain_authz_denial,
        methods=["GET"],
        response_model=AuthzDenialExplanationResponse,
        response_model_exclude_none=True,
        operation_id="explain_authz_denial",
        summary="Read one redacted authorization denial explanation",
        responses={
            **authz_diagnostic_route_responses,
            404: {"model": LaunchplaneErrorResponse},
        },
    )

    app.add_api_route(
        _GENERIC_WEB_PREVIEW_AUTHZ_PLAN_ROUTE,
        plan_generic_web_preview_authz,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        operation_id="plan_generic_web_preview_authz",
        summary="Plan a generic-web preview managed authz rule-set change",
        responses=authz_policy_route_responses,
    )

    app.add_api_route(
        _AUTHZ_POLICY_MANAGED_RECONCILE_ROUTE,
        reconcile_managed_authz_policy,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": _openapi_model_schema(
                            control_plane_authz_grant_service.AuthzManagedPolicyReconcileEnvelope
                        )
                    }
                },
            }
        },
        operation_id="reconcile_managed_authz_policy",
        summary="Reconcile managed authz policy rules",
        responses=authz_policy_route_responses,
    )

    app.add_api_route(
        _AUTHZ_DIAGNOSTIC_EVALUATE_ROUTE,
        evaluate_github_actions_authz_diagnostic,
        methods=["POST"],
        response_model=control_plane_authz_diagnostics.AuthzDiagnosticEvaluateResponse,
        response_model_exclude_none=True,
        operation_id="evaluate_github_actions_authz_diagnostic",
        summary="Evaluate the calling GitHub Actions identity against active authorization",
        responses=authz_diagnostic_route_responses,
    )

    app.add_api_route(
        _LIVE_TARGET_RUNTIME_APPLY_ROUTE,
        apply_live_target_runtime,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": _openapi_model_schema(
                            control_plane_live_target_runtime.LiveTargetRuntimeApplyEnvelope
                        )
                    }
                },
            }
        },
        operation_id="apply_live_target_runtime",
        summary="Plan or apply live target runtime",
        responses={
            400: {"model": LaunchplaneErrorResponse},
            401: {"model": LaunchplaneErrorResponse},
            403: {"model": LaunchplaneErrorResponse},
            409: {"model": LaunchplaneErrorResponse},
            503: {"model": LaunchplaneErrorResponse},
        },
    )

    app.add_api_route(
        PROVIDER_TARGET_OPERATIONS_ROUTE,
        run_provider_target_operations,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": _openapi_model_schema(ProviderTargetOperationEnvelope)
                    }
                },
            }
        },
        operation_id="run_provider_target_operations",
        summary="Audit or backfill provider-target records",
        responses={
            400: {"model": LaunchplaneErrorResponse},
            401: {"model": LaunchplaneErrorResponse},
            403: {"model": LaunchplaneErrorResponse},
            409: {"model": LaunchplaneErrorResponse},
            503: {"model": LaunchplaneErrorResponse},
        },
    )

    register_managed_secret_read_routes(app, dependencies=read_route_dependencies)

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

    register_evidence_write_routes(
        app,
        dependencies=evidence_write_route_dependencies,
    )

    tenant_admission_write_route_dependencies = TenantAdmissionWriteRouteDependencies(
        read_write_identity=read_bearer_identity,
        read_browser_mutation_identity=read_browser_mutation_identity,
        get_record_store=get_record_store,
        next_trace_id=next_trace_id,
        authorization_allows=resolved_authz_policy_runtime.allows,
        http_error=_launchplane_http_error,
        error_response_model=LaunchplaneErrorResponse,
        control_plane_root=resolved_control_plane_root,
        github_token=resolve_launchplane_github_token,
        github_api=github_api_request,
    )
    register_tenant_admission_write_routes(
        app,
        dependencies=tenant_admission_write_route_dependencies,
    )
    register_product_owner_write_routes(
        app,
        dependencies=product_owner_write_route_dependencies,
    )
    register_change_impact_write_routes(
        app,
        dependencies=change_impact_write_route_dependencies,
    )

    def read_operator_ui(path: str = "") -> Response:
        trace_id = next_trace_id()
        route_path = "/" if path == "" else f"/ui/{path}"
        ui_static_root = resolved_control_plane_root / "control_plane" / "ui_static"
        index_path = ui_static_root / "index.html"

        def not_found_response() -> JSONResponse:
            return JSONResponse(
                status_code=404,
                content={
                    "status": "rejected",
                    "trace_id": trace_id,
                    "error": {
                        "code": "not_found",
                        "message": f"No Launchplane route for {route_path}.",
                    },
                },
            )

        def ui_file_response(*, file_path: FilePath, cache_control: str) -> Response:
            content_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
            body = file_path.read_bytes()
            return Response(
                content=body,
                headers={
                    "Content-Type": content_type,
                    "Content-Length": str(len(body)),
                    "Cache-Control": cache_control,
                },
            )

        if not index_path.is_file():
            return not_found_response()
        if route_path in {"/", "/ui", "/ui/"}:
            return ui_file_response(file_path=index_path, cache_control="no-store")
        if route_path.startswith("/ui/assets/"):
            relative_asset_path = unquote(route_path.removeprefix("/ui/"))
            if ".." in FilePath(relative_asset_path).parts:
                return not_found_response()
            asset_path = (ui_static_root / relative_asset_path).resolve()
            try:
                asset_path.relative_to(ui_static_root.resolve())
            except ValueError:
                return not_found_response()
            if not asset_path.is_file():
                return not_found_response()
            return ui_file_response(
                file_path=asset_path,
                cache_control="public, max-age=31536000, immutable",
            )
        if route_path.startswith("/ui/"):
            return ui_file_response(file_path=index_path, cache_control="no-store")
        return not_found_response()

    app.add_api_route(
        "/",
        read_operator_ui,
        methods=["GET"],
        include_in_schema=False,
    )
    app.add_api_route(
        "/ui",
        read_operator_ui,
        methods=["GET"],
        include_in_schema=False,
    )
    app.add_api_route(
        "/ui/{path:path}",
        read_operator_ui,
        methods=["GET"],
        include_in_schema=False,
    )

    async def persist_authz_denial_best_effort(
        *,
        request: Request,
        trace_id: str,
        evaluation: AuthzEvaluation | None,
        policy_provenance: AuthzPolicyProvenance | None,
    ) -> None:
        if evaluation is None:
            evaluation = current_authz_evaluation()
        if evaluation is None or evaluation.decision != "denied" or policy_provenance is None:
            return

        def write_denial_record() -> None:
            store = get_record_store()
            if not isinstance(store, PostgresRecordStore):
                return
            if policy_provenance.source != "db" or not policy_provenance.record_id:
                return
            recorded_at = datetime.now(timezone.utc)
            route = request.scope.get("route")
            route_path = str(getattr(route, "path", request.url.path))
            store.write_authz_denial_record(
                build_authz_denial_record(
                    trace_id=trace_id,
                    recorded_at=recorded_at.isoformat(),
                    expires_at=(recorded_at + timedelta(days=30)).isoformat(),
                    route_path=route_path,
                    evaluation=evaluation,
                    policy_record_id=policy_provenance.record_id,
                    policy_revision=policy_provenance.revision,
                    policy_sha256=policy_provenance.policy_sha256,
                )
            )

        try:
            await run_in_threadpool(write_denial_record)
        except (OSError, RuntimeError, SQLAlchemyError, TypeError, ValueError):
            logging.exception("Failed to persist redacted authorization denial evidence.")

    async def launchplane_http_exception_handler(
        request: Request, error: Exception
    ) -> JSONResponse:
        if not isinstance(error, HTTPException):
            raise error
        http_error: HTTPException = error
        trace_id = next_trace_id()
        code = "authentication_required" if http_error.status_code == 401 else "http_error"
        if isinstance(http_error, LaunchplaneHTTPException):
            detail = http_error.structured_detail
            trace_id = string_value(detail.get("trace_id", trace_id))
            code = string_value(detail.get("code", code))
            message = string_value(detail.get("message", "Launchplane request failed."))
            records = detail.get("records")
            authz = detail.get("authz")
            authz_evaluation = http_error.authz_evaluation
            authz_policy_provenance = http_error.authz_policy_provenance
        else:
            message = (
                http_error.detail
                if isinstance(http_error.detail, str)
                else "Launchplane request failed."
            )
            records = None
            authz = None
            authz_evaluation = None
            authz_policy_provenance = None
        if http_error.status_code == 403 and code == "authorization_denied":
            if authz_policy_provenance is None:
                request_provenance = getattr(
                    request.state,
                    "launchplane_authz_policy_provenance",
                    None,
                )
                if isinstance(request_provenance, AuthzPolicyProvenance):
                    authz_policy_provenance = request_provenance
            await persist_authz_denial_best_effort(
                request=request,
                trace_id=trace_id,
                evaluation=authz_evaluation,
                policy_provenance=authz_policy_provenance,
            )
        payload = LaunchplaneErrorResponse(
            trace_id=trace_id,
            error=LaunchplaneErrorDetail(code=code, message=message),
            records=records if isinstance(records, dict) else None,
            authz=authz if isinstance(authz, dict) else None,
        )
        response = JSONResponse(
            status_code=http_error.status_code,
            content=payload.model_dump(mode="json", exclude_none=True),
            headers={
                **(http_error.headers or {}),
                **(
                    {"Cache-Control": "no-store"}
                    if request.url.path == _AUTHZ_ACTIVATION_PREFLIGHT_ROUTE
                    else {}
                ),
            },
        )
        preserve_renewed_session_cookie(request, response)
        return response

    def launchplane_starlette_http_exception_handler(
        request: Request, error: Exception
    ) -> JSONResponse:
        if not isinstance(error, STARLETTE_HTTP_EXCEPTION):
            raise error
        http_error: Any = error
        trace_id = next_trace_id()
        if http_error.status_code == 404:
            status_code = 404
            code = "not_found"
            message = f"No Launchplane route for {request.url.path}."
        elif http_error.status_code == 405:
            status_code = 405
            code = "method_not_allowed"
            message = "Only GET and POST are allowed for Launchplane routes."
        else:
            status_code = http_error.status_code
            code = "http_error"
            message = (
                http_error.detail
                if isinstance(http_error.detail, str)
                else "Launchplane request failed."
            )
        payload = LaunchplaneErrorResponse(
            trace_id=trace_id,
            error=LaunchplaneErrorDetail(code=code, message=message),
        )
        response = JSONResponse(
            status_code=status_code,
            content=payload.model_dump(mode="json", exclude_none=True),
            headers={
                **(http_error.headers or {}),
                **(
                    {"Cache-Control": "no-store"}
                    if request.url.path == _AUTHZ_ACTIVATION_PREFLIGHT_ROUTE
                    else {}
                ),
            },
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
            content=payload.model_dump(mode="json", exclude_none=True),
            headers=(
                {"Cache-Control": "no-store"}
                if request.url.path
                in {_AUTHZ_REPOSITORY_SCOPE_READ_ROUTE, _AUTHZ_ACTIVATION_PREFLIGHT_ROUTE}
                else None
            ),
        )
        preserve_renewed_session_cookie(request, response)
        return response

    register_product_config_status_read_routes(
        app,
        dependencies=product_read_route_dependencies,
    )
    register_product_promotion_status_read_routes(
        app,
        dependencies=product_read_route_dependencies,
    )
    app.add_exception_handler(HTTPException, launchplane_http_exception_handler)
    app.add_exception_handler(
        STARLETTE_HTTP_EXCEPTION,
        launchplane_starlette_http_exception_handler,
    )
    app.add_exception_handler(
        RequestValidationError,
        launchplane_request_validation_exception_handler,
    )

    return app


def merge_train_controller_fence_http_error(
    *,
    trace_id: str,
    error: (
        MergeTrainControllerLeaseHeldError
        | MergeTrainControllerLeaseLostError
        | MergeTrainControllerReconciliationRequiredError
        | MergeTrainControllerAdoptionRejectedError
    ),
) -> HTTPException:
    if isinstance(error, MergeTrainControllerLeaseHeldError):
        code = "merge_train_controller_lease_held"
    elif isinstance(error, MergeTrainControllerLeaseLostError):
        code = "merge_train_controller_lease_lost"
    elif isinstance(error, MergeTrainControllerReconciliationRequiredError):
        code = "merge_train_controller_reconciliation_required"
    else:
        code = "merge_train_controller_foreign_action"
    return _launchplane_http_error(
        status_code=409,
        trace_id=trace_id,
        code=code,
        message=str(error),
    )


def _launchplane_http_error(
    *,
    status_code: int,
    trace_id: str,
    code: str,
    message: str,
    authz: dict[str, object] | None = None,
    authz_policy_provenance: AuthzPolicyProvenance | None = None,
    headers: dict[str, str] | None = None,
) -> LaunchplaneHTTPException:
    detail: dict[str, object] = {"trace_id": trace_id, "code": code, "message": message}
    if authz is not None:
        detail["authz"] = authz
    authz_evaluation = None
    if code == "authorization_denied":
        evaluation = current_authz_evaluation()
        if evaluation is not None and evaluation.decision == "denied":
            authz_evaluation = evaluation
    return LaunchplaneHTTPException(
        status_code=status_code,
        detail=detail,
        headers=headers,
        authz_evaluation=authz_evaluation,
        authz_policy_provenance=authz_policy_provenance,
    )


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


def _record_slug(value: str) -> str:
    compact = "".join(
        character.lower() if character.isalnum() else "-" for character in value.strip()
    )
    normalized = "-".join(part for part in compact.split("-") if part)
    return normalized or "launchplane-record"


def _authentication_required_error(message: str) -> LaunchplaneHTTPException:
    return LaunchplaneHTTPException(
        status_code=401,
        detail={"code": "authentication_required", "message": message},
        headers=_BEARER_CHALLENGE_HEADER,
    )
