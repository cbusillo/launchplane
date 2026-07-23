from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from control_plane.authz_grant_service import is_immutable_job_workflow_ref
from control_plane.contracts.artifact_identity import (
    ArtifactIdentityManifest,
    artifact_manifest_matches_image_repository,
)
from control_plane.contracts.authz_policy_record import LaunchplaneAuthzPolicyRecord
from control_plane.contracts.data_provenance import FreshnessStatus
from control_plane.contracts.driver_descriptor import (
    DriverActionDescriptor,
    DriverActionReadinessRequirement,
)
from control_plane.contracts.lane_summary import LaunchplaneLaneSummary
from control_plane.contracts.product_environment_read_model import (
    ProductManagedSecretConfigStatusItem,
    ProductRuntimeConfigStatusItem,
)
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductLaneProfile,
    product_config_requirement_applies_to_lane,
)
from control_plane.contracts.product_topology_read_model import ProductEnvironmentTopology
from control_plane.durable_operation_authorization import (
    DurableOperationAuthorizationCaptureError,
    capture_durable_operation_authorization,
)
from control_plane.service_auth import (
    GitHubActionsIdentity,
    GitHubActionsPolicyRule,
    GitHubHumanIdentity,
    LaunchplaneIdentity,
    LocalAdminIdentity,
    LocalOperatorIdentity,
    TerminalAgentIdentity,
)


ProductOperationalReadinessState = Literal[
    "ready",
    "blocked",
    "stale",
    "missing",
    "unsupported",
]
ProductOperationalReadinessDimensionName = Literal[
    "product_lane",
    "action",
    "authorization",
    "provider_target",
    "route_binding",
    "runtime_environment",
    "managed_secrets",
    "artifact",
    "deployment",
    "topology",
]
ProductOperationalReadinessIdentityType = Literal[
    "github_actions",
    "github_human",
    "terminal_agent",
    "local_operator",
    "local_admin",
]


class ProductOperationalReadinessCaller(BaseModel):
    model_config = ConfigDict(extra="forbid")

    identity_type: ProductOperationalReadinessIdentityType
    repository: str = ""
    repository_id: str = ""
    repository_owner_id: str = ""
    workflow_ref: str = ""
    job_workflow_ref: str = ""


class ProductOperationalReadinessAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    requested_action: str
    action_id: str = ""
    label: str = ""
    safety: str = ""
    scope: str = ""
    method: str = ""
    route_path: str = ""
    supported: bool = False
    readiness_requirements: tuple[DriverActionReadinessRequirement, ...] = ()


class ProductOperationalReadinessEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    record_type: str
    record_id: str = ""
    revision: int | None = Field(default=None, ge=1)
    recorded_at: str = ""
    freshness_status: FreshnessStatus = "recorded"


class ProductOperationalReadinessRemediation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: str
    method: str = ""
    route_path: str = ""
    summary: str


