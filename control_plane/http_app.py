import asyncio
import hashlib
import json
import logging
import mimetypes
import os
import secrets
from copy import deepcopy
from functools import cache
from urllib.parse import unquote
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path as FilePath
from typing import Annotated, Any, Literal, NoReturn, Protocol, cast
from uuid import uuid4
import click
from fastapi import Depends, FastAPI, Header, HTTPException, Path, Query, Request, Response
from fastapi.datastructures import DefaultPlaceholder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse
from jwt import InvalidTokenError
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send
from control_plane import authz_grant_service as control_plane_authz_grant_service
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
from control_plane import product_config as control_plane_product_config
from control_plane import product_config_service as control_plane_product_config_service
from control_plane import product_context_audit as control_plane_product_context_audit
from control_plane import product_context_cutover as control_plane_product_context_cutover
from control_plane import product_onboarding_service as control_plane_product_onboarding_service
from control_plane import product_preview_tls as control_plane_product_preview_tls
from control_plane import product_read_service as control_plane_product_read_service
from control_plane import route_binding_backfill as control_plane_route_binding_backfill
from control_plane import secrets as control_plane_secrets
from control_plane import service_status as control_plane_service_status
from control_plane import tracked_target_logs as control_plane_tracked_target_logs
from control_plane import live_target_runtime as control_plane_live_target_runtime
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
from control_plane.contracts.every_code_work_request import (
    EveryCodeWorkRequestRecord,
    EveryCodeWorkRequestStatusUpdate,
    _add_seconds_to_timestamp as _add_lease_seconds,
    requeue_every_code_work_request,
)
from control_plane.contracts.idempotency_record import (
    LaunchplaneIdempotencyRecord,
    build_launchplane_idempotency_record_id,
)
from control_plane.contracts.data_provenance import DataProvenance, FreshnessStatus
from control_plane.contracts.ingress_canary_route_record import IngressCanaryRouteRecord
from control_plane.contracts.ingress_route_audit_record import (
    IngressRouteAuditOperation,
    IngressRouteAuditRecord,
    IngressRouteTlsOwner,
    build_ingress_route_audit_record_id,
)
from control_plane.contracts.merge_train_policy import MergeTrainPolicyRecord
from control_plane.contracts.merge_train_policy import MergeTrainSchedulerPolicy
from control_plane.contracts.merge_train_policy import MergeTrainServiceAuthz
from control_plane.contracts.odoo_stable_bootstrap_operation import (
    OdooStableBootstrapOperationRecord,
)
from control_plane.contracts.odoo_stable_target_replacement_operation import (
    OdooStableTargetReplacementOperationRecord,
)
from control_plane.merge_train_admission import (
    MergeTrainControllerStatusReadModel,
    MergeTrainRunHistoryStore,
    build_merge_train_controller_status_read_model,
    evaluate_merge_train_admission_from_store,
)
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
from control_plane.merge_train_controller_run_once import (
    MergeTrainControllerRequestError,
    MergeTrainControllerRunOnceEnvelope,
    execute_merge_train_controller_run_once,
)
from control_plane.merge_train_github import MergeTrainGitHubError, MergeTrainGitHubStaleHeadError
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
from control_plane.generic_web_rollback_http import (
    GENERIC_WEB_ROLLBACK_ACTION,
    GENERIC_WEB_ROLLBACK_PLAN_ACTION,
    GENERIC_WEB_ROLLBACK_PLAN_ROUTE as _GENERIC_WEB_ROLLBACK_PLAN_ROUTE,
    GENERIC_WEB_ROLLBACK_ROUTE as _GENERIC_WEB_ROLLBACK_ROUTE,
    GenericWebRollbackEnvelope,
    GenericWebRollbackPlanEnvelope,
    GenericWebRollbackProductMismatchError,
    GenericWebRollbackRouteDependencyError,
    execute_generic_web_rollback_plan_result,
    execute_generic_web_rollback_result,
    resolve_generic_web_rollback_lane,
    should_store_generic_web_rollback_idempotency,
)
from control_plane.generic_web_deploy_http import (
    GENERIC_WEB_DEPLOY_ACTION,
    GENERIC_WEB_DEPLOY_ROUTE as _GENERIC_WEB_DEPLOY_ROUTE,
    GenericWebDeployEnvelope,
    GenericWebDeployProductMismatchError,
    GenericWebDeployRouteDependencyError,
    execute_generic_web_deploy_result,
    resolve_generic_web_deploy_lane,
    should_store_generic_web_deploy_idempotency,
)
from control_plane.generic_web_promotion_http import (
    GENERIC_WEB_PROD_PROMOTION_ACTION,
    GENERIC_WEB_PROD_PROMOTION_ROUTE as _GENERIC_WEB_PROD_PROMOTION_ROUTE,
    GENERIC_WEB_PROD_PROMOTION_WORKFLOW_ACTION,
    GENERIC_WEB_PROD_PROMOTION_WORKFLOW_ROUTE as _GENERIC_WEB_PROD_PROMOTION_WORKFLOW_ROUTE,
    GenericWebProdPromotionEnvelope,
    GenericWebProdPromotionResponse,
    GenericWebProdPromotionResponseResult,
    GenericWebPromotionProductMismatchError,
    GenericWebPromotionRouteDependencyError,
    GenericWebPromotionWorkflowEnvelope,
    GenericWebPromotionWorkflowResponse,
    GenericWebPromotionWorkflowResponseResult,
    dispatch_generic_web_promotion_workflow_result,
    execute_generic_web_prod_promotion_result,
    resolve_generic_web_promotion_destination_lane,
    resolve_generic_web_promotion_workflow_lane,
    should_store_generic_web_promotion_idempotency,
    validate_generic_web_prod_promotion_lanes,
)
from control_plane.generic_web_verification_http import (
    GENERIC_WEB_PREVIEW_VERIFICATION_ACTION,
    GENERIC_WEB_PREVIEW_VERIFICATION_ROUTE as _GENERIC_WEB_PREVIEW_VERIFICATION_ROUTE,
    GENERIC_WEB_STABLE_VERIFICATION_ACTION,
    GENERIC_WEB_STABLE_VERIFICATION_ROUTE as _GENERIC_WEB_STABLE_VERIFICATION_ROUTE,
    GenericWebPreviewVerificationEnvelope,
    GenericWebStableVerificationEnvelope,
    GenericWebVerificationProductMismatchError,
    GenericWebVerificationRouteDependencyError,
    apply_generic_web_preview_verification_result,
    apply_generic_web_stable_verification_result,
    generic_web_verification_response_records,
    resolve_generic_web_preview_verification_profile,
    resolve_generic_web_stable_verification_lane,
    should_store_generic_web_verification_idempotency,
)
from control_plane.verireel_read_http import (
    VERIREEL_PREVIEW_DESTROY_ACTION,
    VERIREEL_PREVIEW_DESTROY_ROUTE as _VERIREEL_PREVIEW_DESTROY_ROUTE,
    VERIREEL_PREVIEW_INVENTORY_ACTION,
    VERIREEL_PREVIEW_INVENTORY_ROUTE as _VERIREEL_PREVIEW_INVENTORY_ROUTE,
    VERIREEL_PREVIEW_REFRESH_ACTION,
    VERIREEL_PREVIEW_REFRESH_ROUTE as _VERIREEL_PREVIEW_REFRESH_ROUTE,
    VERIREEL_PREVIEW_VERIFICATION_ACTION,
    VERIREEL_PREVIEW_VERIFICATION_ROUTE as _VERIREEL_PREVIEW_VERIFICATION_ROUTE,
    VERIREEL_RUNTIME_VERIFICATION_ACTION,
    VERIREEL_RUNTIME_VERIFICATION_ROUTE as _VERIREEL_RUNTIME_VERIFICATION_ROUTE,
    VERIREEL_STABLE_ENVIRONMENT_ACTION,
    VERIREEL_STABLE_ENVIRONMENT_ROUTE as _VERIREEL_STABLE_ENVIRONMENT_ROUTE,
    VERIREEL_TESTING_VERIFICATION_ACTION,
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
    VERIREEL_APP_MAINTENANCE_ACTION,
    VERIREEL_APP_MAINTENANCE_ROUTE as _VERIREEL_APP_MAINTENANCE_ROUTE,
    VERIREEL_TESTING_DEPLOY_ACTION,
    VERIREEL_TESTING_DEPLOY_ROUTE as _VERIREEL_TESTING_DEPLOY_ROUTE,
    VeriReelAppMaintenanceEnvelope,
    VeriReelTestingDeployEnvelope,
    apply_verireel_app_maintenance_result,
    apply_verireel_testing_deploy_result,
)
from control_plane.verireel_prod_http import (
    VERIREEL_PROD_BACKUP_GATE_ACTION,
    VERIREEL_PROD_BACKUP_GATE_ROUTE as _VERIREEL_PROD_BACKUP_GATE_ROUTE,
    VERIREEL_PROD_DEPLOY_ACTION,
    VERIREEL_PROD_DEPLOY_ROUTE as _VERIREEL_PROD_DEPLOY_ROUTE,
    VERIREEL_PROD_PROMOTION_ACTION,
    VERIREEL_PROD_PROMOTION_ROUTE as _VERIREEL_PROD_PROMOTION_ROUTE,
    VERIREEL_PROD_ROLLBACK_ACTION,
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
from control_plane.generic_web_preview_http import (
    GENERIC_WEB_PREVIEW_DESTROY_ACTION,
    GENERIC_WEB_PREVIEW_DESTROY_ROUTE as _GENERIC_WEB_PREVIEW_DESTROY_ROUTE,
    GENERIC_WEB_PREVIEW_INVENTORY_ACTION,
    GENERIC_WEB_PREVIEW_INVENTORY_ROUTE as _GENERIC_WEB_PREVIEW_INVENTORY_ROUTE,
    GENERIC_WEB_PREVIEW_READINESS_ACTION,
    GENERIC_WEB_PREVIEW_READINESS_ROUTE as _GENERIC_WEB_PREVIEW_READINESS_ROUTE,
    GENERIC_WEB_PREVIEW_REFRESH_ACTION,
    GENERIC_WEB_PREVIEW_REFRESH_ROUTE as _GENERIC_WEB_PREVIEW_REFRESH_ROUTE,
    GenericWebPreviewDestroyEnvelope,
    GenericWebPreviewInventoryEnvelope,
    GenericWebPreviewProductMismatchError,
    GenericWebPreviewReadinessEnvelope,
    GenericWebPreviewRefreshEnvelope,
    GenericWebPreviewRouteDependencyError,
    apply_generic_web_preview_destroy_result,
    apply_generic_web_preview_inventory_result,
    apply_generic_web_preview_readiness_result,
    apply_generic_web_preview_refresh_result,
    resolve_generic_web_preview_profile,
    should_store_generic_web_preview_idempotency,
)
from control_plane.odoo_artifact_publish_inputs_http import (
    ODOO_ARTIFACT_PUBLISH_INPUTS_ACTION,
    ODOO_ARTIFACT_PUBLISH_INPUTS_ROUTE as _ODOO_ARTIFACT_PUBLISH_INPUTS_ROUTE,
    OdooArtifactPublishInputsEnvelope,
    OdooArtifactPublishInputsProductMismatchError,
    OdooArtifactPublishInputsRouteDependencyError,
    build_odoo_artifact_publish_inputs_result,
    resolve_odoo_artifact_publish_inputs_profile,
)
from control_plane.odoo_artifact_publish_http import (
    ODOO_ARTIFACT_PUBLISH_ACTION,
    ODOO_ARTIFACT_PUBLISH_ROUTE as _ODOO_ARTIFACT_PUBLISH_ROUTE,
    OdooArtifactPublishEnvelope,
    OdooArtifactPublishProductMismatchError,
    OdooArtifactPublishRouteDependencyError,
    ingest_odoo_artifact_publish_evidence_result,
    resolve_odoo_artifact_publish_product_route,
    should_store_odoo_artifact_publish_idempotency,
)
from control_plane.odoo_preview_apply_http import (
    ODOO_PREVIEW_APPLY_ACTION,
    ODOO_PREVIEW_APPLY_INPUTS_ACTION,
    ODOO_PREVIEW_APPLY_INPUTS_ROUTE as _ODOO_PREVIEW_APPLY_INPUTS_ROUTE,
    ODOO_PREVIEW_APPLY_ROUTE as _ODOO_PREVIEW_APPLY_ROUTE,
    OdooPreviewApplyConfigError,
    OdooPreviewApplyEnvelope,
    OdooPreviewApplyInputsEnvelope,
    OdooPreviewApplyProductMismatchError,
    OdooPreviewApplyRouteDependencyError,
    build_odoo_preview_apply_inputs_result,
    driver_result_contains_status,
    execute_odoo_preview_apply_result,
    resolve_odoo_preview_apply_profile,
)
from control_plane.odoo_post_deploy_http import (
    ODOO_CONFIG_PARAMETER_OVERRIDE_ACTION,
    ODOO_CONFIG_PARAMETER_OVERRIDE_ROUTE as _ODOO_CONFIG_PARAMETER_OVERRIDE_ROUTE,
    ODOO_POST_DEPLOY_ACTION,
    ODOO_POST_DEPLOY_ROUTE as _ODOO_POST_DEPLOY_ROUTE,
    ODOO_WEBSITE_BOOTSTRAP_OVERRIDE_ACTION,
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
    ODOO_APP_MAINTENANCE_ACTION,
    ODOO_APP_MAINTENANCE_ROUTE as _ODOO_APP_MAINTENANCE_ROUTE,
    OdooAppMaintenanceEnvelope,
    OdooAppMaintenanceProductMismatchError,
    OdooAppMaintenanceRouteDependencyError,
    execute_odoo_app_maintenance_result,
    resolve_odoo_app_maintenance_product_route,
    should_store_odoo_app_maintenance_idempotency,
)
from control_plane.odoo_prod_backup_gate_http import (
    ODOO_PROD_BACKUP_GATE_ACTION,
    ODOO_PROD_BACKUP_GATE_ROUTE as _ODOO_PROD_BACKUP_GATE_ROUTE,
    OdooProdBackupGateEnvelope,
    OdooProdBackupGateProductMismatchError,
    OdooProdBackupGateRouteDependencyError,
    execute_odoo_prod_backup_gate_result,
    resolve_odoo_prod_backup_gate_product_route,
    should_store_odoo_prod_backup_gate_idempotency,
)
from control_plane.odoo_prod_promotion_http import (
    ODOO_PROD_PROMOTION_ACTION,
    ODOO_PROD_PROMOTION_INPUTS_ACTION,
    ODOO_PROD_PROMOTION_INPUTS_ROUTE as _ODOO_PROD_PROMOTION_INPUTS_ROUTE,
    ODOO_PROD_PROMOTION_ROUTE as _ODOO_PROD_PROMOTION_ROUTE,
    ODOO_PROD_PROMOTION_RUN_ACTION,
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
    ODOO_PROD_ROLLBACK_ACTION,
    ODOO_PROD_ROLLBACK_ROUTE as _ODOO_PROD_ROLLBACK_ROUTE,
    OdooProdRollbackEnvelope,
    OdooProdRollbackProductMismatchError,
    OdooProdRollbackRouteDependencyError,
    execute_odoo_prod_rollback_result,
    resolve_odoo_prod_rollback_product_route,
    should_store_odoo_prod_rollback_idempotency,
)
from control_plane.odoo_stable_bootstrap_http import (
    ODOO_STABLE_BOOTSTRAP_ACTION,
    ODOO_STABLE_BOOTSTRAP_ROUTE as _ODOO_STABLE_BOOTSTRAP_ROUTE,
    OdooStableBootstrapEnvelope,
    OdooStableBootstrapIdempotencyKeyReusedError,
    OdooStableBootstrapOperationActiveError,
    OdooStableBootstrapProductMismatchError,
    OdooStableBootstrapRouteDependencyError,
    enqueue_odoo_stable_bootstrap_operation,
    operation_payload as odoo_stable_bootstrap_operation_payload,
    resolve_odoo_stable_bootstrap_product_route,
)
from control_plane.odoo_target_replacement_plan_http import (
    ODOO_TARGET_REPLACEMENT_PLAN_ACTION,
    ODOO_TARGET_REPLACEMENT_PLAN_ROUTE as _ODOO_TARGET_REPLACEMENT_PLAN_ROUTE,
    OdooTargetReplacementPlanEnvelope,
    OdooTargetReplacementPlanProductMismatchError,
    OdooTargetReplacementPlanRouteDependencyError,
    resolve_odoo_target_replacement_plan_lane,
)
from control_plane.odoo_target_replacement_apply_http import (
    ODOO_TARGET_REPLACEMENT_APPLY_ACTION,
    ODOO_TARGET_REPLACEMENT_APPLY_ROUTE as _ODOO_TARGET_REPLACEMENT_APPLY_ROUTE,
    OdooTargetReplacementApplyEnvelope,
    OdooTargetReplacementApplyIdempotencyKeyReusedError,
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
from control_plane.contracts.product_environment_read_model import (
    ActionAllowed,
    ProductActivityReadModel,
    ProductEnvironmentConfigStatus,
    ProductEnvironmentDetail,
    ProductEnvironmentSummary,
    ProductReadModelStore,
    ProductSiteOverview,
)
from control_plane.contracts.product_onboarding_manifest import ProductOnboardingManifest
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductExpectedConfigProfile,
    ProductRuntimeConfigRequirement,
    ProductSecretConfigRequirement,
)
from control_plane.contracts.repo_product_mapping_read_model import RepoProductMapping
from control_plane.contracts.preview_evidence import (
    PreviewDestroyedEvidenceEnvelope,
    PreviewGenerationEvidenceEnvelope,
)
from control_plane.contracts.preview_generation_record import PreviewGenerationRecord
from control_plane.contracts.preview_desired_state_record import PreviewDesiredStateRecord
from control_plane.contracts.preview_inventory_scan_record import PreviewInventoryScanRecord
from control_plane.contracts.preview_lifecycle_plan_record import (
    PreviewLifecycleDesiredPreview,
    PreviewLifecyclePlanRecord,
)
from control_plane.contracts.preview_pr_feedback_notifications import (
    PreviewPrFeedbackNotificationAttemptRecord,
    PreviewPrFeedbackNotificationPolicyRecord,
)
from control_plane.contracts.preview_pr_feedback_record import (
    PreviewPrFeedbackRecord,
    PreviewPrFeedbackStatus,
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
from control_plane.contracts.route_binding_record import (
    EnvironmentRouteBindingReadModel,
    EnvironmentRouteBindingRecord,
    redacted_route_binding_record,
)
from control_plane.contracts.runner_host_hygiene import RunnerHostHygieneApplyAuditRecord
from control_plane.contracts.runner_host_hygiene_evidence import (
    RunnerHostHygieneAuditEvidenceEnvelope,
)
from control_plane.contracts.runner_lane_registration import RunnerLaneRegistrationAuditRecord
from control_plane.contracts.runner_lane_registration_evidence import (
    RunnerLaneRegistrationAuditEvidenceEnvelope,
)
from control_plane.contracts.runtime_key_safety_policy import RuntimeKeySafetyTarget
from control_plane.contracts.secret_reencryption_request import SecretReencryptionRequest
from control_plane.contracts.secret_record import SecretScope
from control_plane.contracts.public_ingress_monitoring import PublicIngressNotificationPolicyRecord
from control_plane.drivers.registry import build_driver_context_view, list_driver_descriptors
from control_plane.drivers.registry import read_driver_descriptor as read_driver_descriptor_record
from control_plane.drivers.generic_web_preview_dispatch import (
    GenericWebPreviewDesiredStateEnvelope,
    _GENERIC_WEB_PREVIEW_DESIRED_STATE_ROUTE,
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
    product_config_live_target_next_actions,
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
from control_plane.launchplane_mutations import (
    LaunchplaneDestroyPreviewStore,
    LaunchplaneMutationStore,
    apply_launchplane_destroy_preview,
    apply_launchplane_generation_evidence,
)
from control_plane.storage.factory import build_shared_record_store
from control_plane.storage.factory import storage_backend_name
from control_plane.storage.product_authority_bundle import ProductAuthorityBundle
from control_plane.storage.postgres import (
    DbOnlyMutationRequest,
    MutationReservationResult,
    MutationReservationUpdateResult,
    OutboxWithIdempotencyRequest,
    PostgresRecordStore,
    RouteBindingMutationResult,
)
from control_plane.workflows.evidence_ingestion import (
    EvidenceIngestionStore,
    PromotionEvidenceValidationError,
    apply_deployment_evidence,
    apply_promotion_evidence,
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
from control_plane.workflows.generic_web_deploy import product_profile_uses_generic_web_base
from control_plane.workflows.generic_web_preview import (
    GenericWebPreviewProfileStore,
    discover_generic_web_preview_desired_state,
)
from control_plane.workflows.launchplane_self_deploy import execute_launchplane_self_deploy
from control_plane.workflows.ship import utc_now_timestamp
from control_plane.work_graph_issue_inbox import (
    GitHubIssueInboxReadModel,
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
    WorkGraphWorkRequestStore,
    build_repo_product_mapping_service_payload,
    build_work_graph_rank_result,
    build_work_graph_snapshot_service_payload,
)

EveryCodeGitHubWebhookHandler = Callable[
    [bytes, str, str, str, object, FilePath, str], tuple[int, dict[str, object]]
]


_LOGGER = logging.getLogger(__name__)


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
_EVERY_CODE_GITHUB_WEBHOOK_ROUTE = "/v1/every-code/github-webhook"
_PRODUCT_CONFIG_APPLY_ROUTE = "/v1/product-config/apply"
_SECRET_REENCRYPT_ROUTE = "/v1/secrets/reencrypt"
_EVIDENCE_INGRESS_MAX_BODY_BYTES = 2 * 1024 * 1024
_GITHUB_WEBHOOK_MAX_BODY_BYTES = 2 * 1024 * 1024
_PRODUCT_CONFIG_MAX_BODY_BYTES = 2 * 1024 * 1024
_SECRET_REENCRYPT_MAX_BODY_BYTES = 64 * 1024
_EVIDENCE_INGRESS_ROUTES = frozenset(
    {
        _DEPLOYMENT_EVIDENCE_ROUTE,
        _BACKUP_GATE_EVIDENCE_ROUTE,
        _PROMOTION_EVIDENCE_ROUTE,
        _PREVIEW_GENERATION_EVIDENCE_ROUTE,
        _PREVIEW_DESTROYED_EVIDENCE_ROUTE,
        _RUNNER_HOST_HYGIENE_AUDIT_EVIDENCE_ROUTE,
        _RUNNER_LANE_REGISTRATION_AUDIT_EVIDENCE_ROUTE,
    }
)
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
    _PRODUCT_CONFIG_APPLY_ROUTE: (
        "Product config",
        _PRODUCT_CONFIG_MAX_BODY_BYTES,
        True,
        True,
    ),
    _SECRET_REENCRYPT_ROUTE: (
        "Managed-secret re-encryption",
        _SECRET_REENCRYPT_MAX_BODY_BYTES,
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
_INGRESS_ROUTE_APPLY_ROUTE = "/v1/drivers/ingress/route-apply"
_INGRESS_CANARY_ROUTE_RECORD_APPLY_ROUTE = "/v1/ingress/canary-routes/records/apply"
_INGRESS_CANARY_ROUTE_APPLY_ROUTE = "/v1/ingress/canary-routes/apply"
_ROUTE_BINDING_BACKFILL_APPLY_ROUTE = "/v1/route-bindings/backfill/apply"
_PRODUCT_PROFILES_ROUTE = "/v1/product-profiles"
_PRODUCT_EXPECTED_CONFIG_APPLY_ROUTE = "/v1/product-profiles/expected-config/apply"
_PRODUCT_PREVIEW_TLS_APPLY_ROUTE = "/v1/product-profiles/preview-tls/apply"
_PRODUCT_CONTEXT_CUTOVER_APPLY_ROUTE = "/v1/product-profiles/context-cutover/apply"
_PRODUCT_LEGACY_CONTEXT_CLEANUP_APPLY_ROUTE = "/v1/product-profiles/legacy-context-cleanup/apply"
_PRODUCT_ONBOARDING_APPLY_ROUTE = "/v1/product-onboarding/apply"
_MERGE_TRAIN_POLICY_IMPORT_ROUTE = "/v1/merge-train/policies/import"
_AUTHZ_POLICY_GITHUB_ACTIONS_GRANTS_ROUTE = "/v1/authz-policies/github-actions/grants"
_AUTHZ_POLICY_GITHUB_ACTIONS_REMOVALS_ROUTE = "/v1/authz-policies/github-actions/removals"
_AUTHZ_POLICY_GITHUB_HUMANS_GRANTS_ROUTE = "/v1/authz-policies/github-humans/grants"
_AUTHZ_POLICY_TERMINAL_AGENTS_GRANTS_ROUTE = "/v1/authz-policies/terminal-agents/grants"
_AUTHZ_POLICY_LOCAL_OPERATORS_GRANTS_ROUTE = "/v1/authz-policies/local-operators/grants"
_AUTHZ_POLICY_LOCAL_ADMINS_GRANTS_ROUTE = "/v1/authz-policies/local-admins/grants"
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

AuthzPolicyRouteEnvelope = (
    control_plane_authz_grant_service.AuthzPolicyGitHubActionsGrantEnvelope
    | control_plane_authz_grant_service.AuthzPolicyGitHubActionsRemovalEnvelope
    | control_plane_authz_grant_service.AuthzPolicyGitHubHumanGrantEnvelope
    | control_plane_authz_grant_service.AuthzPolicyTerminalAgentGrantEnvelope
    | control_plane_authz_grant_service.AuthzPolicyLocalOperatorGrantEnvelope
    | control_plane_authz_grant_service.AuthzPolicyLocalAdminGrantEnvelope
)


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


class PreviewDesiredStateWriteStore(Protocol):
    def write_preview_desired_state_record(
        self,
        record: PreviewDesiredStateRecord,
    ) -> object: ...


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


class OdooStableBootstrapOperationActiveResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["rejected"] = "rejected"
    trace_id: str
    error: LaunchplaneErrorDetail
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


class BoundedRequestBodyMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http" or str(scope.get("method", "")).upper() != "POST":
            await self.app(scope, receive, send)
            return
        contract = _BOUNDED_REQUEST_BODY_CONTRACTS.get(str(scope.get("path", "")))
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
                message=(f"{request_label} Content-Length must be an unsigned decimal integer."),
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
                message = buffered_messages[next_message_index]
                next_message_index += 1
                return message
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
    controller_status: MergeTrainControllerStatusReadModel


class MergeTrainPolicySummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    record_id: str
    updated_at: str
    policy_sha256: str


class MergeTrainPolicyTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repository: str
    base_branch: str
    policy_key: str
    scheduler: MergeTrainSchedulerPolicy
    service_authz: MergeTrainServiceAuthz


class MergeTrainPolicyTargetsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    policy: MergeTrainPolicySummary
    targets: tuple[MergeTrainPolicyTarget, ...]


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

    source: Literal["runtime", "deployment"]
    line_count: int
    since: str
    search: str


class TrackedTargetLogLinesResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    line_count: int
    lines: tuple[str, ...]
    redacted: bool


class TrackedTargetLogDeploymentResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    deployment_id: str
    status: str
    error_message: str
    created_at: str
    started_at: str
    finished_at: str
    log_path_present: bool


class TrackedTargetLogsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    context: str
    instance: str
    target: TrackedTargetLogTargetResponse
    request: TrackedTargetLogRequestResponse
    logs: TrackedTargetLogLinesResponse
    deployment: TrackedTargetLogDeploymentResponse | None = None


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


class RouteBindingRecordResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    record: EnvironmentRouteBindingReadModel


class RouteBindingRecordsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    product: str
    context: str
    instance: str
    limit: int
    count: int
    records: tuple[EnvironmentRouteBindingReadModel, ...]


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

    @model_validator(mode="before")
    @classmethod
    def _reject_target_reference_compatibility_input(cls, data: object) -> object:
        if _mapping_path_contains_key(data, "deployment", "deploy", "target_reference"):
            raise ValueError(
                "deployment evidence ingress rejects target_reference compatibility input"
            )
        return data

    def model_post_init(self, _context: object) -> None:
        if not self.product.strip():
            raise ValueError("deployment evidence requires product")


def _mapping_path_contains_key(payload: object, *path: str) -> bool:
    if not path or not isinstance(payload, Mapping):
        return False
    *parent_path, terminal_key = path
    current: object = payload
    for path_part in parent_path:
        if not isinstance(current, Mapping):
            return False
        current = current.get(path_part)
    return isinstance(current, Mapping) and terminal_key in current


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

    @model_validator(mode="before")
    @classmethod
    def _reject_target_reference_compatibility_input(cls, data: object) -> object:
        if _mapping_path_contains_key(data, "promotion", "deploy", "target_reference"):
            raise ValueError(
                "promotion evidence ingress rejects target_reference compatibility input"
            )
        return data

    def model_post_init(self, _context: object) -> None:
        if not self.product.strip():
            raise ValueError("promotion evidence requires product")


class AcceptedEvidenceResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["accepted"] = "accepted"
    trace_id: str
    records: dict[str, object]
    result: dict[str, object] | None = None
    replayed: bool | None = None
    original_trace_id: str | None = None


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
    return (requirement.context, requirement.instance, requirement.key)


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


class RouteBindingBackfillApplyEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    mode: Literal["dry-run", "apply"] = "dry-run"
    product: str
    context: str
    instance: str
    source_label: str = "operator-backfill"
    reason: str = ""
    confirmation: str = ""

    @model_validator(mode="after")
    def _validate_envelope(self) -> "RouteBindingBackfillApplyEnvelope":
        if self.schema_version != 1:
            raise ValueError("Unsupported route binding backfill schema version")
        self.product = self.product.strip()
        self.context = self.context.strip()
        self.instance = self.instance.strip()
        self.source_label = self.source_label.strip()
        self.reason = self.reason.strip()
        self.confirmation = self.confirmation.strip()
        if not self.product or not self.context or not self.instance:
            raise ValueError("Route binding backfill requires product, context, and instance")
        if not self.source_label:
            raise ValueError("Route binding backfill requires source_label")
        if self.mode == "apply":
            if not self.reason:
                raise ValueError("Route binding backfill apply requires a reason")
            if self.confirmation != "APPLY LAUNCHPLANE ROUTE BINDING":
                raise ValueError("Route binding backfill apply requires exact confirmation text")
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


class _PreviewPrFeedbackWriteStore(Protocol):
    def write_preview_pr_feedback_record(
        self,
        record: PreviewPrFeedbackRecord,
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


class _RouteBindingApplyStore(
    control_plane_route_binding_backfill.RouteBindingBackfillStore, Protocol
):
    def read_route_binding_record(
        self,
        *,
        product: str,
        context_name: str,
        instance_name: str,
    ) -> EnvironmentRouteBindingRecord: ...

    def write_route_binding_record(
        self,
        record: EnvironmentRouteBindingRecord,
    ) -> object: ...


class _RouteBindingMutationStore(_RouteBindingApplyStore, Protocol):
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
    ) -> MutationReservationResult: ...

    def release_mutation_reservation(
        self,
        *,
        reservation: LaunchplaneIdempotencyRecord,
    ) -> MutationReservationUpdateResult: ...

    def create_route_binding_record_with_mutation(
        self,
        *,
        record: EnvironmentRouteBindingRecord,
        reservation: LaunchplaneIdempotencyRecord,
        response_status_code: int,
        response_trace_id: str,
        response_payload: dict[str, Any],
    ) -> RouteBindingMutationResult: ...


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


class _RouteBindingReadStore(Protocol):
    def read_route_binding_record(
        self,
        *,
        product: str,
        context_name: str,
        instance_name: str,
    ) -> EnvironmentRouteBindingRecord: ...

    def list_route_binding_records(
        self,
        *,
        product: str = "",
        context_name: str = "",
        instance_name: str = "",
        status: str = "",
        limit: int | None = None,
    ) -> tuple[EnvironmentRouteBindingRecord, ...]: ...


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


def require_route_binding_read_store(record_store: object) -> _RouteBindingReadStore:
    required_methods = (
        "read_route_binding_record",
        "list_route_binding_records",
    )
    missing_methods = [
        method_name
        for method_name in required_methods
        if not callable(getattr(record_store, method_name, None))
    ]
    if missing_methods:
        missing_summary = ", ".join(missing_methods)
        raise TypeError(
            f"Launchplane record store does not support route binding reads: {missing_summary}"
        )
    return cast(_RouteBindingReadStore, record_store)


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


def require_route_binding_apply_store(record_store: object) -> _RouteBindingApplyStore:
    required_methods = (
        "read_provider_target_record",
        "read_dokploy_target_record",
        "read_dokploy_target_id_record",
        "read_route_binding_record",
        "list_edge_endpoint_records",
        "list_ingress_route_audit_records",
        "write_route_binding_record",
    )
    missing_methods = [
        method_name
        for method_name in required_methods
        if not callable(getattr(record_store, method_name, None))
    ]
    if missing_methods:
        missing_summary = ", ".join(missing_methods)
        raise TypeError(
            "Launchplane record store does not support route binding backfill applies: "
            f"{missing_summary}"
        )
    return cast(_RouteBindingApplyStore, record_store)


def require_route_binding_mutation_store(record_store: object) -> _RouteBindingMutationStore:
    route_binding_store = require_route_binding_apply_store(record_store)
    required_methods = (
        "reserve_mutation",
        "release_mutation_reservation",
        "create_route_binding_record_with_mutation",
    )
    missing_methods = [
        method_name
        for method_name in required_methods
        if not callable(getattr(route_binding_store, method_name, None))
    ]
    if missing_methods:
        missing_summary = ", ".join(missing_methods)
        raise TypeError(
            "Launchplane record store does not support atomic route binding mutations: "
            f"{missing_summary}"
        )
    return cast(_RouteBindingMutationStore, route_binding_store)


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


def read_generic_web_preview_profile(
    *, record_store: object, product: str
) -> LaunchplaneProductProfileRecord:
    try:
        profile_store = require_product_profile_read_store(record_store)
        profile = profile_store.read_product_profile_record(product.strip())
    except TypeError as error:
        raise TypeError(
            "Launchplane record store does not support generic web preview desired-state "
            "profile reads: read_product_profile_record"
        ) from error
    except FileNotFoundError as error:
        raise FileNotFoundError(
            "Generic web preview desired-state requires an existing product profile."
        ) from error
    if not product_profile_uses_generic_web_base(profile):
        raise ValueError(
            "Product profile is not compatible with the generic-web preview desired-state route."
        )
    if not profile.preview.enabled:
        raise ValueError("Product profile does not have generic-web previews enabled.")
    if not profile.preview.context.strip():
        raise ValueError("Product profile does not define a preview context.")
    return profile


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
    return request_fingerprint(
        canonical_request_payload_for_idempotency(route_path=route_path, payload=payload)
    )


def product_config_continuity_payload(payload: dict[str, object]) -> dict[str, object]:
    continuity_payload = dict(payload)
    continuity_payload.pop("mode", None)
    continuity_payload.pop("reason", None)
    return continuity_payload


def product_config_dry_run_key(payload: dict[str, object]) -> str:
    return (
        "local-operator-product-config-dry-run:"
        f"{request_fingerprint(product_config_continuity_payload(payload))}"
    )


def product_config_identity_actor(identity: LaunchplaneIdentity) -> str:
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


def product_config_dry_run_exists(
    *, record_store: object, identity: LaunchplaneIdentity, request_payload: dict[str, object]
) -> bool:
    idempotency_store = idempotency_capable_store(record_store)
    if idempotency_store is None:
        return False
    continuity_fingerprint = request_fingerprint(product_config_continuity_payload(request_payload))
    stored_record = idempotency_store.read_idempotency_record(
        scope=idempotency_scope(identity),
        route_path=_PRODUCT_CONFIG_APPLY_ROUTE,
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
) -> None:
    idempotency_store = idempotency_capable_store(record_store)
    if idempotency_store is None:
        return
    dry_run_idempotency_key = product_config_dry_run_key(request_payload)
    dry_run_request_fingerprint = request_fingerprint(
        product_config_continuity_payload(request_payload)
    )
    stored_record = idempotency_store.read_idempotency_record(
        scope=idempotency_scope(identity),
        route_path=_PRODUCT_CONFIG_APPLY_ROUTE,
        idempotency_key=dry_run_idempotency_key,
    )
    if product_config_dry_run_record_matches(
        record=stored_record,
        request_fingerprint_value=dry_run_request_fingerprint,
    ):
        return
    dry_run_trace_id = f"{trace_id}-local-operator-dry-run"
    try:
        idempotency_store.write_idempotency_record(
            LaunchplaneIdempotencyRecord(
                record_id=build_launchplane_idempotency_record_id(
                    response_trace_id=dry_run_trace_id
                ),
                scope=idempotency_scope(identity),
                route_path=_PRODUCT_CONFIG_APPLY_ROUTE,
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
                route_path=_PRODUCT_CONFIG_APPLY_ROUTE,
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
        responses = cast(
            dict[int | str, dict[str, Any]] | None,
            kwargs.get("responses"),
        )
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
    every_code_discord_sender: Callable[[str, dict[str, object]], object] = post_discord_webhook,
    preview_pr_feedback_discord_sender: Callable[
        [str, dict[str, object]], object
    ] = post_discord_webhook,
    every_code_github_webhook_handler: EveryCodeGitHubWebhookHandler | None = None,
) -> FastAPI:
    resolved_control_plane_root = (
        control_plane_root_path or FilePath(__file__).resolve().parent.parent
    )
    resolved_state_dir = state_dir or resolved_control_plane_root / "state"
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
    odoo_preview_apply_lock = asyncio.Lock()
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
        except Exception:  # noqa: BLE001
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
        return HTTPException(
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
            controller_status=read_model,
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
        targets: list[MergeTrainPolicyTarget] = []
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
                MergeTrainPolicyTarget(
                    repository=repository_policy.repository,
                    base_branch=repository_policy.base_branch,
                    policy_key=repository_policy.policy_key,
                    scheduler=repository_policy.scheduler,
                    service_authz=repository_policy.service_authz,
                )
            )
        targets.sort(key=lambda target: (target.repository, target.base_branch))
        return MergeTrainPolicyTargetsResponse(
            trace_id=trace_id,
            policy=MergeTrainPolicySummary(
                record_id=policy_record.record_id,
                updated_at=policy_record.updated_at,
                policy_sha256=policy_record.policy_sha256,
            ),
            targets=tuple(targets),
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
        payload_fingerprint = request_fingerprint(cast(dict[str, object], raw_payload))
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
        except TypeError as error:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message="Merge train batch candidate storage requires database-backed records.",
            ) from error
        try:
            batch_result = execute_merge_train_batch_candidate_run_once(
                request=batch_request,
                policy=policy_record.policy,
                policy_sha256=policy_record.policy_sha256,
                token=token,
                trace_id=trace_id,
                recorded_at=utc_now_timestamp(),
                batch_store=batch_store,
                stack_collapse_store=stack_collapse_store,
            )
        except MergeTrainGitHubStaleHeadError as error:
            return merge_train_github_stale_state_response(trace_id=trace_id, error=error)
        except MergeTrainGitHubError as error:
            return merge_train_github_request_failed_response(trace_id=trace_id, error=error)
        except MergeTrainBatchCandidateRecordNotFoundError as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request could not be completed.",
            ) from error
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
        payload_fingerprint = request_fingerprint(cast(dict[str, object], raw_payload))
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
        except TypeError as error:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message="Merge train controller storage requires database-backed records.",
            ) from error
        try:
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
            )
        except MergeTrainGitHubStaleHeadError as error:
            return merge_train_github_stale_state_response(trace_id=trace_id, error=error)
        except MergeTrainGitHubError as error:
            return merge_train_github_request_failed_response(trace_id=trace_id, error=error)
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
        store_apply_idempotency(
            record_store=record_store,
            identity=identity,
            route_path=_MERGE_TRAIN_CONTROLLER_RUN_ONCE_ROUTE,
            idempotency_key=normalized_idempotency_key,
            request_fingerprint_value=payload_fingerprint,
            trace_id=trace_id,
            response=response,
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

        authorization_product = (
            product_profile.product if product_profile is not None else publish_request.product
        )
        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action=ODOO_ARTIFACT_PUBLISH_ACTION,
            product=authorization_product,
            context=publish_request.publish.context,
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
            records={key: str(value) for key, value in records.items()},
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

        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action=ODOO_ARTIFACT_PUBLISH_INPUTS_ACTION,
            product=inputs_request.product,
            context=inputs_request.inputs.context,
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

        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action=ODOO_PREVIEW_APPLY_INPUTS_ACTION,
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

        response = accepted_evidence_response(
            trace_id=trace_id,
            records={},
            result=driver_result,
        )
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

        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action=ODOO_PREVIEW_APPLY_ACTION,
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

        async with odoo_preview_apply_lock:
            (
                normalized_idempotency_key,
                payload_fingerprint,
                replay_response,
            ) = await replay_apply_idempotency(
                request=request,
                record_store=record_store,
                identity=identity,
                route_path=_ODOO_PREVIEW_APPLY_ROUTE,
                idempotency_key=idempotency_key,
                trace_id=trace_id,
                check_replay=bool(idempotency_key.strip()),
            )
            if replay_response is not None:
                return replay_response

            async def execute_apply_and_store() -> AcceptedEvidenceResponse:
                driver_result = await asyncio.to_thread(
                    execute_odoo_preview_apply_result,
                    control_plane_root_path=resolved_control_plane_root,
                    record_store=record_store,
                    profile=product_profile,
                    request=apply_request,
                    database_url=getattr(record_store, "database_url", None),
                )
                response = accepted_evidence_response(
                    trace_id=trace_id,
                    records={},
                    result=driver_result,
                )
                if not driver_result_contains_status(driver_result, "blocked"):
                    store_apply_idempotency(
                        record_store=record_store,
                        identity=identity,
                        route_path=_ODOO_PREVIEW_APPLY_ROUTE,
                        idempotency_key=normalized_idempotency_key,
                        request_fingerprint_value=payload_fingerprint,
                        trace_id=trace_id,
                        response=response,
                    )
                return response

            apply_task = asyncio.create_task(
                execute_apply_and_store(),
                name=f"odoo-preview-apply:{trace_id}",
            )
            try:
                return await asyncio.shield(apply_task)
            except asyncio.CancelledError as cancellation:
                while not apply_task.done():
                    try:
                        await asyncio.shield(apply_task)
                    except asyncio.CancelledError:
                        continue
                    except Exception:
                        _LOGGER.exception(
                            "Odoo preview apply failed after request cancellation",
                            extra={"trace_id": trace_id},
                        )
                        break
                raise cancellation
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

        authorization_product = (
            product_profile.product if product_profile is not None else post_deploy_request.product
        )
        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action=ODOO_POST_DEPLOY_ACTION,
            product=authorization_product,
            context=post_deploy_request.post_deploy.context,
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
            records={key: str(value) for key, value in records.items()},
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

        authorization_product = (
            product_profile.product if product_profile is not None else maintenance_request.product
        )
        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action=ODOO_APP_MAINTENANCE_ACTION,
            product=authorization_product,
            context=maintenance_request.maintenance.context,
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
            records={key: str(value) for key, value in records.items()},
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

        authorization_product = (
            product_profile.product if product_profile is not None else override_request.product
        )
        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action=ODOO_CONFIG_PARAMETER_OVERRIDE_ACTION,
            product=authorization_product,
            context=override_request.override.context,
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

        authorization_product = (
            product_profile.product if product_profile is not None else override_request.product
        )
        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action=ODOO_WEBSITE_BOOTSTRAP_OVERRIDE_ACTION,
            product=authorization_product,
            context=override_request.override.context,
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

        authorization_product = (
            product_profile.product if product_profile is not None else backup_gate_request.product
        )
        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action=ODOO_PROD_BACKUP_GATE_ACTION,
            product=authorization_product,
            context=backup_gate_request.backup_gate.context,
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
            records={key: str(value) for key, value in records.items()},
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

        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action=ODOO_STABLE_BOOTSTRAP_ACTION,
            product=bootstrap_request.product,
            context=bootstrap_request.bootstrap.context,
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
        try:
            records, driver_result = enqueue_odoo_stable_bootstrap_operation(
                record_store=record_store,
                request=bootstrap_request,
                idempotency_key=normalized_idempotency_key,
                request_fingerprint=request_fingerprint(raw_payload),
                created_at=utc_now_timestamp(),
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
            records={key: str(value) for key, value in records.items()},
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

        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action=ODOO_TARGET_REPLACEMENT_PLAN_ACTION,
            product=plan_request.product,
            context=lane.context,
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

        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action=ODOO_TARGET_REPLACEMENT_APPLY_ACTION,
            product=apply_request.product,
            context=lane.context,
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
        try:
            records, driver_result = enqueue_odoo_target_replacement_apply_operation(
                record_store=record_store,
                request=apply_request,
                context=lane.context,
                idempotency_key=normalized_idempotency_key,
                idempotency_scope=idempotency_scope(identity),
                request_fingerprint=request_fingerprint(raw_payload),
                created_at=utc_now_timestamp(),
            )
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
            records={key: str(value) for key, value in records.items()},
            result=driver_result,
        )

    async def write_generic_web_rollback_plan(
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
            rollback_request = GenericWebRollbackPlanEnvelope.model_validate(raw_payload)
        except ValidationError as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request payload failed validation.",
            ) from error
        try:
            _profile, lane = resolve_generic_web_rollback_lane(
                record_store=record_store,
                product=rollback_request.product,
                route_path=_GENERIC_WEB_ROLLBACK_PLAN_ROUTE,
                instance=rollback_request.rollback_plan.instance,
            )
        except GenericWebRollbackRouteDependencyError:
            return driver_route_dependency_not_found_response(
                trace_id=trace_id,
                route_path=_GENERIC_WEB_ROLLBACK_PLAN_ROUTE,
            )
        except GenericWebRollbackProductMismatchError as error:
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
        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action=GENERIC_WEB_ROLLBACK_PLAN_ACTION,
            product=rollback_request.product,
            context=lane.context,
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message=(
                    "Workflow cannot plan the generic web prod rollback"
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
            route_path=_GENERIC_WEB_ROLLBACK_PLAN_ROUTE,
            idempotency_key=idempotency_key,
            trace_id=trace_id,
            check_replay=bool(idempotency_key.strip()),
        )
        if replay_response is not None:
            return replay_response
        try:
            records, driver_result = execute_generic_web_rollback_plan_result(
                record_store=record_store,
                request=rollback_request,
            )
        except FileNotFoundError as error:
            raise _launchplane_http_error(
                status_code=404,
                trace_id=trace_id,
                code="not_found",
                message=f"No Launchplane route for {_GENERIC_WEB_ROLLBACK_PLAN_ROUTE}.",
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
            records={key: str(value) for key, value in records.items()},
            result=driver_result,
        )
        if should_store_generic_web_rollback_idempotency(driver_result):
            store_apply_idempotency(
                record_store=record_store,
                identity=identity,
                route_path=_GENERIC_WEB_ROLLBACK_PLAN_ROUTE,
                idempotency_key=normalized_idempotency_key,
                request_fingerprint_value=payload_fingerprint,
                trace_id=trace_id,
                response=response,
            )
        return response

    async def write_generic_web_rollback(
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
            rollback_request = GenericWebRollbackEnvelope.model_validate(raw_payload)
        except ValidationError as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request payload failed validation.",
            ) from error
        try:
            profile, lane = resolve_generic_web_rollback_lane(
                record_store=record_store,
                product=rollback_request.product,
                route_path=_GENERIC_WEB_ROLLBACK_ROUTE,
                instance=rollback_request.rollback.instance,
            )
        except GenericWebRollbackRouteDependencyError:
            return driver_route_dependency_not_found_response(
                trace_id=trace_id,
                route_path=_GENERIC_WEB_ROLLBACK_ROUTE,
            )
        except GenericWebRollbackProductMismatchError as error:
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
        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action=GENERIC_WEB_ROLLBACK_ACTION,
            product=rollback_request.product,
            context=lane.context,
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message=(
                    "Workflow cannot execute the generic web prod rollback"
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
            route_path=_GENERIC_WEB_ROLLBACK_ROUTE,
            idempotency_key=idempotency_key,
            trace_id=trace_id,
            check_replay=bool(idempotency_key.strip()),
        )
        if replay_response is not None:
            return replay_response
        try:
            records, driver_result = execute_generic_web_rollback_result(
                control_plane_root=resolved_control_plane_root,
                record_store=record_store,
                request=rollback_request,
                profile=profile,
            )
        except FileNotFoundError as error:
            raise _launchplane_http_error(
                status_code=404,
                trace_id=trace_id,
                code="not_found",
                message=f"No Launchplane route for {_GENERIC_WEB_ROLLBACK_ROUTE}.",
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
            records={key: str(value) for key, value in records.items()},
            result=driver_result,
        )
        if should_store_generic_web_rollback_idempotency(driver_result):
            store_apply_idempotency(
                record_store=record_store,
                identity=identity,
                route_path=_GENERIC_WEB_ROLLBACK_ROUTE,
                idempotency_key=normalized_idempotency_key,
                request_fingerprint_value=payload_fingerprint,
                trace_id=trace_id,
                response=response,
            )
        return response

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
                context="",
                instance="",
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

        authorization_product = (
            product_profile.product if product_profile is not None else rollback_request.product
        )
        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action=ODOO_PROD_ROLLBACK_ACTION,
            product=authorization_product,
            context=rollback_request.rollback.context,
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
            records={key: str(value) for key, value in records.items()},
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

        authorization_product = (
            product_profile.product if product_profile is not None else promotion_request.product
        )
        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action=ODOO_PROD_PROMOTION_ACTION,
            product=authorization_product,
            context=promotion_request.promotion.context,
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
            records={key: str(value) for key, value in records.items()},
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

        authorization_product = (
            product_profile.product if product_profile is not None else inputs_request.product
        )
        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action=ODOO_PROD_PROMOTION_INPUTS_ACTION,
            product=authorization_product,
            context=inputs_request.inputs.context,
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
            records={key: str(value) for key, value in records.items()},
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

        authorization_product = (
            product_profile.product if product_profile is not None else run_request.product
        )
        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action=ODOO_PROD_PROMOTION_RUN_ACTION,
            product=authorization_product,
            context=run_request.run.context,
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
            records={key: str(value) for key, value in records.items()},
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
        payload_fingerprint = request_fingerprint(cast(dict[str, object], raw_payload))
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
        payload_fingerprint = request_fingerprint(cast(dict[str, object], raw_payload))
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
            stack_result = execute_merge_train_stack_collapse_run_once(
                request=stack_request,
                policy=policy_record.policy,
                policy_sha256=policy_record.policy_sha256,
                token=token,
                trace_id=trace_id,
                recorded_at=utc_now_timestamp(),
                stack_collapse_store=stack_collapse_store,
                batch_candidate_store=batch_candidate_store,
            )
        except MergeTrainGitHubStaleHeadError as error:
            return merge_train_github_stale_state_response(trace_id=trace_id, error=error)
        except MergeTrainGitHubError as error:
            return merge_train_github_request_failed_response(trace_id=trace_id, error=error)
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
        payload_fingerprint = request_fingerprint(cast(dict[str, object], raw_payload))
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
        except TypeError as error:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message="Merge train batch landing storage requires database-backed records.",
            ) from error
        try:
            landing_result = execute_merge_train_batch_landing_run_once(
                request=landing_request,
                repository_policy=repository_policy,
                policy_sha256=policy_record.policy_sha256,
                token=token,
                trace_id=trace_id,
                recorded_at=utc_now_timestamp(),
                candidate_store=candidate_store,
                landing_store=landing_store,
                stack_collapse_store=stack_collapse_store,
            )
        except MergeTrainGitHubStaleHeadError as error:
            return merge_train_github_stale_state_response(trace_id=trace_id, error=error)
        except MergeTrainGitHubError as error:
            return merge_train_github_request_failed_response(trace_id=trace_id, error=error)
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
        payload_fingerprint = request_fingerprint(cast(dict[str, object], raw_payload))
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
        except TypeError as error:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message=str(error),
            ) from error
        try:
            run_once_result = execute_merge_train_run_once(
                request=merge_train_request,
                policy=policy_record.policy,
                policy_sha256=policy_record.policy_sha256,
                token=token,
                trace_id=trace_id,
                recorded_at=utc_now_timestamp(),
            )
        except MergeTrainGitHubStaleHeadError as error:
            return merge_train_github_stale_state_response(trace_id=trace_id, error=error)
        except MergeTrainGitHubError as error:
            return merge_train_github_request_failed_response(trace_id=trace_id, error=error)
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
        payload_fingerprint = request_fingerprint(cast(dict[str, object], raw_payload))
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
        new_lease_expires_at = _add_lease_seconds(now, heartbeat_request.lease_seconds)
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
        source: Annotated[str, Query()] = "runtime",
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
            normalized_source = (
                control_plane_tracked_target_logs.normalize_tracked_target_log_source(source)
            )
            if normalized_source == "deployment" and normalized_since != "all":
                raise ValueError("Tracked deployment logs require since='all'.")
            if normalized_source == "deployment" and normalized_search:
                raise ValueError("Tracked deployment logs do not support search.")
        except (ValueError, click.ClickException) as error:
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
                source=normalized_source,
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
        except control_plane_tracked_target_logs.TrackedTargetLogsProviderError as error:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="target_logs_unavailable",
                message=(
                    f"Tracked target logs are unavailable during {error.operation}: {error.detail}"
                ),
            ) from error
        except click.ClickException as error:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="target_logs_unavailable",
                message="Tracked target logs are unavailable from the provider.",
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

    def ensure_route_binding_allowed(
        *,
        identity: LaunchplaneIdentity,
        trace_id: str,
        action: str,
        product: str,
        context_name: str,
        message: str,
    ) -> None:
        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action=action,
            product=product,
            context=context_name,
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message=message,
            )

    def list_route_binding_records(
        identity: Annotated[LaunchplaneIdentity, Depends(read_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
        product: Annotated[str, Query()] = "",
        context: Annotated[str, Query()] = "",
        instance: Annotated[str, Query()] = "",
        status: Annotated[str, Query()] = "",
        limit: Annotated[str, Query()] = "25",
    ) -> RouteBindingRecordsResponse:
        trace_id = next_trace_id()
        normalized_product = product.strip()
        context_name = context.strip()
        instance_name = instance.strip()
        if not normalized_product or not context_name:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_query",
                message="Route binding list requires product and context query parameters.",
            )
        ensure_route_binding_allowed(
            identity=identity,
            trace_id=trace_id,
            action="route_binding.read",
            product=normalized_product,
            context_name=context_name,
            message="Workflow cannot read route bindings for the requested product/context.",
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
            route_binding_store = require_route_binding_read_store(record_store)
            records = route_binding_store.list_route_binding_records(
                product=normalized_product,
                context_name=context_name,
                instance_name=instance_name,
                status=status.strip(),
                limit=normalized_limit,
            )
        except ValueError as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_query",
                message=str(error),
            ) from error
        except TypeError as error:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message=str(error),
            ) from error
        return RouteBindingRecordsResponse(
            trace_id=trace_id,
            product=normalized_product,
            context=context_name,
            instance=instance_name,
            limit=normalized_limit,
            count=len(records),
            records=tuple(redacted_route_binding_record(record) for record in records),
        )

    def read_route_binding_record(
        identity: Annotated[LaunchplaneIdentity, Depends(read_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
        product: Annotated[str, Query()] = "",
        context: Annotated[str, Query()] = "",
        instance: Annotated[str, Query()] = "",
    ) -> RouteBindingRecordResponse:
        trace_id = next_trace_id()
        normalized_product = product.strip()
        context_name = context.strip()
        instance_name = instance.strip()
        if not normalized_product or not context_name or not instance_name:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_query",
                message=(
                    "Route binding record reads require product, context, and instance "
                    "query parameters."
                ),
            )
        ensure_route_binding_allowed(
            identity=identity,
            trace_id=trace_id,
            action="route_binding.read",
            product=normalized_product,
            context_name=context_name,
            message="Workflow cannot read route bindings for the requested product/context.",
        )
        try:
            route_binding_store = require_route_binding_read_store(record_store)
            record = route_binding_store.read_route_binding_record(
                product=normalized_product,
                context_name=context_name,
                instance_name=instance_name,
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
        return RouteBindingRecordResponse(
            trace_id=trace_id,
            record=redacted_route_binding_record(record),
        )

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
        request_payload = cast(dict[str, object], raw_payload)
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
                payload=cast(dict[str, object], raw_payload),
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
        records: Mapping[str, object],
        result: dict[str, object] | None = None,
        replayed: bool = False,
        original_trace_id: str = "",
    ) -> AcceptedEvidenceResponse:
        return AcceptedEvidenceResponse(
            trace_id=trace_id,
            records=dict(records),
            result=result,
            replayed=True if replayed else None,
            original_trace_id=original_trace_id or None,
        )

    def replay_idempotent_response(
        *,
        trace_id: str,
        stored_record: LaunchplaneIdempotencyRecord,
        route_path: str = "",
    ) -> AcceptedEvidenceResponse:
        stored_records = {
            str(key): value
            if str(key).endswith("_preview_verification") and isinstance(value, dict)
            else str(value)
            for key, value in dict(stored_record.response_payload.get("records") or {}).items()
        }
        stored_result = stored_record.response_payload.get("result")
        if route_path in {
            _GENERIC_WEB_DEPLOY_ROUTE,
            _GENERIC_WEB_PROD_PROMOTION_ROUTE,
            _GENERIC_WEB_PROD_PROMOTION_WORKFLOW_ROUTE,
            _VERIREEL_PROD_DEPLOY_ROUTE,
            _VERIREEL_PROD_PROMOTION_ROUTE,
            _VERIREEL_TESTING_DEPLOY_ROUTE,
        } and isinstance(stored_result, dict):
            stored_records.pop("target_type", None)
            stored_result = {str(key): value for key, value in stored_result.items()}
            stored_result.pop("target_type", None)
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

    def require_secret_reencryption_database_store(
        *, record_store: object, trace_id: str
    ) -> PostgresRecordStore:
        if not isinstance(record_store, PostgresRecordStore):
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_required",
                message="Managed-secret re-encryption requires DB-backed Launchplane storage.",
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
        payload_fingerprint = idempotency_request_fingerprint(
            route_path=route_path,
            payload=cast(dict[str, object], raw_payload),
        )
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
        except ValueError as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Product config request failed validation.",
            ) from error
        if not isinstance(raw_payload, dict):
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Product config request failed validation.",
            )
        request_payload = cast(dict[str, object], raw_payload)
        try:
            product_config_request = ProductConfigApplyEnvelope.model_validate(request_payload)
        except ValidationError as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Product config request failed validation.",
            ) from error
        if (
            isinstance(identity, LocalOperatorIdentity | LocalAdminIdentity)
            and not product_config_request.reason
        ):
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="reason_required",
                message="Local operator product-config requests require a reason.",
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
        if (
            isinstance(identity, LocalOperatorIdentity | LocalAdminIdentity)
            and product_config_request.mode == "apply"
            and not product_config_dry_run_exists(
                record_store=record_store,
                identity=identity,
                request_payload=request_payload,
            )
        ):
            raise _launchplane_http_error(
                status_code=409,
                trace_id=trace_id,
                code="matching_dry_run_required",
                message="Local operator product-config apply requires a prior matching dry-run.",
            )
        (
            normalized_idempotency_key,
            payload_fingerprint,
            replay_response,
        ) = await replay_apply_idempotency(
            request=request,
            record_store=record_store,
            identity=identity,
            route_path=_PRODUCT_CONFIG_APPLY_ROUTE,
            idempotency_key=idempotency_key,
            trace_id=trace_id,
            check_replay=bool(idempotency_key.strip()),
        )
        if replay_response is not None:
            return ProductConfigApplyResponse.model_validate(
                replay_response.model_dump(mode="json")
            )
        database_store = require_product_config_database_store(
            record_store=record_store,
            trace_id=trace_id,
        )
        try:
            driver_result, authority_bundle = (
                control_plane_product_config.plan_product_config_authority_bundle(
                    record_store=database_store,
                    payload=product_config_request.product_config_payload(),
                    mode=product_config_request.mode,
                    actor=product_config_identity_actor(identity),
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
        next_actions = product_config_live_target_next_actions(
            request=product_config_request,
            driver_result=driver_result,
            tracked_targets=database_store.list_dokploy_target_records(),
        )
        if next_actions and driver_result is not None:
            driver_result = {
                **driver_result,
                "status": "records_applied_live_sync_required",
                "next_actions": next_actions,
            }
        product_config_response = ProductConfigApplyResponse(
            trace_id=trace_id,
            records={},
            result=ProductConfigApplyResult.model_validate(driver_result),
        )
        if (
            isinstance(identity, LocalOperatorIdentity | LocalAdminIdentity)
            and product_config_request.mode == "dry-run"
        ):
            store_product_config_dry_run_record(
                record_store=database_store,
                identity=identity,
                request_payload=request_payload,
                trace_id=trace_id,
                response=product_config_response,
            )
        if product_config_request.mode == "dry-run":
            store_apply_idempotency(
                record_store=database_store,
                identity=identity,
                route_path=_PRODUCT_CONFIG_APPLY_ROUTE,
                idempotency_key=normalized_idempotency_key,
                request_fingerprint_value=payload_fingerprint,
                trace_id=trace_id,
                response=product_config_response,
            )
        else:
            database_store.write_product_authority_bundle(
                authority_bundle_with_apply_idempotency(
                    bundle=authority_bundle,
                    identity=identity,
                    route_path=_PRODUCT_CONFIG_APPLY_ROUTE,
                    idempotency_key=normalized_idempotency_key,
                    request_fingerprint_value=payload_fingerprint,
                    trace_id=trace_id,
                    response=product_config_response,
                )
            )
        return product_config_response

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
        request_payload = cast(dict[str, object], raw_payload)
        try:
            reencryption_request = SecretReencryptionRequest.model_validate(request_payload)
        except ValidationError as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Managed-secret re-encryption request failed validation.",
            ) from error
        action = f"secret.reencrypt.{reencryption_request.mode}"
        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action=action,
            product="launchplane",
            context="launchplane",
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Workflow cannot re-encrypt Launchplane managed secrets.",
            )
        if reencryption_request.mode == "apply" and not idempotency_key.strip():
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="idempotency_key_required",
                message="Managed-secret re-encryption apply requires Idempotency-Key.",
            )
        (
            normalized_idempotency_key,
            payload_fingerprint,
            replay_response,
        ) = await replay_apply_idempotency(
            request=request,
            record_store=record_store,
            identity=identity,
            route_path=_SECRET_REENCRYPT_ROUTE,
            idempotency_key=idempotency_key,
            trace_id=trace_id,
            check_replay=reencryption_request.mode == "apply",
        )
        if replay_response is not None:
            return replay_response
        database_store = require_secret_reencryption_database_store(
            record_store=record_store,
            trace_id=trace_id,
        )
        actor = product_config_identity_actor(identity)
        operation_token = ""
        if reencryption_request.mode == "apply":
            operation_token = hashlib.sha256(
                "\x1f".join((actor, normalized_idempotency_key, payload_fingerprint)).encode(
                    "utf-8"
                )
            ).hexdigest()

        def reencryption_idempotency_record(
            result_payload: dict[str, object],
        ) -> LaunchplaneIdempotencyRecord:
            return build_apply_idempotency_record(
                identity=identity,
                route_path=_SECRET_REENCRYPT_ROUTE,
                idempotency_key=normalized_idempotency_key,
                request_fingerprint_value=payload_fingerprint,
                trace_id=trace_id,
                response=accepted_evidence_response(
                    trace_id=trace_id,
                    records={},
                    result=result_payload,
                ),
            )

        try:
            result = control_plane_secrets.reencrypt_secrets(
                record_store=database_store,
                apply=reencryption_request.mode == "apply",
                expected_plan_digest=reencryption_request.expected_plan_digest,
                operation_token=operation_token,
                idempotency_record_factory=(
                    reencryption_idempotency_record
                    if reencryption_request.mode == "apply"
                    else None
                ),
                actor=actor,
                source_label=reencryption_request.source_label,
                reason=reencryption_request.reason,
            )
        except click.ClickException as error:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="secret_key_configuration_invalid",
                message="Managed-secret key configuration is unavailable or invalid.",
            ) from error
        if reencryption_request.mode == "apply" and result["status"] != "ok":
            raise _launchplane_http_error(
                status_code=409,
                trace_id=trace_id,
                code="secret_reencryption_conflict",
                message="Managed-secret state no longer matches the approved dry-run.",
            )
        response = accepted_evidence_response(trace_id=trace_id, records={}, result=result)
        return response

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
        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action="product_onboarding.apply",
            product=onboarding_request.product,
            context=_LAUNCHPLANE_SERVICE_CONTEXT,
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Workflow cannot apply Launchplane product onboarding manifests.",
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
        try:
            onboarding_result, authority_bundle = plan_product_onboarding_authority_bundle(
                record_store=database_store,
                manifest=onboarding_request.manifest,
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
        onboarding_response = accepted_evidence_response(
            trace_id=trace_id,
            records={
                "product_profile": str(result["product_profile"]),
                "provider_target_count": str(result["provider_target_count"]),
                "provider_target_id_count": str(result["provider_target_id_count"]),
                "runtime_environment_record_count": str(result["runtime_environment_record_count"]),
                "secret_binding_count": str(result["secret_binding_count"]),
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
        payload_fingerprint = request_fingerprint(cast(dict[str, object], raw_payload))
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
        return {"authz_policy_record_id": str(record_id)}

    def validate_authz_policy_route_payload(
        *,
        raw_payload: object,
        envelope_model: type[BaseModel],
        trace_id: str,
    ) -> AuthzPolicyRouteEnvelope:
        if not isinstance(raw_payload, dict):
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request payload failed validation.",
            )
        try:
            return cast(
                AuthzPolicyRouteEnvelope,
                envelope_model.model_validate(raw_payload),
            )
        except ValidationError as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request payload failed validation.",
            ) from error

    async def apply_authz_policy_route(
        *,
        request: Request,
        identity: LaunchplaneIdentity,
        record_store: object,
        idempotency_key: str,
        route_path: str,
        envelope_model: type[BaseModel],
        database_required_message: str,
        denied_message: str,
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
        authz_request = validate_authz_policy_route_payload(
            raw_payload=raw_payload,
            envelope_model=envelope_model,
            trace_id=trace_id,
        )
        database_store = require_authz_policy_database_store(
            record_store=record_store,
            trace_id=trace_id,
            message=database_required_message,
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
                message=denied_message,
            )
        normalized_idempotency_key = idempotency_key.strip()
        payload_fingerprint = request_fingerprint(cast(dict[str, object], raw_payload))
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
                check_replay=bool(normalized_idempotency_key),
            )
            if replay_response is not None:
                return replay_response
        try:
            route_result = control_plane_authz_grant_service.execute_authz_policy_route(
                record_store=database_store,
                request=authz_request,
                identity=identity,
                trace_id=trace_id,
                now_timestamp=authz_policy_record_timestamp,
            )
        except ValueError as error:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="authz_policy_unavailable",
                message="Launchplane active authz policy is unavailable.",
            ) from error
        if authz_request.mode == "apply":
            resolved_authz_policy_runtime.update(
                route_result.updated_policy,
                policy_sha256=route_result.authz_policy_record.policy_sha256,
                source="db",
            )
        response = accepted_evidence_response(
            trace_id=trace_id,
            records=authz_policy_route_records(route_result.result),
            result=route_result.driver_result,
        )
        if authz_request.mode == "apply":
            store_apply_idempotency(
                record_store=database_store,
                identity=identity,
                route_path=route_path,
                idempotency_key=normalized_idempotency_key,
                request_fingerprint_value=payload_fingerprint,
                trace_id=trace_id,
                response=response,
            )
        return response

    async def grant_github_actions_authz_policy(
        request: Request,
        identity: Annotated[LaunchplaneIdentity, Depends(read_browser_mutation_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> AcceptedEvidenceResponse:
        return await apply_authz_policy_route(
            request=request,
            identity=identity,
            record_store=record_store,
            idempotency_key=idempotency_key,
            route_path=_AUTHZ_POLICY_GITHUB_ACTIONS_GRANTS_ROUTE,
            envelope_model=control_plane_authz_grant_service.AuthzPolicyGitHubActionsGrantEnvelope,
            database_required_message="Authz policy grant writes require Launchplane database storage.",
            denied_message="Workflow cannot write Launchplane authz policy grants.",
        )

    async def remove_github_actions_authz_policy(
        request: Request,
        identity: Annotated[LaunchplaneIdentity, Depends(read_browser_mutation_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> AcceptedEvidenceResponse:
        return await apply_authz_policy_route(
            request=request,
            identity=identity,
            record_store=record_store,
            idempotency_key=idempotency_key,
            route_path=_AUTHZ_POLICY_GITHUB_ACTIONS_REMOVALS_ROUTE,
            envelope_model=control_plane_authz_grant_service.AuthzPolicyGitHubActionsRemovalEnvelope,
            database_required_message="Authz policy removals require Launchplane database storage.",
            denied_message="Workflow cannot remove Launchplane authz policy grants.",
        )

    async def grant_github_human_authz_policy(
        request: Request,
        identity: Annotated[LaunchplaneIdentity, Depends(read_browser_mutation_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> AcceptedEvidenceResponse:
        return await apply_authz_policy_route(
            request=request,
            identity=identity,
            record_store=record_store,
            idempotency_key=idempotency_key,
            route_path=_AUTHZ_POLICY_GITHUB_HUMANS_GRANTS_ROUTE,
            envelope_model=control_plane_authz_grant_service.AuthzPolicyGitHubHumanGrantEnvelope,
            database_required_message="Authz human policy grant writes require Launchplane database storage.",
            denied_message="Workflow cannot write Launchplane authz human policy grants.",
        )

    async def grant_terminal_agent_authz_policy(
        request: Request,
        identity: Annotated[LaunchplaneIdentity, Depends(read_browser_mutation_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> AcceptedEvidenceResponse:
        return await apply_authz_policy_route(
            request=request,
            identity=identity,
            record_store=record_store,
            idempotency_key=idempotency_key,
            route_path=_AUTHZ_POLICY_TERMINAL_AGENTS_GRANTS_ROUTE,
            envelope_model=control_plane_authz_grant_service.AuthzPolicyTerminalAgentGrantEnvelope,
            database_required_message="Authz terminal-agent policy grant writes require Launchplane database storage.",
            denied_message="Workflow cannot write Launchplane authz terminal-agent policy grants.",
        )

    async def grant_local_operator_authz_policy(
        request: Request,
        identity: Annotated[LaunchplaneIdentity, Depends(read_browser_mutation_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> AcceptedEvidenceResponse:
        return await apply_authz_policy_route(
            request=request,
            identity=identity,
            record_store=record_store,
            idempotency_key=idempotency_key,
            route_path=_AUTHZ_POLICY_LOCAL_OPERATORS_GRANTS_ROUTE,
            envelope_model=control_plane_authz_grant_service.AuthzPolicyLocalOperatorGrantEnvelope,
            database_required_message="Authz local-operator policy grant writes require Launchplane database storage.",
            denied_message="Workflow cannot write Launchplane authz local-operator policy grants.",
        )

    async def grant_local_admin_authz_policy(
        request: Request,
        identity: Annotated[LaunchplaneIdentity, Depends(read_browser_mutation_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> AcceptedEvidenceResponse:
        return await apply_authz_policy_route(
            request=request,
            identity=identity,
            record_store=record_store,
            idempotency_key=idempotency_key,
            route_path=_AUTHZ_POLICY_LOCAL_ADMINS_GRANTS_ROUTE,
            envelope_model=control_plane_authz_grant_service.AuthzPolicyLocalAdminGrantEnvelope,
            database_required_message="Authz local-admin policy grant writes require Launchplane database storage.",
            denied_message="Workflow cannot write Launchplane authz local-admin policy grants.",
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
        payload_fingerprint = request_fingerprint(cast(dict[str, object], raw_payload))
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
            result, authority_bundle = (
                control_plane_product_context_cutover.plan_product_context_cutover_authority_bundle(
                    record_store=database_store,
                    request=context_cutover_request,
                )
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
        if context_cutover_request.mode == "apply":
            database_store.write_product_authority_bundle(
                authority_bundle_with_apply_idempotency(
                    bundle=authority_bundle,
                    identity=identity,
                    route_path=_PRODUCT_CONTEXT_CUTOVER_APPLY_ROUTE,
                    idempotency_key=normalized_idempotency_key,
                    request_fingerprint_value=payload_fingerprint,
                    trace_id=trace_id,
                    response=response,
                )
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
            result, authority_bundle = (
                control_plane_product_context_cutover.plan_legacy_context_cleanup_authority_bundle(
                    record_store=database_store,
                    request=legacy_cleanup_request,
                )
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
        if legacy_cleanup_request.mode == "apply":
            database_store.write_product_authority_bundle(
                authority_bundle_with_apply_idempotency(
                    bundle=authority_bundle,
                    identity=identity,
                    route_path=_PRODUCT_LEGACY_CONTEXT_CLEANUP_APPLY_ROUTE,
                    idempotency_key=normalized_idempotency_key,
                    request_fingerprint_value=payload_fingerprint,
                    trace_id=trace_id,
                    response=response,
                )
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

    async def apply_route_binding_backfill(
        request: Request,
        binding_request: RouteBindingBackfillApplyEnvelope,
        identity: Annotated[LaunchplaneIdentity, Depends(read_write_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> AcceptedEvidenceResponse:
        trace_id = next_trace_id()
        ensure_route_binding_allowed(
            identity=identity,
            trace_id=trace_id,
            action="route_binding.apply",
            product=binding_request.product,
            context_name=binding_request.context,
            message="Workflow cannot apply route bindings for the requested product/context.",
        )
        if binding_request.mode == "apply" and not idempotency_key.strip():
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="idempotency_key_required",
                message="Route binding backfill apply requests require an Idempotency-Key header.",
            )
        normalized_key = idempotency_key.strip()
        payload_fingerprint = ""
        try:
            route_binding_store = require_route_binding_apply_store(record_store)
        except TypeError as error:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message=str(error),
            ) from error
        mutation_store: _RouteBindingMutationStore | None = None
        mutation_reservation: LaunchplaneIdempotencyRecord | None = None
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
                route_path=_ROUTE_BINDING_BACKFILL_APPLY_ROUTE,
                payload=cast(dict[str, object], raw_payload),
            )
            reservation_result = mutation_store.reserve_mutation(
                scope=idempotency_scope(identity),
                route_path=_ROUTE_BINDING_BACKFILL_APPLY_ROUTE,
                idempotency_key=normalized_key,
                request_fingerprint=payload_fingerprint,
                lease_owner=trace_id,
                lease_seconds=int(_DB_ONLY_MUTATION_LEASE.total_seconds()),
            )
            if reservation_result.status == "replayed":
                return replay_idempotent_response(
                    trace_id=trace_id,
                    stored_record=reservation_result.record,
                    route_path=_ROUTE_BINDING_BACKFILL_APPLY_ROUTE,
                )
            if reservation_result.status == "conflict":
                raise _launchplane_http_error(
                    status_code=409,
                    trace_id=trace_id,
                    code="idempotency_key_reused",
                    message=(
                        "Idempotency-Key was already used for a different "
                        "Launchplane request payload on this route."
                    ),
                )
            if reservation_result.status == "in_progress":
                raise _launchplane_http_error(
                    status_code=409,
                    trace_id=trace_id,
                    code="mutation_in_progress",
                    message=(
                        "A matching route binding mutation is already running. "
                        "Retry with the same Idempotency-Key."
                    ),
                )
            if reservation_result.status == "reconcile_required":
                raise _launchplane_http_error(
                    status_code=409,
                    trace_id=trace_id,
                    code="mutation_reconciliation_required",
                    message=("The route binding mutation requires reconciliation before retry."),
                )
            mutation_reservation = reservation_result.record

        def release_route_binding_reservation() -> None:
            if mutation_store is None or mutation_reservation is None:
                return
            release_result = mutation_store.release_mutation_reservation(
                reservation=mutation_reservation,
            )
            if release_result.status != "released":
                raise RuntimeError(
                    "Route binding mutation reservation could not be released before effects."
                )

        def existing_route_binding_response(
            existing_record: EnvironmentRouteBindingRecord,
        ) -> AcceptedEvidenceResponse:
            existing_plan = control_plane_route_binding_backfill.RouteBindingBackfillPlan(
                status="blocked",
                findings=(
                    control_plane_route_binding_backfill.RouteBindingBackfillFinding(
                        code="route_binding_exists",
                        detail=(
                            "Backfill will not overwrite an existing environment route-binding "
                            "record."
                        ),
                    ),
                ),
            )
            return accepted_evidence_response(
                trace_id=trace_id,
                records={
                    "route_binding_status": "blocked",
                    "product": existing_record.product,
                    "context": existing_record.context,
                    "instance": existing_record.instance,
                },
                result={
                    "mode": binding_request.mode,
                    **existing_plan.model_dump(mode="json", exclude_none=True),
                },
            )

        try:
            existing_record = route_binding_store.read_route_binding_record(
                product=binding_request.product,
                context_name=binding_request.context,
                instance_name=binding_request.instance,
            )
        except FileNotFoundError:
            existing_record = None
        if existing_record is not None:
            release_route_binding_reservation()
            return existing_route_binding_response(existing_record)
        backfill_plan = control_plane_route_binding_backfill.plan_route_binding_backfill(
            record_store=route_binding_store,
            request=control_plane_route_binding_backfill.RouteBindingBackfillRequest(
                product=binding_request.product,
                context=binding_request.context,
                instance=binding_request.instance,
                source_label=binding_request.source_label,
                evaluated_at=utc_now_timestamp(),
            ),
        )
        if backfill_plan.status != "ready" or backfill_plan.record is None:
            release_route_binding_reservation()
            return accepted_evidence_response(
                trace_id=trace_id,
                records={
                    "route_binding_status": "blocked",
                    "product": binding_request.product,
                    "context": binding_request.context,
                    "instance": binding_request.instance,
                },
                result={
                    "mode": binding_request.mode,
                    **backfill_plan.model_dump(mode="json", exclude_none=True),
                },
            )
        record_status = "applied" if binding_request.mode == "apply" else "planned"
        response = accepted_evidence_response(
            trace_id=trace_id,
            records={
                "route_binding_status": record_status,
                "product": backfill_plan.record.product,
                "context": backfill_plan.record.context,
                "instance": backfill_plan.record.instance,
            },
            result={
                "mode": binding_request.mode,
                "route_binding_status": record_status,
                "record": redacted_route_binding_record(backfill_plan.record).model_dump(
                    mode="json"
                ),
            },
        )
        if binding_request.mode == "apply":
            if mutation_store is None or mutation_reservation is None:
                raise RuntimeError("Route binding apply requires a mutation reservation.")
            mutation_result = mutation_store.create_route_binding_record_with_mutation(
                record=backfill_plan.record,
                reservation=mutation_reservation,
                response_status_code=202,
                response_trace_id=trace_id,
                response_payload=response.model_dump(mode="json", exclude_none=True),
            )
            if mutation_result.status == "created":
                return response
            if mutation_result.status == "replayed":
                if mutation_result.idempotency_record is None:
                    raise RuntimeError("Replayed route binding mutation requires evidence.")
                return replay_idempotent_response(
                    trace_id=trace_id,
                    stored_record=mutation_result.idempotency_record,
                    route_path=_ROUTE_BINDING_BACKFILL_APPLY_ROUTE,
                )
            if mutation_result.status == "exists":
                if mutation_result.route_binding is None:
                    raise RuntimeError("Existing route binding mutation requires a record.")
                return existing_route_binding_response(mutation_result.route_binding)
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
                    message=("The route binding mutation requires reconciliation before retry."),
                )
            if mutation_result.status == "reservation_expired":
                raise _launchplane_http_error(
                    status_code=409,
                    trace_id=trace_id,
                    code="mutation_lease_expired",
                    message=(
                        "The route binding mutation lease expired before completion. "
                        "Retry with the same Idempotency-Key."
                    ),
                )
            raise _launchplane_http_error(
                status_code=409,
                trace_id=trace_id,
                code="mutation_in_progress",
                message=(
                    "The route binding mutation reservation changed before completion. "
                    "Retry with the same Idempotency-Key."
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

    async def apply_generic_web_preview_desired_state(
        request: Request,
        desired_state_request: GenericWebPreviewDesiredStateEnvelope,
        identity: Annotated[LaunchplaneIdentity, Depends(read_write_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> AcceptedEvidenceResponse:
        trace_id = next_trace_id()
        try:
            profile = read_generic_web_preview_profile(
                record_store=record_store,
                product=desired_state_request.product,
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
                status_code=503,
                trace_id=trace_id,
                code="driver_route_dependency_not_found",
                message=(
                    "Driver route is registered, but required product or runtime records "
                    "were not found."
                ),
            ) from error
        except (ValueError, click.ClickException) as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request could not be completed.",
            ) from error
        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action="preview_desired_state.discover",
            product=profile.product,
            context=profile.preview.context,
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message=(
                    "Workflow cannot discover generic web preview desired state "
                    "for the requested product/context."
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
            route_path=_GENERIC_WEB_PREVIEW_DESIRED_STATE_ROUTE.route_path,
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
        driver_result = discover_generic_web_preview_desired_state(
            control_plane_root=resolved_control_plane_root,
            record_store=cast(GenericWebPreviewProfileStore, record_store),
            request=desired_state_request.desired_state,
            discovered_at=utc_now_timestamp(),
            profile=profile,
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
                route_path=_GENERIC_WEB_PREVIEW_DESIRED_STATE_ROUTE.route_path,
                idempotency_key=normalized_key,
                request_fingerprint_value=payload_fingerprint,
                trace_id=trace_id,
                response=response,
            )
        return response

    async def apply_generic_web_preview_inventory(
        inventory_request: GenericWebPreviewInventoryEnvelope,
        identity: Annotated[LaunchplaneIdentity, Depends(read_write_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
    ) -> AcceptedEvidenceResponse | JSONResponse:
        trace_id = next_trace_id()
        try:
            profile = resolve_generic_web_preview_profile(
                record_store=record_store,
                product=inventory_request.product,
            )
        except GenericWebPreviewRouteDependencyError:
            return driver_route_dependency_not_found_response(
                trace_id=trace_id,
                route_path=_GENERIC_WEB_PREVIEW_INVENTORY_ROUTE,
            )
        except GenericWebPreviewProductMismatchError as error:
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
        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action=GENERIC_WEB_PREVIEW_INVENTORY_ACTION,
            product=profile.product,
            context=profile.preview.context,
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message=(
                    "Workflow cannot read generic web preview inventory"
                    " for the requested product/context."
                ),
            )
        try:
            records, result = apply_generic_web_preview_inventory_result(
                control_plane_root=resolved_control_plane_root,
                record_store=record_store,
                request=inventory_request,
                profile=profile,
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
            records=records,
            result=result,
        )

    async def apply_generic_web_preview_readiness(
        readiness_request: GenericWebPreviewReadinessEnvelope,
        identity: Annotated[LaunchplaneIdentity, Depends(read_write_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
    ) -> AcceptedEvidenceResponse | JSONResponse:
        trace_id = next_trace_id()
        try:
            profile = resolve_generic_web_preview_profile(
                record_store=record_store,
                product=readiness_request.product,
            )
        except GenericWebPreviewRouteDependencyError:
            return driver_route_dependency_not_found_response(
                trace_id=trace_id,
                route_path=_GENERIC_WEB_PREVIEW_READINESS_ROUTE,
            )
        except GenericWebPreviewProductMismatchError as error:
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
        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action=GENERIC_WEB_PREVIEW_READINESS_ACTION,
            product=profile.product,
            context=profile.preview.context,
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message=(
                    "Workflow cannot evaluate generic web preview readiness"
                    " for the requested product/context."
                ),
            )
        try:
            records, result = apply_generic_web_preview_readiness_result(
                control_plane_root=resolved_control_plane_root,
                record_store=record_store,
                request=readiness_request,
                profile=profile,
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
            records=records,
            result=result,
        )

    async def apply_generic_web_preview_refresh(
        request: Request,
        refresh_request: GenericWebPreviewRefreshEnvelope,
        identity: Annotated[LaunchplaneIdentity, Depends(read_write_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> AcceptedEvidenceResponse | JSONResponse:
        trace_id = next_trace_id()
        try:
            profile = resolve_generic_web_preview_profile(
                record_store=record_store,
                product=refresh_request.product,
            )
        except GenericWebPreviewRouteDependencyError:
            return driver_route_dependency_not_found_response(
                trace_id=trace_id,
                route_path=_GENERIC_WEB_PREVIEW_REFRESH_ROUTE,
            )
        except GenericWebPreviewProductMismatchError as error:
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
        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action=GENERIC_WEB_PREVIEW_REFRESH_ACTION,
            product=profile.product,
            context=profile.preview.context,
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message=(
                    "Workflow cannot refresh generic web preview state"
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
            route_path=_GENERIC_WEB_PREVIEW_REFRESH_ROUTE,
            idempotency_key=idempotency_key,
            trace_id=trace_id,
            check_replay=bool(idempotency_key.strip()),
        )
        if replayed_response is not None:
            return replayed_response
        try:
            records, result = apply_generic_web_preview_refresh_result(
                control_plane_root=resolved_control_plane_root,
                record_store=record_store,
                request=refresh_request,
                profile=profile,
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
            records=records,
            result=result,
        )
        if should_store_generic_web_preview_idempotency(result):
            store_apply_idempotency(
                record_store=record_store,
                identity=identity,
                route_path=_GENERIC_WEB_PREVIEW_REFRESH_ROUTE,
                idempotency_key=normalized_key,
                request_fingerprint_value=payload_fingerprint,
                trace_id=trace_id,
                response=response,
            )
        return response

    async def apply_generic_web_preview_destroy(
        request: Request,
        destroy_request: GenericWebPreviewDestroyEnvelope,
        identity: Annotated[LaunchplaneIdentity, Depends(read_write_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> AcceptedEvidenceResponse | JSONResponse:
        trace_id = next_trace_id()
        try:
            profile = resolve_generic_web_preview_profile(
                record_store=record_store,
                product=destroy_request.product,
            )
        except GenericWebPreviewRouteDependencyError:
            return driver_route_dependency_not_found_response(
                trace_id=trace_id,
                route_path=_GENERIC_WEB_PREVIEW_DESTROY_ROUTE,
            )
        except GenericWebPreviewProductMismatchError as error:
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
        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action=GENERIC_WEB_PREVIEW_DESTROY_ACTION,
            product=profile.product,
            context=profile.preview.context,
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message=(
                    "Workflow cannot destroy generic web preview state"
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
            route_path=_GENERIC_WEB_PREVIEW_DESTROY_ROUTE,
            idempotency_key=idempotency_key,
            trace_id=trace_id,
            check_replay=bool(idempotency_key.strip()),
        )
        if replayed_response is not None:
            return replayed_response
        try:
            records, result = apply_generic_web_preview_destroy_result(
                control_plane_root=resolved_control_plane_root,
                record_store=record_store,
                request=destroy_request,
                profile=profile,
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
            records=records,
            result=result,
        )
        if should_store_generic_web_preview_idempotency(result):
            store_apply_idempotency(
                record_store=record_store,
                identity=identity,
                route_path=_GENERIC_WEB_PREVIEW_DESTROY_ROUTE,
                idempotency_key=normalized_key,
                request_fingerprint_value=payload_fingerprint,
                trace_id=trace_id,
                response=response,
            )
        return response

    async def apply_generic_web_deploy(
        request: Request,
        deploy_request: GenericWebDeployEnvelope,
        identity: Annotated[LaunchplaneIdentity, Depends(read_write_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> AcceptedEvidenceResponse | JSONResponse:
        trace_id = next_trace_id()
        try:
            profile, lane = resolve_generic_web_deploy_lane(
                record_store=record_store,
                product=deploy_request.product,
                instance=deploy_request.deploy.instance,
            )
        except GenericWebDeployRouteDependencyError:
            return driver_route_dependency_not_found_response(
                trace_id=trace_id,
                route_path=_GENERIC_WEB_DEPLOY_ROUTE,
            )
        except GenericWebDeployProductMismatchError as error:
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
        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action=GENERIC_WEB_DEPLOY_ACTION,
            product=profile.product,
            context=lane.context,
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message=(
                    "Workflow cannot execute the generic web deploy driver"
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
            route_path=_GENERIC_WEB_DEPLOY_ROUTE,
            idempotency_key=idempotency_key,
            trace_id=trace_id,
            check_replay=bool(idempotency_key.strip()),
        )
        if replayed_response is not None:
            return replayed_response
        try:
            records, result = execute_generic_web_deploy_result(
                control_plane_root=resolved_control_plane_root,
                record_store=record_store,
                request=deploy_request,
                profile=profile,
                lane=lane,
            )
        except FileNotFoundError as error:
            raise _launchplane_http_error(
                status_code=404,
                trace_id=trace_id,
                code="not_found",
                message=f"No Launchplane route for {_GENERIC_WEB_DEPLOY_ROUTE}.",
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
        if should_store_generic_web_deploy_idempotency(result):
            store_apply_idempotency(
                record_store=record_store,
                identity=identity,
                route_path=_GENERIC_WEB_DEPLOY_ROUTE,
                idempotency_key=normalized_key,
                request_fingerprint_value=payload_fingerprint,
                trace_id=trace_id,
                response=response,
            )
        return response

    async def apply_generic_web_prod_promotion(
        request: Request,
        promotion_request: GenericWebProdPromotionEnvelope,
        identity: Annotated[LaunchplaneIdentity, Depends(read_browser_mutation_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> GenericWebProdPromotionResponse | JSONResponse:
        trace_id = next_trace_id()
        if isinstance(identity, TerminalAgentIdentity):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Terminal agent credentials can only read redacted Launchplane context.",
            )
        try:
            profile, lane = resolve_generic_web_promotion_destination_lane(
                record_store=record_store,
                product=promotion_request.product,
                instance=promotion_request.promotion.to_instance,
            )
            validate_generic_web_prod_promotion_lanes(
                record_store=record_store,
                request=promotion_request,
            )
        except GenericWebPromotionRouteDependencyError:
            return driver_route_dependency_not_found_response(
                trace_id=trace_id,
                route_path=_GENERIC_WEB_PROD_PROMOTION_ROUTE,
            )
        except GenericWebPromotionProductMismatchError as error:
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
        if isinstance(identity, GitHubHumanIdentity) and not promotion_request.promotion.dry_run:
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Launchplane UI can only dry-run generic-web prod promotions.",
            )
        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action=GENERIC_WEB_PROD_PROMOTION_ACTION,
            product=profile.product,
            context=lane.context.strip(),
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message=(
                    "Workflow cannot execute the generic web prod promotion driver"
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
            route_path=_GENERIC_WEB_PROD_PROMOTION_ROUTE,
            idempotency_key=idempotency_key,
            trace_id=trace_id,
            check_replay=bool(idempotency_key.strip()),
        )
        if replayed_response is not None:
            return GenericWebProdPromotionResponse.model_validate(
                replayed_response.model_dump(mode="json")
            )
        try:
            records, result = execute_generic_web_prod_promotion_result(
                control_plane_root=resolved_control_plane_root,
                record_store=record_store,
                request=promotion_request,
            )
        except FileNotFoundError as error:
            raise _launchplane_http_error(
                status_code=404,
                trace_id=trace_id,
                code="not_found",
                message=f"No Launchplane route for {_GENERIC_WEB_PROD_PROMOTION_ROUTE}.",
            ) from error
        except (ValueError, click.ClickException) as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request could not be completed.",
            ) from error
        response = GenericWebProdPromotionResponse(
            trace_id=trace_id,
            records=records,
            result=GenericWebProdPromotionResponseResult.model_validate(
                result.model_dump(mode="json")
            ),
        )
        if should_store_generic_web_promotion_idempotency(result):
            store_apply_idempotency(
                record_store=record_store,
                identity=identity,
                route_path=_GENERIC_WEB_PROD_PROMOTION_ROUTE,
                idempotency_key=normalized_key,
                request_fingerprint_value=payload_fingerprint,
                trace_id=trace_id,
                response=response,
            )
        return response

    async def dispatch_generic_web_prod_promotion_workflow(
        request: Request,
        workflow_request: GenericWebPromotionWorkflowEnvelope,
        identity: Annotated[LaunchplaneIdentity, Depends(read_browser_mutation_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> GenericWebPromotionWorkflowResponse | JSONResponse:
        trace_id = next_trace_id()
        if isinstance(identity, TerminalAgentIdentity):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Terminal agent credentials can only read redacted Launchplane context.",
            )
        try:
            profile, lane = resolve_generic_web_promotion_workflow_lane(
                record_store=record_store,
                product=workflow_request.product,
                context=workflow_request.workflow.context,
            )
        except GenericWebPromotionRouteDependencyError:
            return driver_route_dependency_not_found_response(
                trace_id=trace_id,
                route_path=_GENERIC_WEB_PROD_PROMOTION_WORKFLOW_ROUTE,
            )
        except GenericWebPromotionProductMismatchError as error:
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
        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action=GENERIC_WEB_PROD_PROMOTION_WORKFLOW_ACTION,
            product=profile.product,
            context=lane.context.strip(),
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message=(
                    "Workflow cannot dispatch the generic web prod promotion workflow"
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
            route_path=_GENERIC_WEB_PROD_PROMOTION_WORKFLOW_ROUTE,
            idempotency_key=idempotency_key,
            trace_id=trace_id,
            check_replay=bool(idempotency_key.strip()),
        )
        if replayed_response is not None:
            return GenericWebPromotionWorkflowResponse.model_validate(
                replayed_response.model_dump(mode="json")
            )
        try:
            records, result, outbox_delivery = dispatch_generic_web_promotion_workflow_result(
                control_plane_root=resolved_control_plane_root,
                request=workflow_request,
                profile=profile,
            )
        except FileNotFoundError as error:
            raise _launchplane_http_error(
                status_code=404,
                trace_id=trace_id,
                code="not_found",
                message=(f"No Launchplane route for {_GENERIC_WEB_PROD_PROMOTION_WORKFLOW_ROUTE}."),
            ) from error
        except (ValueError, click.ClickException) as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request could not be completed.",
            ) from error
        response = GenericWebPromotionWorkflowResponse(
            trace_id=trace_id,
            records=records,
            result=GenericWebPromotionWorkflowResponseResult.model_validate(
                result.model_dump(mode="json")
            ),
        )
        if not isinstance(record_store, PostgresRecordStore):
            raise _launchplane_http_error(
                status_code=409,
                trace_id=trace_id,
                code="outbox_requires_postgres",
                message="Workflow dispatch requires PostgreSQL transactional outbox storage.",
            )
        idempotency_record = None
        if normalized_key and should_store_generic_web_promotion_idempotency(result):
            idempotency_record = build_apply_idempotency_record(
                identity=identity,
                route_path=_GENERIC_WEB_PROD_PROMOTION_WORKFLOW_ROUTE,
                idempotency_key=normalized_key,
                request_fingerprint_value=payload_fingerprint,
                trace_id=trace_id,
                response=response,
            )
        record_store.enqueue_outbox_delivery_with_idempotency(
            OutboxWithIdempotencyRequest(
                delivery=outbox_delivery,
                idempotency_record=idempotency_record,
            )
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
    ) -> None:
        raise _launchplane_http_error(
            status_code=403,
            trace_id=trace_id,
            code="product_driver_mismatch",
            message="Product is not configured for the requested driver route.",
        ) from error

    def raise_verireel_invalid_request_error(
        *, trace_id: str, error: ValueError | click.ClickException
    ) -> None:
        raise _launchplane_http_error(
            status_code=400,
            trace_id=trace_id,
            code="invalid_request",
            message="Request could not be completed.",
        ) from error

    def raise_verireel_unexpected_driver_error(*, trace_id: str, error: Exception) -> None:
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
        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action=VERIREEL_PROD_DEPLOY_ACTION,
            product=authorization_product,
            context=authorization_context,
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
        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action=VERIREEL_PROD_BACKUP_GATE_ACTION,
            product=authorization_product,
            context=authorization_context,
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
        try:
            records, result = apply_verireel_prod_backup_gate_result(
                record_store=record_store,
                request=backup_gate_request,
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
        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action=VERIREEL_PROD_PROMOTION_ACTION,
            product=authorization_product,
            context=authorization_context,
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
        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action=VERIREEL_PROD_ROLLBACK_ACTION,
            product=authorization_product,
            context=authorization_context,
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
        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action=VERIREEL_TESTING_DEPLOY_ACTION,
            product=authorization_product,
            context=authorization_context,
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
        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action=VERIREEL_APP_MAINTENANCE_ACTION,
            product=authorization_product,
            context=authorization_context,
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
        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action=VERIREEL_TESTING_VERIFICATION_ACTION,
            product=authorization_product,
            context=authorization_context,
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
        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action=VERIREEL_STABLE_ENVIRONMENT_ACTION,
            product=authorization_product,
            context=authorization_context,
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
        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action=VERIREEL_RUNTIME_VERIFICATION_ACTION,
            product=authorization_product,
            context=authorization_context,
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
        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action=VERIREEL_PREVIEW_INVENTORY_ACTION,
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
        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action=VERIREEL_PREVIEW_REFRESH_ACTION,
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
        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action=VERIREEL_PREVIEW_DESTROY_ACTION,
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
        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action=VERIREEL_PREVIEW_VERIFICATION_ACTION,
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

    async def apply_generic_web_stable_verification(
        request: Request,
        verification_request: GenericWebStableVerificationEnvelope,
        identity: Annotated[LaunchplaneIdentity, Depends(read_write_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> AcceptedEvidenceResponse | JSONResponse:
        trace_id = next_trace_id()
        try:
            profile, lane = resolve_generic_web_stable_verification_lane(
                record_store=record_store,
                product=verification_request.product,
                context=verification_request.verification.context,
                instance=verification_request.verification.instance,
            )
        except GenericWebVerificationRouteDependencyError:
            return driver_route_dependency_not_found_response(
                trace_id=trace_id,
                route_path=_GENERIC_WEB_STABLE_VERIFICATION_ROUTE,
            )
        except GenericWebVerificationProductMismatchError as error:
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
        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action=GENERIC_WEB_STABLE_VERIFICATION_ACTION,
            product=profile.product,
            context=lane.context,
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message=(
                    "Workflow cannot write generic web stable verification"
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
            route_path=_GENERIC_WEB_STABLE_VERIFICATION_ROUTE,
            idempotency_key=idempotency_key,
            trace_id=trace_id,
            check_replay=bool(idempotency_key.strip()),
        )
        if replayed_response is not None:
            return replayed_response
        try:
            result = apply_generic_web_stable_verification_result(
                record_store=record_store,
                request=verification_request,
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
            records=generic_web_verification_response_records(result),
            result=result,
        )
        if should_store_generic_web_verification_idempotency(result):
            store_apply_idempotency(
                record_store=record_store,
                identity=identity,
                route_path=_GENERIC_WEB_STABLE_VERIFICATION_ROUTE,
                idempotency_key=normalized_key,
                request_fingerprint_value=payload_fingerprint,
                trace_id=trace_id,
                response=response,
            )
        return response

    async def apply_generic_web_preview_verification(
        request: Request,
        verification_request: GenericWebPreviewVerificationEnvelope,
        identity: Annotated[LaunchplaneIdentity, Depends(read_write_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> AcceptedEvidenceResponse | JSONResponse:
        trace_id = next_trace_id()
        try:
            profile = resolve_generic_web_preview_verification_profile(
                record_store=record_store,
                product=verification_request.product,
                context=verification_request.verification.context,
            )
        except GenericWebVerificationRouteDependencyError:
            return driver_route_dependency_not_found_response(
                trace_id=trace_id,
                route_path=_GENERIC_WEB_PREVIEW_VERIFICATION_ROUTE,
            )
        except GenericWebVerificationProductMismatchError as error:
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
        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action=GENERIC_WEB_PREVIEW_VERIFICATION_ACTION,
            product=profile.product,
            context=profile.preview.context,
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message=(
                    "Workflow cannot write generic web preview verification"
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
            route_path=_GENERIC_WEB_PREVIEW_VERIFICATION_ROUTE,
            idempotency_key=idempotency_key,
            trace_id=trace_id,
            check_replay=bool(idempotency_key.strip()),
        )
        if replayed_response is not None:
            return replayed_response
        verification_request.verification.context = profile.preview.context
        try:
            result = apply_generic_web_preview_verification_result(
                control_plane_root=resolved_control_plane_root,
                record_store=record_store,
                request=verification_request,
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
            records=generic_web_verification_response_records(result),
            result=result,
        )
        if should_store_generic_web_verification_idempotency(result):
            store_apply_idempotency(
                record_store=record_store,
                identity=identity,
                route_path=_GENERIC_WEB_PREVIEW_VERIFICATION_ROUTE,
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

    app.add_api_route(
        _GENERIC_WEB_PREVIEW_DESIRED_STATE_ROUTE.route_path,
        apply_generic_web_preview_desired_state,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        operation_id="apply_generic_web_preview_desired_state",
        summary="Discover generic web preview desired state",
        responses={
            400: {"model": LaunchplaneErrorResponse},
            401: {"model": LaunchplaneErrorResponse},
            403: {"model": LaunchplaneErrorResponse},
            409: {"model": LaunchplaneErrorResponse},
            503: {"model": LaunchplaneErrorResponse},
        },
    )

    app.add_api_route(
        _GENERIC_WEB_PREVIEW_INVENTORY_ROUTE,
        apply_generic_web_preview_inventory,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": _openapi_model_schema(GenericWebPreviewInventoryEnvelope)
                    }
                },
            }
        },
        operation_id="apply_generic_web_preview_inventory",
        summary="Read generic web preview inventory",
        responses={
            400: {"model": LaunchplaneErrorResponse},
            401: {"model": LaunchplaneErrorResponse},
            403: {"model": LaunchplaneErrorResponse},
            503: {"model": LaunchplaneErrorResponse},
        },
    )

    app.add_api_route(
        _GENERIC_WEB_PREVIEW_READINESS_ROUTE,
        apply_generic_web_preview_readiness,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": _openapi_model_schema(GenericWebPreviewReadinessEnvelope)
                    }
                },
            }
        },
        operation_id="apply_generic_web_preview_readiness",
        summary="Evaluate generic web preview readiness",
        responses={
            400: {"model": LaunchplaneErrorResponse},
            401: {"model": LaunchplaneErrorResponse},
            403: {"model": LaunchplaneErrorResponse},
            503: {"model": LaunchplaneErrorResponse},
        },
    )

    app.add_api_route(
        _GENERIC_WEB_PREVIEW_REFRESH_ROUTE,
        apply_generic_web_preview_refresh,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": _openapi_model_schema(GenericWebPreviewRefreshEnvelope)
                    }
                },
            }
        },
        operation_id="apply_generic_web_preview_refresh",
        summary="Refresh generic web preview",
        responses={
            400: {"model": LaunchplaneErrorResponse},
            401: {"model": LaunchplaneErrorResponse},
            403: {"model": LaunchplaneErrorResponse},
            409: {"model": LaunchplaneErrorResponse},
            503: {"model": LaunchplaneErrorResponse},
        },
    )

    app.add_api_route(
        _GENERIC_WEB_PREVIEW_DESTROY_ROUTE,
        apply_generic_web_preview_destroy,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": _openapi_model_schema(GenericWebPreviewDestroyEnvelope)
                    }
                },
            }
        },
        operation_id="apply_generic_web_preview_destroy",
        summary="Destroy generic web preview",
        responses={
            400: {"model": LaunchplaneErrorResponse},
            401: {"model": LaunchplaneErrorResponse},
            403: {"model": LaunchplaneErrorResponse},
            409: {"model": LaunchplaneErrorResponse},
            503: {"model": LaunchplaneErrorResponse},
        },
    )

    app.add_api_route(
        _GENERIC_WEB_DEPLOY_ROUTE,
        apply_generic_web_deploy,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {"schema": _openapi_model_schema(GenericWebDeployEnvelope)}
                },
            }
        },
        operation_id="apply_generic_web_deploy",
        summary="Execute generic web deploy",
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
        _GENERIC_WEB_PROD_PROMOTION_ROUTE,
        apply_generic_web_prod_promotion,
        methods=["POST"],
        status_code=202,
        response_model=GenericWebProdPromotionResponse,
        response_model_exclude_none=True,
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": _openapi_model_schema(GenericWebProdPromotionEnvelope)
                    }
                },
            }
        },
        operation_id="apply_generic_web_prod_promotion",
        summary="Execute generic web prod promotion",
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
        _GENERIC_WEB_PROD_PROMOTION_WORKFLOW_ROUTE,
        dispatch_generic_web_prod_promotion_workflow,
        methods=["POST"],
        status_code=202,
        response_model=GenericWebPromotionWorkflowResponse,
        response_model_exclude_none=True,
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": _openapi_model_schema(GenericWebPromotionWorkflowEnvelope)
                    }
                },
            }
        },
        operation_id="dispatch_generic_web_prod_promotion_workflow",
        summary="Dispatch generic web prod promotion workflow",
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
        _GENERIC_WEB_STABLE_VERIFICATION_ROUTE,
        apply_generic_web_stable_verification,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": _openapi_model_schema(GenericWebStableVerificationEnvelope)
                    }
                },
            }
        },
        operation_id="apply_generic_web_stable_verification",
        summary="Write generic web stable verification",
        responses={
            400: {"model": LaunchplaneErrorResponse},
            401: {"model": LaunchplaneErrorResponse},
            403: {"model": LaunchplaneErrorResponse},
            409: {"model": LaunchplaneErrorResponse},
            503: {"model": LaunchplaneErrorResponse},
        },
    )

    app.add_api_route(
        _GENERIC_WEB_PREVIEW_VERIFICATION_ROUTE,
        apply_generic_web_preview_verification,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": _openapi_model_schema(GenericWebPreviewVerificationEnvelope)
                    }
                },
            }
        },
        operation_id="apply_generic_web_preview_verification",
        summary="Write generic web preview verification",
        responses={
            400: {"model": LaunchplaneErrorResponse},
            401: {"model": LaunchplaneErrorResponse},
            403: {"model": LaunchplaneErrorResponse},
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
        _GENERIC_WEB_ROLLBACK_PLAN_ROUTE,
        write_generic_web_rollback_plan,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": _openapi_model_schema(GenericWebRollbackPlanEnvelope)
                    }
                },
            }
        },
        operation_id="write_generic_web_rollback_plan",
        summary="Plan generic web prod rollback",
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
        _GENERIC_WEB_ROLLBACK_ROUTE,
        write_generic_web_rollback,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": _openapi_model_schema(GenericWebRollbackEnvelope)
                    }
                },
            }
        },
        operation_id="write_generic_web_rollback",
        summary="Execute generic web prod rollback",
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
        _ROUTE_BINDING_BACKFILL_APPLY_ROUTE,
        apply_route_binding_backfill,
        methods=["POST"],
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        status_code=202,
        operation_id="apply_route_binding_backfill",
        summary="Plan or apply one provider-neutral environment route binding",
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
        "/v1/route-bindings/records",
        list_route_binding_records,
        methods=["GET"],
        response_model=RouteBindingRecordsResponse,
        operation_id="list_route_binding_records",
        summary="List provider-neutral environment route bindings",
        responses={
            400: {"model": LaunchplaneErrorResponse},
            401: {"model": LaunchplaneErrorResponse},
            403: {"model": LaunchplaneErrorResponse},
            503: {"model": LaunchplaneErrorResponse},
        },
    )

    app.add_api_route(
        "/v1/route-bindings/records/current",
        read_route_binding_record,
        methods=["GET"],
        response_model=RouteBindingRecordResponse,
        operation_id="read_route_binding_record",
        summary="Read one provider-neutral environment route binding",
        responses={
            400: {"model": LaunchplaneErrorResponse},
            401: {"model": LaunchplaneErrorResponse},
            403: {"model": LaunchplaneErrorResponse},
            404: {"model": LaunchplaneErrorResponse},
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
    evidence_ingress_error_responses: dict[int | str, dict[str, object]] = {
        400: {"model": LaunchplaneErrorResponse},
        401: {"model": LaunchplaneErrorResponse},
        403: {"model": LaunchplaneErrorResponse},
        409: {"model": LaunchplaneErrorResponse},
        413: {"model": LaunchplaneErrorResponse},
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
        response_model=WorkGraphRankResponse,
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
        response_model=WorkGraphIssueInboxReconcileResponse,
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

    app.add_api_route(
        _AUTHZ_POLICY_GITHUB_ACTIONS_GRANTS_ROUTE,
        grant_github_actions_authz_policy,
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
                            control_plane_authz_grant_service.AuthzPolicyGitHubActionsGrantEnvelope
                        )
                    }
                },
            }
        },
        operation_id="grant_github_actions_authz_policy",
        summary="Grant GitHub Actions authz policy rules",
        responses=authz_policy_route_responses,
    )

    app.add_api_route(
        _AUTHZ_POLICY_GITHUB_ACTIONS_REMOVALS_ROUTE,
        remove_github_actions_authz_policy,
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
                            control_plane_authz_grant_service.AuthzPolicyGitHubActionsRemovalEnvelope
                        )
                    }
                },
            }
        },
        operation_id="remove_github_actions_authz_policy",
        summary="Remove GitHub Actions authz policy rules",
        responses=authz_policy_route_responses,
    )

    app.add_api_route(
        _AUTHZ_POLICY_GITHUB_HUMANS_GRANTS_ROUTE,
        grant_github_human_authz_policy,
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
                            control_plane_authz_grant_service.AuthzPolicyGitHubHumanGrantEnvelope
                        )
                    }
                },
            }
        },
        operation_id="grant_github_human_authz_policy",
        summary="Grant GitHub human authz policy rules",
        responses=authz_policy_route_responses,
    )

    app.add_api_route(
        _AUTHZ_POLICY_TERMINAL_AGENTS_GRANTS_ROUTE,
        grant_terminal_agent_authz_policy,
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
                            control_plane_authz_grant_service.AuthzPolicyTerminalAgentGrantEnvelope
                        )
                    }
                },
            }
        },
        operation_id="grant_terminal_agent_authz_policy",
        summary="Grant terminal-agent authz policy rules",
        responses=authz_policy_route_responses,
    )

    app.add_api_route(
        _AUTHZ_POLICY_LOCAL_OPERATORS_GRANTS_ROUTE,
        grant_local_operator_authz_policy,
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
                            control_plane_authz_grant_service.AuthzPolicyLocalOperatorGrantEnvelope
                        )
                    }
                },
            }
        },
        operation_id="grant_local_operator_authz_policy",
        summary="Grant local-operator authz policy rules",
        responses=authz_policy_route_responses,
    )

    app.add_api_route(
        _AUTHZ_POLICY_LOCAL_ADMINS_GRANTS_ROUTE,
        grant_local_admin_authz_policy,
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
                            control_plane_authz_grant_service.AuthzPolicyLocalAdminGrantEnvelope
                        )
                    }
                },
            }
        },
        operation_id="grant_local_admin_authz_policy",
        summary="Grant local-admin authz policy rules",
        responses=authz_policy_route_responses,
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
                        "schema": _openapi_model_schema(
                            control_plane_product_context_cutover.ProductContextCutoverRequest
                        )
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
                        "schema": _openapi_model_schema(
                            control_plane_product_context_cutover.LegacyContextCleanupRequest
                        )
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
        responses=evidence_ingress_error_responses,
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
        responses=evidence_ingress_error_responses,
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
        responses=evidence_ingress_error_responses,
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
        responses=evidence_ingress_error_responses,
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
        responses=evidence_ingress_error_responses,
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
        responses=evidence_ingress_error_responses,
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
        responses=evidence_ingress_error_responses,
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
            records = detail.get("records")
            authz = detail.get("authz")
        else:
            message = str(http_error.detail)
            records = None
            authz = None
        payload = LaunchplaneErrorResponse(
            trace_id=trace_id,
            error=LaunchplaneErrorDetail(code=code, message=message),
            records=records if isinstance(records, dict) else None,
            authz=authz if isinstance(authz, dict) else None,
        )
        response = JSONResponse(
            status_code=http_error.status_code,
            content=payload.model_dump(mode="json", exclude_none=True),
            headers=http_error.headers,
        )
        preserve_renewed_session_cookie(request, response)
        return response

    def launchplane_starlette_http_exception_handler(
        request: Request, error: Exception
    ) -> JSONResponse:
        if not isinstance(error, StarletteHTTPException):
            raise error
        trace_id = next_trace_id()
        if error.status_code == 404:
            status_code = 404
            code = "not_found"
            message = f"No Launchplane route for {request.url.path}."
        elif error.status_code == 405:
            status_code = 405
            code = "method_not_allowed"
            message = "Only GET and POST are allowed for Launchplane routes."
        else:
            status_code = error.status_code
            code = "http_error"
            message = str(error.detail)
        payload = LaunchplaneErrorResponse(
            trace_id=trace_id,
            error=LaunchplaneErrorDetail(code=code, message=message),
        )
        response = JSONResponse(
            status_code=status_code,
            content=payload.model_dump(mode="json", exclude_none=True),
            headers=error.headers,
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
        )
        preserve_renewed_session_cookie(request, response)
        return response

    app.add_api_route(
        "/v1/products/{product}/environments/{environment}/config-status",
        read_product_environment_config_status,
        methods=["GET"],
        operation_id="read_product_environment_config_status",
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
        StarletteHTTPException,
        launchplane_starlette_http_exception_handler,
    )
    app.add_exception_handler(
        RequestValidationError,
        launchplane_request_validation_exception_handler,
    )

    return app


def _launchplane_http_error(
    *,
    status_code: int,
    trace_id: str,
    code: str,
    message: str,
    authz: dict[str, object] | None = None,
) -> HTTPException:
    detail: dict[str, object] = {"trace_id": trace_id, "code": code, "message": message}
    if authz is not None:
        detail["authz"] = authz
    return HTTPException(
        status_code=status_code,
        detail=detail,
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


def _authentication_required_error(message: str) -> HTTPException:
    return HTTPException(
        status_code=401,
        detail={"code": "authentication_required", "message": message},
        headers=_BEARER_CHALLENGE_HEADER,
    )
