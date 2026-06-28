from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
import os
import re
import uuid
from pathlib import Path
from typing import Iterable, Protocol, cast

import click
from pydantic import BaseModel

from control_plane.http_app import (
    LaunchplaneAuthzPolicyRuntime,
    create_launchplane_fastapi_app,
    resolve_launchplane_authz_policy,
)
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
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductLaneProfile,
)
from control_plane.drivers.registry import list_driver_descriptors, read_driver_descriptor
from control_plane.every_code_work_request_write import (
    EveryCodeWorkRequestCreateEnvelope,
    build_every_code_work_request_record,
)
from control_plane.drivers.dispatch import (
    _DriverRouteEnvelopeT as _DriverRouteEnvelopeT,
    _DriverRouteExecutionMetadata as _DriverRouteExecutionMetadata,
    _ProductRouteEnvelope as _ProductRouteEnvelope,
    _ResolvedProductDriverContext as _ResolvedProductDriverContext,
    _image_reference_tail as _image_reference_tail,
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
    _stable_verification_health_evidence as _stable_verification_health_evidence,
    _validate_stable_verification_request as _validate_stable_verification_request,
)
from control_plane.drivers.generic_web_preview_dispatch import (
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
    _write_preview_desired_state_if_supported as _write_preview_desired_state_if_supported,
    _write_preview_inventory_scan_if_supported as _write_preview_inventory_scan_if_supported,
)
from control_plane.generic_web_preview_http import (
    GenericWebPreviewDestroyEnvelope as GenericWebPreviewDestroyEnvelope,
    GenericWebPreviewInventoryEnvelope as GenericWebPreviewInventoryEnvelope,
    GenericWebPreviewReadinessEnvelope as GenericWebPreviewReadinessEnvelope,
    GenericWebPreviewRefreshEnvelope as GenericWebPreviewRefreshEnvelope,
)
from control_plane.odoo_preview_apply_http import (
    ODOO_PREVIEW_APPLY_INPUTS_ROUTE,
    ODOO_PREVIEW_APPLY_ROUTE,
    OdooPreviewApplyEnvelope,
    OdooPreviewApplyInputsEnvelope,
)
from control_plane.launchplane_mutations import (
    control_plane_root,
)
from control_plane.service_auth import (
    AgentAuthzDecision,
    BearerIdentityConfig,
    GitHubHumanIdentity,
    LaunchplaneAuthzPolicy,
    LaunchplaneIdentity,
    LocalAdminIdentity,
    LocalOperatorIdentity,
    TerminalAgentIdentity,
    agent_authz_audit,
    load_authz_policy,
)
from control_plane.service_human_auth import (
    GitHubOAuthClient,
    HumanSessionManager,
    OAuthLoginStateStore,
    load_github_oauth_config_from_env,
)
from control_plane.storage.factory import build_shared_record_store
from control_plane.work_graph_github_projects import (
    build_github_project_planning_facts,
    load_github_project_planning_facts_config_from_env,
)
from control_plane.work_graph_issue_inbox import (
    build_github_issue_inbox_read_model,
    load_github_issue_inbox_config_from_env,
    reconcile_github_issue_inbox,
)
from control_plane.workflows.preview_pr_feedback import (
    handle_every_code_preview_validation_comment,
)
from control_plane.workflows.launchplane import (
    ProductProfileListStore,
    launchplane_anchor_repo_context,
    resolve_launchplane_github_token,
    verify_github_webhook_signature,
)
from control_plane.odoo_artifact_publish_http import (
    ODOO_ARTIFACT_PUBLISH_ROUTE,
    OdooArtifactPublishEnvelope as OdooArtifactPublishEnvelope,
)
from control_plane.odoo_post_deploy_http import (
    ODOO_CONFIG_PARAMETER_OVERRIDE_ROUTE,
    ODOO_POST_DEPLOY_ROUTE,
    ODOO_WEBSITE_BOOTSTRAP_OVERRIDE_ROUTE,
    OdooConfigParameterOverrideEnvelope as OdooConfigParameterOverrideEnvelope,
    OdooPostDeployEnvelope as OdooPostDeployEnvelope,
    OdooWebsiteBootstrapOverrideEnvelope as OdooWebsiteBootstrapOverrideEnvelope,
)
from control_plane.odoo_prod_backup_gate_http import (
    ODOO_PROD_BACKUP_GATE_ROUTE,
    OdooProdBackupGateEnvelope as OdooProdBackupGateEnvelope,
)
from control_plane.odoo_prod_promotion_http import (
    ODOO_PROD_PROMOTION_INPUTS_ROUTE,
    ODOO_PROD_PROMOTION_ROUTE,
    ODOO_PROD_PROMOTION_RUN_ROUTE,
    OdooProdPromotionEnvelope as OdooProdPromotionEnvelope,
    OdooProdPromotionInputsEnvelope as OdooProdPromotionInputsEnvelope,
    OdooProdPromotionRunEnvelope as OdooProdPromotionRunEnvelope,
)
from control_plane.odoo_prod_rollback_http import (
    ODOO_PROD_ROLLBACK_ROUTE,
    OdooProdRollbackEnvelope as OdooProdRollbackEnvelope,
)
from control_plane.odoo_stable_bootstrap_http import (
    ODOO_STABLE_BOOTSTRAP_ROUTE,
    OdooStableBootstrapEnvelope as OdooStableBootstrapEnvelope,
)
from control_plane.odoo_target_replacement_plan_http import (
    ODOO_TARGET_REPLACEMENT_PLAN_ROUTE,
    OdooTargetReplacementPlanEnvelope as OdooTargetReplacementPlanEnvelope,
)
from control_plane.odoo_target_replacement_apply_http import (
    ODOO_TARGET_REPLACEMENT_APPLY_ROUTE,
    OdooTargetReplacementApplyEnvelope as OdooTargetReplacementApplyEnvelope,
)
from control_plane.verireel_read_http import (
    VeriReelPreviewDestroyEnvelope as VeriReelPreviewDestroyEnvelope,
    VeriReelPreviewInventoryEnvelope as VeriReelPreviewInventoryEnvelope,
    VeriReelPreviewRefreshEnvelope as VeriReelPreviewRefreshEnvelope,
    VeriReelPreviewVerificationEnvelope as VeriReelPreviewVerificationEnvelope,
    VeriReelPreviewVerificationRequest as VeriReelPreviewVerificationRequest,
    VeriReelRuntimeVerificationEnvelope as VeriReelRuntimeVerificationEnvelope,
    VeriReelStableEnvironmentEnvelope as VeriReelStableEnvironmentEnvelope,
    VeriReelTestingVerificationEnvelope as VeriReelTestingVerificationEnvelope,
    VeriReelTestingVerificationRequest as VeriReelTestingVerificationRequest,
    _VERIREEL_PREVIEW_DESTROY_ROUTE as _VERIREEL_PREVIEW_DESTROY_ROUTE,
    _VERIREEL_PREVIEW_INVENTORY_ROUTE as _VERIREEL_PREVIEW_INVENTORY_ROUTE,
    _VERIREEL_PREVIEW_REFRESH_ROUTE as _VERIREEL_PREVIEW_REFRESH_ROUTE,
    _VERIREEL_PREVIEW_VERIFICATION_ROUTE as _VERIREEL_PREVIEW_VERIFICATION_ROUTE,
    _VERIREEL_RUNTIME_VERIFICATION_ROUTE as _VERIREEL_RUNTIME_VERIFICATION_ROUTE,
    _VERIREEL_STABLE_ENVIRONMENT_ROUTE as _VERIREEL_STABLE_ENVIRONMENT_ROUTE,
    _VERIREEL_TESTING_VERIFICATION_ROUTE as _VERIREEL_TESTING_VERIFICATION_ROUTE,
)
from control_plane.verireel_nonprod_http import (
    VeriReelAppMaintenanceEnvelope as VeriReelAppMaintenanceEnvelope,
    VeriReelTestingDeployEnvelope as VeriReelTestingDeployEnvelope,
    _VERIREEL_APP_MAINTENANCE_ROUTE as _VERIREEL_APP_MAINTENANCE_ROUTE,
    _VERIREEL_TESTING_DEPLOY_ROUTE as _VERIREEL_TESTING_DEPLOY_ROUTE,
)
from control_plane.verireel_prod_http import (
    VeriReelProdBackupGateEnvelope as VeriReelProdBackupGateEnvelope,
    VeriReelProdDeployEnvelope as VeriReelProdDeployEnvelope,
    VeriReelProdPromotionEnvelope as VeriReelProdPromotionEnvelope,
    VeriReelProdRollbackEnvelope as VeriReelProdRollbackEnvelope,
    _VERIREEL_PROD_BACKUP_GATE_ROUTE as _VERIREEL_PROD_BACKUP_GATE_ROUTE,
    _VERIREEL_PROD_DEPLOY_ROUTE as _VERIREEL_PROD_DEPLOY_ROUTE,
    _VERIREEL_PROD_PROMOTION_ROUTE as _VERIREEL_PROD_PROMOTION_ROUTE,
    _VERIREEL_PROD_ROLLBACK_ROUTE as _VERIREEL_PROD_ROLLBACK_ROUTE,
)