class ProductOperationalReadinessDimension(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dimension: ProductOperationalReadinessDimensionName
    state: ProductOperationalReadinessState
    required: bool = True
    summary: str
    owner_record_type: str
    evidence: tuple[ProductOperationalReadinessEvidence, ...] = ()
    details: tuple[str, ...] = ()
    remediation: ProductOperationalReadinessRemediation | None = None


class ProductOperationalReadiness(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    generated_at: str
    product: str
    display_name: str
    repository: str
    driver_id: str
    context: str
    instance: str
    caller: ProductOperationalReadinessCaller
    action: ProductOperationalReadinessAction
    state: ProductOperationalReadinessState
    ready: bool
    dimensions: tuple[ProductOperationalReadinessDimension, ...]
    non_ready_dimensions: tuple[ProductOperationalReadinessDimensionName, ...] = ()


@dataclass(frozen=True, slots=True)
class ProductOperationalReadinessInputs:
    profile: LaunchplaneProductProfileRecord
    lane: ProductLaneProfile
    requested_action: str
    action: DriverActionDescriptor | None
    identity: LaunchplaneIdentity
    active_policy_records: tuple[LaunchplaneAuthzPolicyRecord, ...]
    lane_summary: LaunchplaneLaneSummary | None
    runtime_settings: tuple[ProductRuntimeConfigStatusItem, ...]
    managed_secrets: tuple[ProductManagedSecretConfigStatusItem, ...]
    topology: ProductEnvironmentTopology
    requested_artifact_id: str
    expected_current_artifact_id: str
    current_inventory_artifact_id: str | None
    artifact_manifest: ArtifactIdentityManifest | None
    generated_at: str


def build_product_operational_readiness(
    inputs: ProductOperationalReadinessInputs,
) -> ProductOperationalReadiness:
    action_projection, action_dimension = _action_projection(inputs)
    dimensions = [
        _product_lane_dimension(inputs),
        action_dimension,
        _authorization_dimension(inputs, action_dimension=action_dimension),
    ]
    if action_dimension.state == "ready" and inputs.action is not None:
        dimension_builders = {
            "provider_target": _provider_target_dimension,
            "route_binding": _route_binding_dimension,
            "runtime_environment": _runtime_environment_dimension,
            "managed_secrets": _managed_secrets_dimension,
            "artifact": _artifact_dimension,
            "deployment": _deployment_dimension,
            "topology": _topology_dimension,
        }
        dimensions.extend(
            dimension_builders[requirement](inputs)
            for requirement in inputs.action.readiness_requirements
        )
    state = combine_operational_readiness_states(
        tuple(dimension.state for dimension in dimensions if dimension.required)
    )
    return ProductOperationalReadiness(
        generated_at=inputs.generated_at,
        product=inputs.profile.product,
        display_name=inputs.profile.display_name,
        repository=inputs.profile.repository,
        driver_id=inputs.profile.driver_id,
        context=inputs.lane.context,
        instance=inputs.lane.instance,
        caller=_caller_summary(inputs.identity),
        action=action_projection,
        state=state,
        ready=state == "ready",
        dimensions=tuple(dimensions),
        non_ready_dimensions=tuple(
            dimension.dimension for dimension in dimensions if dimension.state != "ready"
        ),
    )


def combine_operational_readiness_states(
    states: tuple[ProductOperationalReadinessState, ...],
) -> ProductOperationalReadinessState:
    if not states:
        return "unsupported"
    for state in ("blocked", "missing", "stale", "unsupported"):
        if state in states:
            return state
    return "ready"


def _product_lane_dimension(
    inputs: ProductOperationalReadinessInputs,
) -> ProductOperationalReadinessDimension:
    return ProductOperationalReadinessDimension(
        dimension="product_lane",
        state="ready",
        summary="The product profile owns the requested context and instance.",
        owner_record_type="product_profile_record",
        evidence=(
            ProductOperationalReadinessEvidence(
                record_type="product_profile_record",
                record_id=inputs.profile.product,
                recorded_at=inputs.profile.updated_at,
            ),
        ),
    )


def _action_projection(
    inputs: ProductOperationalReadinessInputs,
) -> tuple[ProductOperationalReadinessAction, ProductOperationalReadinessDimension]:
    action = inputs.action
    supported = bool(
        action is not None
        and action.scope == "instance"
        and inputs.requested_action in (action.authz_action, *action.alternate_authz_actions)
        and action.readiness_requirements
    )
    projection = ProductOperationalReadinessAction(
        requested_action=inputs.requested_action,
        action_id=action.action_id if action is not None else "",
        label=action.label if action is not None else "",
        safety=action.safety if action is not None else "",
        scope=action.scope if action is not None else "",
        method=action.method if action is not None else "",
        route_path=action.route_path if action is not None else "",
        supported=supported,
        readiness_requirements=(action.readiness_requirements if supported and action else ()),
    )
    if supported:
        assert action is not None
        return projection, ProductOperationalReadinessDimension(
            dimension="action",
            state="ready",
            summary="The product driver declares operational readiness requirements for the action.",
            owner_record_type="driver_descriptor",
            evidence=(
                ProductOperationalReadinessEvidence(
                    record_type="driver_descriptor",
                    record_id=f"{inputs.profile.driver_id}:{action.action_id}",
                ),
            ),
        )
    return projection, ProductOperationalReadinessDimension(
        dimension="action",
        state="unsupported",
        summary="The requested action has no instance-scoped operational readiness contract.",
        owner_record_type="driver_descriptor",
        remediation=ProductOperationalReadinessRemediation(
            action="driver.read",
            method="GET",
            route_path="/v1/drivers",
            summary="Choose an instance-scoped driver action with declared readiness requirements.",
        ),
    )


def _authorization_dimension(
    inputs: ProductOperationalReadinessInputs,
    *,
    action_dimension: ProductOperationalReadinessDimension,
) -> ProductOperationalReadinessDimension:
    if action_dimension.state != "ready":
        return ProductOperationalReadinessDimension(
            dimension="authorization",
            state="unsupported",
            summary="Authorization readiness cannot be evaluated for an unsupported action.",
            owner_record_type="authz_policy_record",
        )
    records = inputs.active_policy_records
    if not records:
        return _authorization_not_ready(
            state="missing",
            summary="Launchplane has no active DB-backed authorization policy record.",
        )
    if len(records) != 1 or records[0].status != "active":
        return _authorization_not_ready(
            state="blocked",
            summary="Launchplane does not have exactly one active authorization policy record.",
        )
    record = records[0]
    identity = inputs.identity
    if not isinstance(identity, GitHubActionsIdentity):
        return _authorization_not_ready(
            state="blocked",
            summary="Operational workflow readiness requires an authenticated GitHub Actions caller.",
            record=record,
        )
    try:
        captured = capture_durable_operation_authorization(
            identity=identity,
            action=inputs.requested_action,
            product=inputs.profile.product,
            context=inputs.lane.context,
            instances=(inputs.lane.instance,),
            policy_record=record,
            authorized_at=inputs.generated_at,
        )
    except DurableOperationAuthorizationCaptureError:
        return _authorization_not_ready(
            state="blocked",
            summary=(
                "The active policy does not contain exactly one managed rule for the exact caller, "
                "action, product, context, and instance."
            ),
            record=record,
        )
    matching_rules = tuple(
        rule
        for rule in record.policy.github_actions
        if rule.managed_set_id == captured.managed_set_id
        and rule.managed_rule_id == captured.managed_rule_id
    )
    if len(matching_rules) != 1 or not _exact_managed_workflow_rule_ready(
        identity=identity,
        rule=matching_rules[0],
        requested_action=inputs.requested_action,
        product=inputs.profile.product,
        context=inputs.lane.context,
        instance=inputs.lane.instance,
    ):
        return _authorization_not_ready(
            state="blocked",
            summary=(
                "The matching managed rule does not use singleton exact selectors for the "
                "caller, reusable workflow, action, product, context, and instance."
            ),
            record=record,
        )
    return ProductOperationalReadinessDimension(
        dimension="authorization",
        state="ready",
        summary=(
            "The active DB-backed policy authorizes the immutable workflow caller for the exact "
            "action, product, context, and instance."
        ),
        owner_record_type="authz_policy_record",
        evidence=(_authz_policy_evidence(record),),
    )


def _authorization_not_ready(
    *,
    state: ProductOperationalReadinessState,
    summary: str,
    record: LaunchplaneAuthzPolicyRecord | None = None,
) -> ProductOperationalReadinessDimension:
    return ProductOperationalReadinessDimension(
        dimension="authorization",
        state=state,
        summary=summary,
        owner_record_type="authz_policy_record",
        evidence=(_authz_policy_evidence(record),) if record is not None else (),
        remediation=ProductOperationalReadinessRemediation(
            action="authz_policy.manage",
            method="POST",
            route_path="/v1/authz-policies/managed-rule-sets/reconcile",
            summary="Reconcile one exact managed workflow grant through the service API.",
        ),
    )


def _authz_policy_evidence(
    record: LaunchplaneAuthzPolicyRecord,
) -> ProductOperationalReadinessEvidence:
    return ProductOperationalReadinessEvidence(
        record_type="authz_policy_record",
        record_id=record.record_id,
        revision=record.revision,
        recorded_at=record.updated_at,
    )


def _exact_managed_workflow_rule_ready(
    *,
    identity: GitHubActionsIdentity,
    rule: GitHubActionsPolicyRule,
    requested_action: str,
    product: str,
    context: str,
    instance: str,
) -> bool:
    return bool(
        identity.repository_id
        and identity.repository_owner_id
        and rule.repository == identity.repository
        and rule.repository_id == identity.repository_id
        and rule.repository_owner_id == identity.repository_owner_id
        and rule.workflow_refs == (identity.workflow_ref,)
        and rule.job_workflow_refs == (identity.job_workflow_ref,)
        and is_immutable_job_workflow_ref(identity.job_workflow_ref)
        and rule.products == (product,)
        and rule.contexts == (context,)
        and rule.instances == (instance,)
        and rule.actions == (requested_action,)
    )


def _provider_target_dimension(
    inputs: ProductOperationalReadinessInputs,
) -> ProductOperationalReadinessDimension:
    lane_summary = inputs.lane_summary
    if lane_summary is None or lane_summary.provider_target is None:
        return _missing_dimension(
            dimension="provider_target",
            summary="Launchplane has no provider-target record for this lane.",
            owner_record_type="provider_target_record",
        )
    target = lane_summary.provider_target
    if target.context != inputs.lane.context or target.instance != inputs.lane.instance:
        return _blocked_dimension(
            dimension="provider_target",
            summary="The provider-target record does not match the requested lane.",
            owner_record_type="provider_target_record",
        )
    freshness_state = _freshness_readiness_state(lane_summary.provenance.freshness_status)
    return ProductOperationalReadinessDimension(
        dimension="provider_target",
        state=freshness_state,
        summary=(
            "Launchplane has current provider-target authority for this lane."
            if freshness_state == "ready"
            else "Provider-target evidence is not current for this lane."
        ),
        owner_record_type="provider_target_record",
        evidence=(
            ProductOperationalReadinessEvidence(
                record_type="provider_target_record",
                recorded_at=target.updated_at,
                freshness_status=lane_summary.provenance.freshness_status,
            ),
        ),
    )


def _route_binding_dimension(
    inputs: ProductOperationalReadinessInputs,
) -> ProductOperationalReadinessDimension:
    provider_recorded = inputs.topology.provider_recorded
    if provider_recorded.authority_status == "missing":
        return _missing_dimension(
            dimension="route_binding",
            summary="Launchplane has no provider-neutral route-binding record for this lane.",
            owner_record_type="environment_route_binding_record",
        )
    if provider_recorded.authority_status == "disabled":
        return _blocked_dimension(
            dimension="route_binding",
            summary="The provider-neutral route-binding record is disabled.",
            owner_record_type="environment_route_binding_record",
        )
    warnings = tuple(
        warning.code
        for warning in inputs.topology.warnings
        if warning.scope in {"authority", "placement", "domains", "ingress", "tls"}
    )
    if warnings:
        return _blocked_dimension(
            dimension="route_binding",
            summary="Recorded route authority does not match the requested lane topology.",
            owner_record_type="environment_route_binding_record",
            details=warnings,
        )
    state = _freshness_readiness_state(provider_recorded.trust_state)
    return ProductOperationalReadinessDimension(
        dimension="route_binding",
        state=state,
        summary=(
            "Provider-neutral route authority is active and current."
            if state == "ready"
            else "Provider-neutral route authority is not current."
        ),
        owner_record_type="environment_route_binding_record",
        evidence=(
            ProductOperationalReadinessEvidence(
                record_type="environment_route_binding_record",
                recorded_at=provider_recorded.provenance.recorded_at,
                freshness_status=provider_recorded.trust_state,
            ),
        ),
    )


def _runtime_environment_dimension(
    inputs: ProductOperationalReadinessInputs,
) -> ProductOperationalReadinessDimension:
    requirements = tuple(
        requirement
        for requirement in inputs.profile.expected_config.runtime_environment_keys
        if product_config_requirement_applies_to_lane(
            requirement_context=requirement.context,
            requirement_instance=requirement.instance,
            lane=inputs.lane,
        )
    )
    if not requirements:
        return _unsupported_dimension(
            dimension="runtime_environment",
            summary="The product profile declares no runtime-environment requirements for this lane.",
            owner_record_type="product_profile_record",
        )
    statuses = {item.key: item for item in inputs.runtime_settings}
    missing_keys = tuple(
        requirement.key for requirement in requirements if requirement.key not in statuses
    )
    states = tuple(_config_status_readiness_state(item.status) for item in statuses.values())
    if missing_keys:
        states = (*states, "missing")
    state = combine_operational_readiness_states(states)
    return ProductOperationalReadinessDimension(
        dimension="runtime_environment",
        state=state,
        summary=(
            "All declared runtime-environment keys are recorded for this lane."
            if state == "ready"
            else "Declared runtime-environment metadata is incomplete or not current."
        ),
        owner_record_type="runtime_environment_record",
        details=missing_keys,
        remediation=ProductOperationalReadinessRemediation(
            action="product_config.plan",
            method="POST",
            route_path="/v1/products/{product}/environments/{environment}/config/apply",
            summary="Plan the missing runtime-environment metadata through the product config API.",
        )
        if state != "ready"
        else None,
    )


def _managed_secrets_dimension(
    inputs: ProductOperationalReadinessInputs,
) -> ProductOperationalReadinessDimension:
    requirements = tuple(
        requirement
        for requirement in inputs.profile.expected_config.managed_secret_bindings
        if product_config_requirement_applies_to_lane(
            requirement_context=requirement.context,
            requirement_instance=requirement.instance,
            lane=inputs.lane,
        )
    )
    if not requirements:
        return _unsupported_dimension(
            dimension="managed_secrets",
            summary="The product profile declares no managed-secret binding requirements for this lane.",
            owner_record_type="product_profile_record",
        )
    statuses = {(item.integration, item.binding_key): item for item in inputs.managed_secrets}
    missing_bindings = tuple(
        f"{requirement.integration}:{requirement.binding_key}"
        for requirement in requirements
        if (requirement.integration, requirement.binding_key) not in statuses
    )
    states = tuple(_config_status_readiness_state(item.status) for item in statuses.values())
    if missing_bindings:
        states = (*states, "missing")
    state = combine_operational_readiness_states(states)
    return ProductOperationalReadinessDimension(
        dimension="managed_secrets",
        state=state,
        summary=(
            "All declared managed-secret bindings are recorded and enabled."
            if state == "ready"
            else "Declared managed-secret binding metadata is incomplete or disabled."
        ),
        owner_record_type="secret_binding_record",
        details=missing_bindings,
        remediation=ProductOperationalReadinessRemediation(
            action="product_config.plan",
            method="POST",
            route_path="/v1/products/{product}/environments/{environment}/config/apply",
            summary="Plan the missing managed-secret bindings through the product config API.",
        )
        if state != "ready"
        else None,
    )


def _artifact_dimension(
    inputs: ProductOperationalReadinessInputs,
) -> ProductOperationalReadinessDimension:
    artifact_id = inputs.requested_artifact_id.strip()
    if not artifact_id:
        return _missing_dimension(
            dimension="artifact",
            summary="The readiness request does not name an exact persisted artifact.",
            owner_record_type="artifact_manifest",
        )
    manifest = inputs.artifact_manifest
    if manifest is None:
        return _missing_dimension(
            dimension="artifact",
            summary="Launchplane has no artifact manifest for the requested artifact ID.",
            owner_record_type="artifact_manifest",
            details=(artifact_id,),
        )
    if manifest.artifact_id != artifact_id:
        return _blocked_dimension(
            dimension="artifact",
            summary="The resolved artifact manifest does not match the requested artifact ID.",
            owner_record_type="artifact_manifest",
        )
    if not artifact_manifest_matches_image_repository(
        manifest,
        expected_repository=inputs.profile.image.repository,
    ):
        return _blocked_dimension(
            dimension="artifact",
            summary="The requested artifact does not belong to the product image repository.",
            owner_record_type="artifact_manifest",
        )
    return ProductOperationalReadinessDimension(
        dimension="artifact",
        state="ready",
        summary="The exact requested artifact manifest is persisted in Launchplane.",
        owner_record_type="artifact_manifest",
        evidence=(
            ProductOperationalReadinessEvidence(
                record_type="artifact_manifest",
                record_id=artifact_id,
            ),
        ),
    )


def _deployment_dimension(
    inputs: ProductOperationalReadinessInputs,
) -> ProductOperationalReadinessDimension:
    lane_summary = inputs.lane_summary
    deployment = lane_summary.latest_deployment if lane_summary is not None else None
    if deployment is None:
        return _missing_dimension(
            dimension="deployment",
            summary="Launchplane has no deployment record for this lane.",
            owner_record_type="deployment_record",
        )
    if deployment.context != inputs.lane.context or deployment.instance != inputs.lane.instance:
        return _blocked_dimension(
            dimension="deployment",
            summary="The deployment record does not match the requested lane.",
            owner_record_type="deployment_record",
        )
    expected_current_artifact_id = inputs.expected_current_artifact_id.strip()
    if expected_current_artifact_id and inputs.current_inventory_artifact_id is None:
        return _missing_dimension(
            dimension="deployment",
            summary="Launchplane has no current environment inventory for this lane.",
            owner_record_type="environment_inventory",
        )
    if (
        expected_current_artifact_id
        and inputs.current_inventory_artifact_id != expected_current_artifact_id
    ):
        return _blocked_dimension(
            dimension="deployment",
            summary="The current inventory artifact changed after the caller read the lane.",
            owner_record_type="environment_inventory",
        )
    deployed_artifact_id = (
        deployment.artifact_identity.artifact_id if deployment.artifact_identity is not None else ""
    )
    if expected_current_artifact_id and deployed_artifact_id != expected_current_artifact_id:
        return _blocked_dimension(
            dimension="deployment",
            summary="The current deployment artifact changed after the caller read the lane.",
            owner_record_type="deployment_record",
        )
    health = deployment.destination_health
    runtime_identity_required = inputs.lane.odoo_data_policy.requires_runtime_identity
    if (
        deployment.deploy.status != "pass"
        or not health.verified
        or health.status != "pass"
        or (runtime_identity_required and health.runtime_identity_status != "match")
    ):
        return _blocked_dimension(
            dimension="deployment",
            summary="Current deployment, health, or runtime-identity evidence is not passing.",
            owner_record_type="deployment_record",
            evidence=(
                ProductOperationalReadinessEvidence(
                    record_type="deployment_record",
                    record_id=deployment.record_id,
                    recorded_at=deployment.deploy.finished_at,
                ),
            ),
        )
    freshness = lane_summary.provenance.freshness_status if lane_summary is not None else "missing"
    state = _freshness_readiness_state(freshness)
    return ProductOperationalReadinessDimension(
        dimension="deployment",
        state=state,
        summary=(
            "Current deployment, health, and runtime-identity evidence are passing."
            if state == "ready"
            else "Deployment evidence is not current."
        ),
        owner_record_type="deployment_record",
        evidence=(
            ProductOperationalReadinessEvidence(
                record_type="deployment_record",
                record_id=deployment.record_id,
                recorded_at=deployment.deploy.finished_at,
                freshness_status=freshness,
            ),
        ),
    )


def _topology_dimension(
    inputs: ProductOperationalReadinessInputs,
) -> ProductOperationalReadinessDimension:
    warnings = tuple(warning.code for warning in inputs.topology.warnings)
    if warnings:
        return _blocked_dimension(
            dimension="topology",
            summary="Current route, runtime, HTTP, or TLS topology evidence has warnings.",
            owner_record_type="product_environment_topology",
            details=warnings,
        )
    if (
        inputs.lane.odoo_data_policy.requires_runtime_identity
        and inputs.topology.observed.placement.runtime_identity_status != "match"
    ):
        return _blocked_dimension(
            dimension="topology",
            summary="Observed topology does not prove the expected runtime identity.",
            owner_record_type="public_ingress_observation_record",
        )
    state = _freshness_readiness_state(inputs.topology.trust_state)
    return ProductOperationalReadinessDimension(
        dimension="topology",
        state=state,
        summary=(
            "Recorded and observed route, runtime, HTTP, and TLS topology evidence is current."
            if state == "ready"
            else "Product topology evidence is not current."
        ),
        owner_record_type="product_environment_topology",
        evidence=(
            ProductOperationalReadinessEvidence(
                record_type="environment_route_binding_record",
                recorded_at=inputs.topology.provider_recorded.provenance.recorded_at,
                freshness_status=inputs.topology.provider_recorded.trust_state,
            ),
            ProductOperationalReadinessEvidence(
                record_type="public_ingress_observation_record",
                recorded_at=inputs.topology.observed.ingress.observed_at,
                freshness_status=inputs.topology.observed.trust_state,
            ),
        ),
    )


def _caller_summary(identity: LaunchplaneIdentity) -> ProductOperationalReadinessCaller:
    if isinstance(identity, GitHubActionsIdentity):
        return ProductOperationalReadinessCaller(
            identity_type="github_actions",
            repository=identity.repository,
            repository_id=identity.repository_id,
            repository_owner_id=identity.repository_owner_id,
            workflow_ref=identity.workflow_ref,
            job_workflow_ref=identity.job_workflow_ref,
        )
    if isinstance(identity, GitHubHumanIdentity):
        identity_type: ProductOperationalReadinessIdentityType = "github_human"
    elif isinstance(identity, TerminalAgentIdentity):
        identity_type = "terminal_agent"
    elif isinstance(identity, LocalOperatorIdentity):
        identity_type = "local_operator"
    elif isinstance(identity, LocalAdminIdentity):
        identity_type = "local_admin"
    else:
        raise TypeError(f"Unsupported Launchplane identity type: {type(identity).__name__}")
    return ProductOperationalReadinessCaller(identity_type=identity_type)


def _freshness_readiness_state(freshness: FreshnessStatus) -> ProductOperationalReadinessState:
    if freshness in {"verified", "recorded"}:
        return "ready"
    if freshness == "stale":
        return "stale"
    if freshness == "unsupported":
        return "unsupported"
    return "missing"


def _config_status_readiness_state(status: str) -> ProductOperationalReadinessState:
    if status == "configured":
        return "ready"
    if status == "stale":
        return "stale"
    if status == "unsupported":
        return "unsupported"
    if status in {"disabled", "unvalidated"}:
        return "blocked"
    return "missing"


def _missing_dimension(
    *,
    dimension: ProductOperationalReadinessDimensionName,
    summary: str,
    owner_record_type: str,
    details: tuple[str, ...] = (),
) -> ProductOperationalReadinessDimension:
    return ProductOperationalReadinessDimension(
        dimension=dimension,
        state="missing",
        summary=summary,
        owner_record_type=owner_record_type,
        details=details,
    )


def _blocked_dimension(
    *,
    dimension: ProductOperationalReadinessDimensionName,
    summary: str,
    owner_record_type: str,
    evidence: tuple[ProductOperationalReadinessEvidence, ...] = (),
    details: tuple[str, ...] = (),
) -> ProductOperationalReadinessDimension:
    return ProductOperationalReadinessDimension(
        dimension=dimension,
        state="blocked",
        summary=summary,
        owner_record_type=owner_record_type,
        evidence=evidence,
        details=details,
    )


def _unsupported_dimension(
    *,
    dimension: ProductOperationalReadinessDimensionName,
    summary: str,
    owner_record_type: str,
) -> ProductOperationalReadinessDimension:
    return ProductOperationalReadinessDimension(
        dimension=dimension,
        state="unsupported",
        summary=summary,
        owner_record_type=owner_record_type,
    )