_LAUNCHPLANE_SERVICE_CONTEXT = "launchplane"
_WHOLE_PRODUCT_CONTEXT = "*"
_EVERY_CODE_GITHUB_WEBHOOK_SECRET_ENV_KEY = "LAUNCHPLANE_EVERY_CODE_GITHUB_WEBHOOK_SECRET"
_NATIVE_FASTAPI_DRIVER_ROUTE_PATHS = frozenset(
    {
        _GENERIC_WEB_PREVIEW_DESIRED_STATE_ROUTE.route_path,
        _GENERIC_WEB_PREVIEW_DESTROY_ROUTE.route_path,
        _GENERIC_WEB_PREVIEW_INVENTORY_ROUTE.route_path,
        _GENERIC_WEB_PREVIEW_READINESS_ROUTE.route_path,
        _GENERIC_WEB_PREVIEW_REFRESH_ROUTE.route_path,
        _GENERIC_WEB_PREVIEW_VERIFICATION_ROUTE.route_path,
        _GENERIC_WEB_DEPLOY_ROUTE.route_path,
        _GENERIC_WEB_SOURCE_REF_DEPLOY_ROUTE.route_path,
        _GENERIC_WEB_PROD_PROMOTION_ROUTE.route_path,
        _GENERIC_WEB_PROD_PROMOTION_WORKFLOW_ROUTE.route_path,
        _GENERIC_WEB_ROLLBACK_PLAN_ROUTE.route_path,
        _GENERIC_WEB_ROLLBACK_ROUTE.route_path,
        _GENERIC_WEB_STABLE_VERIFICATION_ROUTE.route_path,
        _VERIREEL_PREVIEW_DESTROY_ROUTE.route_path,
        _VERIREEL_PREVIEW_INVENTORY_ROUTE.route_path,
        _VERIREEL_PREVIEW_REFRESH_ROUTE.route_path,
        _VERIREEL_PREVIEW_VERIFICATION_ROUTE.route_path,
        _VERIREEL_RUNTIME_VERIFICATION_ROUTE.route_path,
        _VERIREEL_STABLE_ENVIRONMENT_ROUTE.route_path,
        _VERIREEL_TESTING_DEPLOY_ROUTE.route_path,
        _VERIREEL_TESTING_VERIFICATION_ROUTE.route_path,
        _VERIREEL_APP_MAINTENANCE_ROUTE.route_path,
        _VERIREEL_PROD_BACKUP_GATE_ROUTE.route_path,
        _VERIREEL_PROD_DEPLOY_ROUTE.route_path,
        _VERIREEL_PROD_PROMOTION_ROUTE.route_path,
        _VERIREEL_PROD_ROLLBACK_ROUTE.route_path,
        "/v1/drivers/ingress/route-apply",
        ODOO_ARTIFACT_PUBLISH_ROUTE,
        "/v1/drivers/odoo/artifact-publish-inputs",
        ODOO_CONFIG_PARAMETER_OVERRIDE_ROUTE,
        ODOO_POST_DEPLOY_ROUTE,
        ODOO_PREVIEW_APPLY_INPUTS_ROUTE,
        ODOO_PREVIEW_APPLY_ROUTE,
        ODOO_PROD_BACKUP_GATE_ROUTE,
        ODOO_PROD_PROMOTION_INPUTS_ROUTE,
        ODOO_PROD_PROMOTION_ROUTE,
        ODOO_PROD_PROMOTION_RUN_ROUTE,
        ODOO_PROD_ROLLBACK_ROUTE,
        ODOO_STABLE_BOOTSTRAP_ROUTE,
        ODOO_TARGET_REPLACEMENT_APPLY_ROUTE,
        ODOO_TARGET_REPLACEMENT_PLAN_ROUTE,
        ODOO_WEBSITE_BOOTSTRAP_OVERRIDE_ROUTE,
    }
)


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


_EveryCodeWebhookResponse = tuple[int, dict[str, object]]


_LOGGER = logging.getLogger(__name__)


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
_PREVIEW_VERIFICATION_ROUTE_PATHS = frozenset({_GENERIC_WEB_PREVIEW_VERIFICATION_ROUTE.route_path})
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
    | _PREVIEW_VERIFICATION_ROUTE_PATHS
)
_GENERIC_WEB_BASE_DRIVER_ROUTE_PATHS = frozenset(
    _GENERIC_WEB_BASE_DRIVER_SHARED_ROUTE_PATHS | _GENERIC_WEB_BASE_DRIVER_PREVIEW_ROUTE_PATHS
)


_ODOO_POST_DEPLOY_ROUTE = _DriverRouteExecutionMetadata(
    route_path=ODOO_POST_DEPLOY_ROUTE,
    envelope_model=OdooPostDeployEnvelope,
    denial_message=(
        "Workflow cannot execute the Odoo post-deploy driver for the requested product/context."
    ),
)


_ODOO_CONFIG_PARAMETER_OVERRIDE_ROUTE = _DriverRouteExecutionMetadata(
    route_path=ODOO_CONFIG_PARAMETER_OVERRIDE_ROUTE,
    envelope_model=OdooConfigParameterOverrideEnvelope,
    denial_message=(
        "Workflow cannot write Odoo config-parameter overrides for the requested product/context."
    ),
)


_ODOO_WEBSITE_BOOTSTRAP_OVERRIDE_ROUTE = _DriverRouteExecutionMetadata(
    route_path=ODOO_WEBSITE_BOOTSTRAP_OVERRIDE_ROUTE,
    envelope_model=OdooWebsiteBootstrapOverrideEnvelope,
    denial_message=(
        "Workflow cannot write Odoo website-bootstrap overrides for the requested product/context."
    ),
)


_ODOO_STABLE_BOOTSTRAP_ROUTE = _DriverRouteExecutionMetadata(
    route_path=ODOO_STABLE_BOOTSTRAP_ROUTE,
    envelope_model=OdooStableBootstrapEnvelope,
    denial_message=(
        "Workflow cannot execute Odoo stable bootstrap for the requested product/context."
    ),
)


_ODOO_ARTIFACT_PUBLISH_ROUTE = _DriverRouteExecutionMetadata(
    route_path=ODOO_ARTIFACT_PUBLISH_ROUTE,
    envelope_model=OdooArtifactPublishEnvelope,
    denial_message=(
        "Workflow cannot write Odoo artifact publish evidence for the requested product/context."
    ),
)


_ODOO_PROD_BACKUP_GATE_METADATA = _DriverRouteExecutionMetadata(
    route_path=ODOO_PROD_BACKUP_GATE_ROUTE,
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


_ODOO_PROD_ROLLBACK_METADATA = _DriverRouteExecutionMetadata(
    route_path=ODOO_PROD_ROLLBACK_ROUTE,
    envelope_model=OdooProdRollbackEnvelope,
    denial_message=(
        "Workflow cannot execute the Odoo prod rollback driver for the requested product/context."
    ),
)


_ODOO_TARGET_REPLACEMENT_PLAN_ROUTE = _DriverRouteExecutionMetadata(
    route_path=ODOO_TARGET_REPLACEMENT_PLAN_ROUTE,
    envelope_model=OdooTargetReplacementPlanEnvelope,
    denial_message=(
        "Workflow cannot read the Odoo target replacement plan for the requested product/context."
    ),
)


_ODOO_TARGET_REPLACEMENT_APPLY_ROUTE = _DriverRouteExecutionMetadata(
    route_path=ODOO_TARGET_REPLACEMENT_APPLY_ROUTE,
    envelope_model=OdooTargetReplacementApplyEnvelope,
    denial_message=(
        "Workflow cannot apply Odoo target replacement for the requested product/context."
    ),
)


def _fastapi_route_paths_by_method(app: object, method: str) -> frozenset[str]:
    normalized_method = method.upper()
    route_paths: set[str] = set()
    for route in cast(Iterable[object], getattr(app, "routes", ())):
        route_path = getattr(route, "path", None)
        route_methods = getattr(route, "methods", None)
        if not isinstance(route_path, str) or route_methods is None:
            continue
        methods = {
            str(route_method).upper() for route_method in cast(Iterable[object], route_methods)
        }
        if normalized_method in methods:
            route_paths.add(route_path)
    return frozenset(route_paths)


def _validate_native_fastapi_driver_route_paths(app: object) -> None:
    missing_native_routes = sorted(
        _NATIVE_FASTAPI_DRIVER_ROUTE_PATHS - _fastapi_route_paths_by_method(app, "POST")
    )
    if missing_native_routes:
        raise ValueError(
            "Native FastAPI driver routes must be registered by the FastAPI app: "
            f"{', '.join(missing_native_routes)}"
        )


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


def _validate_native_descriptor_driver_routes() -> None:
    descriptor_routes = _driver_route_metadata_from_descriptors()
    post_descriptor_routes = frozenset(
        route_path
        for route_path, route_metadata in descriptor_routes.items()
        if route_metadata.method == "POST"
    )
    missing_post_descriptor_routes = sorted(
        post_descriptor_routes - _NATIVE_FASTAPI_DRIVER_ROUTE_PATHS
    )
    if missing_post_descriptor_routes:
        raise ValueError(
            "POST driver descriptor routes must be implemented as native FastAPI "
            f"routes: {', '.join(missing_post_descriptor_routes)}"
        )


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


def _decode_json_request_body_or_none(body_bytes: bytes) -> dict[str, object] | None:
    try:
        payload = json.loads(body_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    return cast(dict[str, object], payload)


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
    github_oauth_config = load_github_oauth_config_from_env()
    human_session_manager = (
        HumanSessionManager(
            config=github_oauth_config,
            session_store=service_record_store,
        )
        if github_oauth_config is not None
        else None
    )
    github_oauth_client = (
        GitHubOAuthClient(github_oauth_config) if github_oauth_config is not None else None
    )
    oauth_login_state_store = OAuthLoginStateStore()
    fastapi_application = create_launchplane_fastapi_app(
        verifier=verifier,
        authz_policy=resolved_fastapi_policy.policy,
        authz_policy_runtime=authz_policy_runtime,
        record_store_factory=lambda: service_record_store,
        bearer_identity_config=_bearer_identity_config_from_env(),
        human_session_manager=human_session_manager,
        github_oauth_client=github_oauth_client,
        oauth_login_state_store=oauth_login_state_store,
        control_plane_root_path=control_plane_root(),
        state_dir=state_dir,
        work_graph_planning_facts_provider=work_graph_planning_facts_provider,
        work_graph_issue_inbox_provider=work_graph_issue_inbox_provider,
        work_graph_issue_inbox_reconcile_provider=work_graph_issue_inbox_reconcile_provider,
        every_code_github_webhook_handler=handle_every_code_github_webhook_request,
    )
    _validate_native_descriptor_driver_routes()
    _validate_native_fastapi_driver_route_paths(fastapi_application)
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
