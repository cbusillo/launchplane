from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from control_plane.contracts.artifact_identity import ArtifactIdentityManifest
from control_plane.contracts.authz_policy_record import LaunchplaneAuthzPolicyRecord
from control_plane.contracts.backup_gate_record import BackupGateRecord
from control_plane.contracts.data_provenance import DataProvenance, FreshnessStatus
from control_plane.contracts.deployment_record import DeploymentRecord
from control_plane.contracts.driver_descriptor import DriverActionDescriptor, DriverDescriptor
from control_plane.contracts.lane_summary import LaunchplaneLaneSummary
from control_plane.contracts.preview_desired_state_record import PreviewDesiredStateRecord
from control_plane.contracts.preview_lifecycle_cleanup_record import PreviewLifecycleCleanupRecord
from control_plane.contracts.preview_pr_feedback_record import PreviewPrFeedbackRecord
from control_plane.contracts.preview_record import PreviewRecord
from control_plane.contracts.preview_summary import LaunchplanePreviewSummary
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductLaneProfile,
    ProductRuntimeConfigRequirement,
    ProductSecretConfigRequirement,
)
from control_plane.contracts.public_ingress_monitoring import PublicIngressIncidentRecord
from control_plane.contracts.public_ingress_monitoring import PublicIngressObservationRecord
from control_plane.contracts.promotion_record import PromotionRecord
from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord
from control_plane.contracts.runtime_identity import RuntimeIdentity, RuntimeIdentityStatus
from control_plane.contracts.secret_record import SecretBinding
from control_plane.drivers.registry import (
    build_driver_context_view,
    effective_driver_actions,
    list_driver_descriptors,
    read_driver_descriptor,
)


ActionAllowed = Callable[[str, str, str], bool]
ProductSecretBindingTrustState = FreshnessStatus | Literal["disabled"]
ProductConfigItemStatus = Literal[
    "configured",
    "missing",
    "disabled",
    "unvalidated",
    "stale",
    "unsupported",
]


class ProductReadModelStore(Protocol):
    def list_product_profile_records(
        self,
        *,
        driver_id: str = "",
    ) -> tuple[LaunchplaneProductProfileRecord, ...]: ...

    def read_product_profile_record(self, product: str) -> LaunchplaneProductProfileRecord: ...


class ProductEnvironmentReadModelStore(ProductReadModelStore, Protocol):
    def read_lane_summary(
        self, *, context_name: str, instance_name: str
    ) -> LaunchplaneLaneSummary: ...

    def list_deployment_records(
        self, *, context_name: str = "", instance_name: str = "", limit: int | None = None
    ) -> tuple[DeploymentRecord, ...]: ...

    def list_promotion_records(
        self,
        *,
        context_name: str = "",
        from_instance_name: str = "",
        to_instance_name: str = "",
        limit: int | None = None,
    ) -> tuple[PromotionRecord, ...]: ...

    def list_backup_gate_records(
        self, *, context_name: str = "", instance_name: str = "", limit: int | None = None
    ) -> tuple[BackupGateRecord, ...]: ...

    def list_preview_records(
        self,
        *,
        context_name: str = "",
        anchor_repo: str = "",
        anchor_pr_number: int | None = None,
        limit: int | None = None,
    ) -> tuple[PreviewRecord, ...]: ...

    def list_preview_summaries(
        self,
        *,
        context_name: str = "",
        anchor_repo: str = "",
        anchor_pr_number: int | None = None,
        preview_limit: int | None = None,
        generation_limit: int | None = 1,
    ) -> tuple[LaunchplanePreviewSummary, ...]: ...

    def list_preview_desired_state_records(
        self, *, context_name: str = "", limit: int | None = None
    ) -> tuple[PreviewDesiredStateRecord, ...]: ...

    def list_preview_lifecycle_cleanup_records(
        self, *, context_name: str = "", limit: int | None = None
    ) -> tuple[PreviewLifecycleCleanupRecord, ...]: ...

    def list_preview_pr_feedback_records(
        self, *, context_name: str = "", limit: int | None = None
    ) -> tuple[PreviewPrFeedbackRecord, ...]: ...

    def list_authz_policy_records(
        self, *, status: str = "", limit: int | None = None
    ) -> tuple[LaunchplaneAuthzPolicyRecord, ...]: ...

    def list_public_ingress_observation_records(
        self,
        *,
        product: str = "",
        context_name: str = "",
        instance_name: str = "",
        check_name: str = "",
        check_kind: str = "",
        limit: int | None = None,
    ) -> tuple[PublicIngressObservationRecord, ...]: ...

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
    ) -> tuple[PublicIngressIncidentRecord, ...]: ...


class ProductEnvironmentReadModelCapabilityError(RuntimeError):
    pass


def _build_action_authz_by_route() -> dict[str, str]:
    return {
        action.route_path: action.authz_action
        for descriptor in list_driver_descriptors()
        for action in descriptor.actions
        if action.route_path and action.authz_action
    }


ACTION_AUTHZ_BY_ROUTE = _build_action_authz_by_route()

PREVIEW_PROFILE_REQUIRED_ACTION_IDS = {
    "preview_desired_state",
    "preview_inventory",
    "preview_readiness",
}

OPERATOR_ACTION_IDS = {
    "stable_deploy": ("Deploy lane", "mutation", "instance"),
    "prod_promotion": ("Promote testing to prod", "mutation", "instance"),
    "prod_promotion_workflow": ("Dispatch promote workflow", "mutation", "instance"),
    "prod_backup_gate": ("Capture prod backup gate", "safe_write", "instance"),
    "prod_rollback": ("Roll back prod", "destructive", "instance"),
    "preview_desired_state": ("Discover desired previews", "safe_write", "context"),
    "preview_inventory": ("Read preview inventory", "read", "context"),
    "preview_refresh": ("Refresh preview", "mutation", "preview"),
    "preview_readiness": ("Evaluate preview readiness", "read", "context"),
    "preview_destroy": ("Destroy preview", "destructive", "preview"),
}


class ProductActionAvailability(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action_id: str
    label: str
    description: str = ""
    safety: str
    scope: str
    method: str = ""
    route_path: str = ""
    authz_action: str = ""
    alternate_authz_actions: tuple[str, ...] = ()
    enabled: bool
    disabled_reasons: tuple[str, ...] = ()
    trust_state: FreshnessStatus = "recorded"


class ProductRuntimeSettingSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope: str
    context: str
    instance: str
    env_keys: tuple[str, ...]
    env_value_count: int = Field(ge=0)
    updated_at: str
    source_label: str
    trust_state: FreshnessStatus = "recorded"


class ProductSecretBindingSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    binding_id: str
    secret_id: str
    integration: str
    binding_type: str
    binding_key: str
    context: str
    instance: str
    status: str
    updated_at: str
    trust_state: ProductSecretBindingTrustState


class ProductTargetSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str = "dokploy"
    target_type: str = ""
    target_name: str = ""
    target_id: str = ""
    provider_target_type: str = ""
    target_id_recorded: bool = False
    artifact_manifest: ArtifactIdentityManifest | None = None
    expected_runtime_identity: RuntimeIdentity | None = None
    observed_runtime_identity: RuntimeIdentity | None = None
    runtime_identity_status: RuntimeIdentityStatus = "unchecked"
    runtime_identity_detail: str = ""
    trust_state: FreshnessStatus = "missing"


class ProductPublicIngressSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str = "missing"
    failure_code: str = ""
    observed_at: str = ""
    record_id: str = ""
    summary: str = ""
    notification_sent: bool = False
    incident_status: str = ""
    incident_id: str = ""
    incident_opened_at: str = ""
    trust_state: FreshnessStatus = "missing"
    provenance: DataProvenance = DataProvenance(
        source_kind="record",
        freshness_status="missing",
        detail="Launchplane has not recorded public ingress observations for this lane.",
    )


class ProductEnvironmentSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    environment: str
    context: str
    base_url: str = ""
    health_url: str = ""
    prelaunch_rebuild_allowed: bool = False
    prelaunch_rebuild_data_source_mode: str = ""
    prelaunch_rebuild_approval_issue_url: str = ""
    odoo_data_authority: str = "unknown"
    odoo_allowed_rebuild_sources: tuple[str, ...] = ()
    odoo_upstream_source: str = ""
    odoo_requires_backup_before_destroy: bool = True
    odoo_requires_restore_proof: bool = True
    odoo_requires_runtime_identity: bool = True
    public_ingress: ProductPublicIngressSummary = Field(default_factory=ProductPublicIngressSummary)
    trust_state: FreshnessStatus
    provenance: DataProvenance
    warnings: tuple[str, ...] = ()
    available_actions: tuple[ProductActionAvailability, ...] = ()


class ProductPreviewSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool
    context: str = ""
    slug_template: str = ""
    active_count: int = Field(default=0, ge=0)
    latest_preview_id: str = ""
    trust_state: FreshnessStatus = "unsupported"
    provenance: DataProvenance = DataProvenance(
        source_kind="unsupported",
        freshness_status="unsupported",
        detail="Product previews are not enabled for this product profile.",
    )


class ProductSiteOverview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    product: str
    display_name: str
    repository: str
    driver_id: str
    base_driver_id: str = ""
    environments: tuple[ProductEnvironmentSummary, ...] = ()
    preview: ProductPreviewSummary
    warnings: tuple[str, ...] = ()
    trust_state: FreshnessStatus
    provenance: DataProvenance
    available_actions: tuple[ProductActionAvailability, ...] = ()


class ProductEnvironmentDetail(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    product: str
    display_name: str
    repository: str
    driver_id: str
    base_driver_id: str = ""
    environment: str
    context: str
    base_url: str = ""
    health_url: str = ""
    prelaunch_rebuild_allowed: bool = False
    prelaunch_rebuild_data_source_mode: str = ""
    prelaunch_rebuild_approval_issue_url: str = ""
    odoo_data_authority: str = "unknown"
    odoo_allowed_rebuild_sources: tuple[str, ...] = ()
    odoo_upstream_source: str = ""
    odoo_requires_backup_before_destroy: bool = True
    odoo_requires_restore_proof: bool = True
    odoo_requires_runtime_identity: bool = True
    target: ProductTargetSummary
    public_ingress: ProductPublicIngressSummary = Field(default_factory=ProductPublicIngressSummary)
    runtime_settings: tuple[ProductRuntimeSettingSummary, ...] = ()
    managed_secrets: tuple[ProductSecretBindingSummary, ...] = ()
    available_actions: tuple[ProductActionAvailability, ...] = ()
    warnings: tuple[str, ...] = ()
    trust_state: FreshnessStatus
    provenance: DataProvenance


class ProductRuntimeConfigStatusItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    status: ProductConfigItemStatus
    context: str = ""
    instance: str = ""
    source_label: str = ""
    updated_at: str = ""
    trust_state: FreshnessStatus = "missing"


class ProductManagedSecretConfigStatusItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    binding_key: str
    status: ProductConfigItemStatus
    integration: str
    context: str = ""
    instance: str = ""
    updated_at: str = ""
    trust_state: ProductSecretBindingTrustState | Literal["unsupported"] = "missing"


class ProductEnvironmentConfigStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    product: str
    display_name: str
    repository: str
    driver_id: str
    base_driver_id: str = ""
    environment: str
    context: str
    runtime_settings: tuple[ProductRuntimeConfigStatusItem, ...] = ()
    managed_secrets: tuple[ProductManagedSecretConfigStatusItem, ...] = ()
    warnings: tuple[str, ...] = ()
    trust_state: FreshnessStatus
    provenance: DataProvenance


class ProductActivityRecordLink(BaseModel):
    model_config = ConfigDict(extra="forbid")

    record_type: str
    record_id: str


class ProductActivityEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str
    event_type: str
    product: str
    context: str
    environment: str = ""
    driver_id: str
    action_id: str
    status: str
    occurred_at: str
    title: str
    summary: str = ""
    records: tuple[ProductActivityRecordLink, ...] = ()
    trust_state: FreshnessStatus = "recorded"


class ProductActivityReadModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    product: str
    display_name: str
    repository: str
    driver_id: str
    events: tuple[ProductActivityEvent, ...] = ()


def build_product_site_overviews(
    *, record_store: ProductReadModelStore, action_allowed: ActionAllowed
) -> tuple[ProductSiteOverview, ...]:
    return tuple(
        build_product_site_overview(
            record_store=record_store,
            product=profile.product,
            action_allowed=action_allowed,
        )
        for profile in record_store.list_product_profile_records()
    )


def build_product_site_overview(
    *, record_store: ProductReadModelStore, product: str, action_allowed: ActionAllowed
) -> ProductSiteOverview:
    profile = record_store.read_product_profile_record(product)
    descriptor, descriptor_warning = _read_profile_descriptor(profile)
    environment_summaries = tuple(
        _build_environment_summary(
            record_store=record_store,
            profile=profile,
            descriptor=descriptor,
            lane=lane,
            action_allowed=action_allowed,
        )
        for lane in profile.lanes
    )
    preview_summary = _build_preview_summary(record_store=record_store, profile=profile)
    available_actions = _action_availability(
        descriptor=descriptor,
        profile=profile,
        product=profile.product,
        previews_enabled=profile.preview.enabled,
        action_allowed=action_allowed,
        include_unsupported=True,
        context_resolver=_product_action_context_resolver(profile=profile),
    )
    warnings = tuple(warning for warning in (descriptor_warning,) if warning)
    trust_state = _combine_trust_states(
        (summary.trust_state for summary in environment_summaries),
        fallback="recorded",
    )
    return ProductSiteOverview(
        product=profile.product,
        display_name=profile.display_name,
        repository=profile.repository,
        driver_id=profile.driver_id,
        base_driver_id=descriptor.base_driver_id if descriptor is not None else "",
        environments=environment_summaries,
        preview=preview_summary,
        warnings=warnings,
        trust_state=trust_state,
        provenance=_profile_provenance(profile),
        available_actions=available_actions,
    )


def build_product_environment_detail(
    *,
    record_store: ProductReadModelStore,
    product: str,
    environment: str,
    action_allowed: ActionAllowed,
) -> ProductEnvironmentDetail:
    profile = record_store.read_product_profile_record(product)
    lane = _find_lane(profile=profile, environment=environment)
    descriptor, descriptor_warning = _read_profile_descriptor(profile)
    lane_summary = _read_product_lane_summary(
        record_store=record_store,
        profile=profile,
        lane=lane,
    )
    provenance = (
        lane_summary.provenance if lane_summary is not None else _missing_lane_provenance(lane)
    )
    warnings = tuple(warning for warning in (descriptor_warning,) if warning)
    return ProductEnvironmentDetail(
        product=profile.product,
        display_name=profile.display_name,
        repository=profile.repository,
        driver_id=profile.driver_id,
        base_driver_id=descriptor.base_driver_id if descriptor is not None else "",
        environment=lane.instance,
        context=lane.context,
        base_url=lane.base_url,
        health_url=lane.health_url,
        prelaunch_rebuild_allowed=lane.odoo_prelaunch_rebuild.enabled,
        prelaunch_rebuild_data_source_mode=lane.odoo_prelaunch_rebuild.data_source_mode
        if lane.odoo_prelaunch_rebuild.enabled
        else "",
        prelaunch_rebuild_approval_issue_url=lane.odoo_prelaunch_rebuild.approval_issue_url,
        odoo_data_authority=lane.odoo_data_policy.data_authority,
        odoo_allowed_rebuild_sources=lane.odoo_data_policy.allowed_rebuild_sources,
        odoo_upstream_source=lane.odoo_data_policy.upstream_source,
        odoo_requires_backup_before_destroy=lane.odoo_data_policy.requires_backup_before_destroy,
        odoo_requires_restore_proof=lane.odoo_data_policy.requires_restore_proof,
        odoo_requires_runtime_identity=lane.odoo_data_policy.requires_runtime_identity,
        target=_target_summary(lane_summary),
        public_ingress=_public_ingress_summary(
            record_store=record_store,
            profile=profile,
            lane=lane,
        ),
        runtime_settings=_runtime_setting_summaries(lane_summary),
        managed_secrets=_secret_binding_summaries(lane_summary),
        available_actions=_action_availability(
            descriptor=descriptor,
            profile=profile,
            product=profile.product,
            previews_enabled=profile.preview.enabled,
            action_allowed=action_allowed,
            include_unsupported=True,
            context_resolver=_lane_context_resolver(context=lane.context),
        ),
        warnings=warnings,
        trust_state=provenance.freshness_status,
        provenance=provenance,
    )


def build_product_environment_config_status(
    *,
    record_store: ProductReadModelStore,
    product: str,
    environment: str,
) -> ProductEnvironmentConfigStatus:
    profile = record_store.read_product_profile_record(product)
    lane = _find_lane(profile=profile, environment=environment)
    descriptor, descriptor_warning = _read_profile_descriptor(profile)
    lane_summary = _read_product_lane_summary(
        record_store=record_store,
        profile=profile,
        lane=lane,
    )
    provenance = (
        lane_summary.provenance if lane_summary is not None else _missing_lane_provenance(lane)
    )
    runtime_settings = _runtime_config_status_items(
        requirements=profile.expected_config.runtime_environment_keys,
        lane=lane,
        lane_summary=lane_summary,
    )
    managed_secrets = _managed_secret_config_status_items(
        requirements=profile.expected_config.managed_secret_bindings,
        lane=lane,
        lane_summary=lane_summary,
    )
    warnings = tuple(warning for warning in (descriptor_warning,) if warning)
    trust_state = _combine_trust_states(
        (
            *(_config_item_freshness(item.status) for item in runtime_settings),
            *(_config_item_freshness(item.status) for item in managed_secrets),
        ),
        fallback=provenance.freshness_status,
    )
    return ProductEnvironmentConfigStatus(
        product=profile.product,
        display_name=profile.display_name,
        repository=profile.repository,
        driver_id=profile.driver_id,
        base_driver_id=descriptor.base_driver_id if descriptor is not None else "",
        environment=lane.instance,
        context=lane.context,
        runtime_settings=runtime_settings,
        managed_secrets=managed_secrets,
        warnings=warnings,
        trust_state=trust_state,
        provenance=provenance,
    )


def build_product_activity_read_model(
    *, record_store: ProductReadModelStore, product: str, limit: int = 50
) -> ProductActivityReadModel:
    profile = record_store.read_product_profile_record(product)
    source_limit = max(limit, 0)
    events: list[ProductActivityEvent] = []
    for lane in _product_activity_lanes(profile):
        events.extend(
            _deployment_activity_events(
                record_store=record_store,
                profile=profile,
                lane=lane,
                source_limit=source_limit,
            )
        )
        events.extend(
            _promotion_activity_events(
                record_store=record_store,
                profile=profile,
                lane=lane,
                source_limit=source_limit,
            )
        )
        events.extend(
            _backup_gate_activity_events(
                record_store=record_store,
                profile=profile,
                lane=lane,
                source_limit=source_limit,
            )
        )
    for preview_context in _product_activity_preview_contexts(profile):
        events.extend(
            _preview_activity_events(
                record_store=record_store,
                profile=profile,
                preview_context=preview_context,
                source_limit=source_limit,
            )
        )
        events.extend(
            _preview_context_activity_events(
                record_store=record_store,
                profile=profile,
                preview_context=preview_context,
                source_limit=source_limit,
            )
        )
    events.extend(
        _authz_policy_activity_events(
            record_store=record_store,
            profile=profile,
            source_limit=source_limit,
        )
    )
    events.sort(key=lambda event: (event.occurred_at, event.event_id), reverse=True)
    return ProductActivityReadModel(
        product=profile.product,
        display_name=profile.display_name,
        repository=profile.repository,
        driver_id=profile.driver_id,
        events=tuple(events[:limit]),
    )


def _read_profile_descriptor(
    profile: LaunchplaneProductProfileRecord,
) -> tuple[DriverDescriptor | None, str]:
    try:
        return read_driver_descriptor(profile.driver_id), ""
    except FileNotFoundError:
        return None, f"Product driver {profile.driver_id!r} is not registered in Launchplane."


def _optional_records(
    record_store: object, method_name: str, **kwargs: object
) -> tuple[object, ...]:
    method = getattr(record_store, method_name, None)
    if not callable(method):
        return ()
    return tuple(method(**kwargs))


def _required_records(
    record_store: object, method_name: str, **kwargs: object
) -> tuple[object, ...]:
    method = getattr(record_store, method_name, None)
    if not callable(method):
        raise ProductEnvironmentReadModelCapabilityError(
            "Product environment reads require DB-backed Launchplane storage; "
            f"missing store method(s): {method_name}."
        )
    return tuple(method(**kwargs))


def _record_link(record_type: str, record_id: str) -> ProductActivityRecordLink:
    return ProductActivityRecordLink(record_type=record_type, record_id=record_id)


def _event_trust_state(status: str) -> FreshnessStatus:
    if status in {"pass", "ready", "active", "configured"}:
        return "recorded"
    if status in {"destroyed", "skipped", "superseded"}:
        return "recorded"
    if status in {"pending", "failed", "fail", "blocked"}:
        return "recorded"
    return "recorded"


def _activity_event(
    *,
    event_type: str,
    product: str,
    context: str,
    environment: str,
    driver_id: str,
    action_id: str,
    status: str,
    occurred_at: str,
    title: str,
    summary: str = "",
    records: tuple[ProductActivityRecordLink, ...] = (),
) -> ProductActivityEvent:
    record_key = records[0].record_id if records else f"{context}:{environment}:{occurred_at}"
    return ProductActivityEvent(
        event_id=f"{event_type}:{record_key}",
        event_type=event_type,
        product=product,
        context=context,
        environment=environment,
        driver_id=driver_id,
        action_id=action_id,
        status=status,
        occurred_at=occurred_at,
        title=title,
        summary=summary,
        records=records,
        trust_state=_event_trust_state(status),
    )


def _lane_action_id(*, profile: LaunchplaneProductProfileRecord, lane: ProductLaneProfile) -> str:
    if profile.driver_id == "verireel" and lane.instance == "testing":
        return "testing_deploy"
    if profile.driver_id == "verireel" and lane.instance == "prod":
        return "prod_deploy"
    return "stable_deploy"


def _product_activity_lanes(
    profile: LaunchplaneProductProfileRecord,
) -> tuple[ProductLaneProfile, ...]:
    lanes: list[ProductLaneProfile] = []
    seen_routes: set[tuple[str, str]] = set()
    for lane in profile.lanes:
        for context in (lane.context, *profile.historical_contexts):
            normalized_context = context.strip()
            route = (normalized_context, lane.instance)
            if not normalized_context or route in seen_routes:
                continue
            seen_routes.add(route)
            lanes.append(lane.model_copy(update={"context": normalized_context}))
    return tuple(lanes)


def _product_activity_preview_contexts(profile: LaunchplaneProductProfileRecord) -> tuple[str, ...]:
    contexts: list[str] = []
    for context in (profile.preview.context, *profile.historical_contexts):
        normalized_context = context.strip()
        if normalized_context and normalized_context not in contexts:
            contexts.append(normalized_context)
    return tuple(contexts)


def _deployment_activity_events(
    *,
    record_store: object,
    profile: LaunchplaneProductProfileRecord,
    lane: ProductLaneProfile,
    source_limit: int,
) -> tuple[ProductActivityEvent, ...]:
    events: list[ProductActivityEvent] = []
    for record in _optional_records(
        record_store,
        "list_deployment_records",
        context_name=lane.context,
        instance_name=lane.instance,
        limit=source_limit,
    ):
        deploy = getattr(record, "deploy")
        occurred_at = deploy.finished_at or deploy.started_at
        events.append(
            _activity_event(
                event_type="deployment",
                product=profile.product,
                context=lane.context,
                environment=lane.instance,
                driver_id=profile.driver_id,
                action_id=_lane_action_id(profile=profile, lane=lane),
                status=str(deploy.status),
                occurred_at=occurred_at,
                title=f"{profile.display_name} {lane.instance} deployment",
                summary=f"Deployment {deploy.status} for {lane.context}/{lane.instance}.",
                records=(_record_link("deployment", str(getattr(record, "record_id"))),),
            )
        )
    return tuple(events)


def _promotion_activity_events(
    *,
    record_store: object,
    profile: LaunchplaneProductProfileRecord,
    lane: ProductLaneProfile,
    source_limit: int,
) -> tuple[ProductActivityEvent, ...]:
    events: list[ProductActivityEvent] = []
    for record in _optional_records(
        record_store,
        "list_promotion_records",
        context_name=lane.context,
        to_instance_name=lane.instance,
        limit=source_limit,
    ):
        deploy = getattr(record, "deploy")
        rollback = getattr(record, "rollback")
        rollback_attempted = bool(getattr(rollback, "attempted", False))
        occurred_at = (
            rollback.finished_at or rollback.started_at
            if rollback_attempted
            else deploy.finished_at or deploy.started_at
        )
        action_id = "prod_rollback" if rollback_attempted else "prod_promotion"
        status = str(rollback.status if rollback_attempted else deploy.status)
        record_links = [_record_link("promotion", str(getattr(record, "record_id")))]
        deployment_record_id = str(getattr(record, "deployment_record_id", "") or "")
        backup_record_id = str(getattr(record, "backup_record_id", "") or "")
        if deployment_record_id:
            record_links.append(_record_link("deployment", deployment_record_id))
        if backup_record_id:
            record_links.append(_record_link("backup_gate", backup_record_id))
        events.append(
            _activity_event(
                event_type="rollback" if rollback_attempted else "promotion",
                product=profile.product,
                context=lane.context,
                environment=lane.instance,
                driver_id=profile.driver_id,
                action_id=action_id,
                status=status,
                occurred_at=occurred_at,
                title=f"{profile.display_name} {lane.instance} {action_id.replace('_', ' ')}",
                summary=(
                    f"{getattr(record, 'from_instance')} to "
                    f"{getattr(record, 'to_instance')} {status}."
                ),
                records=tuple(record_links),
            )
        )
    return tuple(events)


def _backup_gate_activity_events(
    *,
    record_store: object,
    profile: LaunchplaneProductProfileRecord,
    lane: ProductLaneProfile,
    source_limit: int,
) -> tuple[ProductActivityEvent, ...]:
    events: list[ProductActivityEvent] = []
    for record in _optional_records(
        record_store,
        "list_backup_gate_records",
        context_name=lane.context,
        instance_name=lane.instance,
        limit=source_limit,
    ):
        events.append(
            _activity_event(
                event_type="backup_gate",
                product=profile.product,
                context=lane.context,
                environment=lane.instance,
                driver_id=profile.driver_id,
                action_id="prod_backup_gate",
                status=str(getattr(record, "status")),
                occurred_at=str(getattr(record, "created_at")),
                title=f"{profile.display_name} {lane.instance} backup gate",
                summary=f"Backup gate {getattr(record, 'status')} for {lane.context}/{lane.instance}.",
                records=(_record_link("backup_gate", str(getattr(record, "record_id"))),),
            )
        )
    return tuple(events)


def _preview_activity_events(
    *,
    record_store: object,
    profile: LaunchplaneProductProfileRecord,
    preview_context: str,
    source_limit: int,
) -> tuple[ProductActivityEvent, ...]:
    events: list[ProductActivityEvent] = []
    for record in _optional_records(
        record_store,
        "list_preview_records",
        context_name=preview_context,
        anchor_repo=_profile_anchor_repo(profile),
        limit=source_limit,
    ):
        state = str(getattr(record, "state"))
        action_id = "preview_destroy" if state == "destroyed" else "preview_refresh"
        events.append(
            _activity_event(
                event_type="preview",
                product=profile.product,
                context=preview_context,
                environment="preview",
                driver_id=profile.driver_id,
                action_id=action_id,
                status=state,
                occurred_at=str(getattr(record, "updated_at")),
                title=f"{profile.display_name} preview {state}",
                summary=str(getattr(record, "preview_label")),
                records=(_record_link("preview", str(getattr(record, "preview_id"))),),
            )
        )
    return tuple(events)


def _preview_context_activity_events(
    *,
    record_store: object,
    profile: LaunchplaneProductProfileRecord,
    preview_context: str,
    source_limit: int,
) -> tuple[ProductActivityEvent, ...]:
    events: list[ProductActivityEvent] = []
    for record in _optional_records(
        record_store,
        "list_preview_desired_state_records",
        context_name=preview_context,
        limit=source_limit,
    ):
        if getattr(record, "product", "") != profile.product:
            continue
        events.append(
            _activity_event(
                event_type="preview_desired_state",
                product=profile.product,
                context=preview_context,
                environment="preview",
                driver_id=profile.driver_id,
                action_id="preview_desired_state",
                status=str(getattr(record, "status")),
                occurred_at=str(getattr(record, "discovered_at")),
                title=f"{profile.display_name} desired previews discovered",
                summary=f"{getattr(record, 'desired_count')} desired preview(s).",
                records=(
                    _record_link("preview_desired_state", str(getattr(record, "desired_state_id"))),
                ),
            )
        )
    for record in _optional_records(
        record_store,
        "list_preview_lifecycle_cleanup_records",
        context_name=preview_context,
        limit=source_limit,
    ):
        if getattr(record, "product", "") != profile.product:
            continue
        events.append(
            _activity_event(
                event_type="preview_cleanup",
                product=profile.product,
                context=preview_context,
                environment="preview",
                driver_id=profile.driver_id,
                action_id="preview_destroy",
                status=str(getattr(record, "status")),
                occurred_at=str(getattr(record, "requested_at")),
                title=f"{profile.display_name} preview cleanup",
                records=(
                    _record_link("preview_lifecycle_cleanup", str(getattr(record, "cleanup_id"))),
                ),
            )
        )
    for record in _optional_records(
        record_store,
        "list_preview_pr_feedback_records",
        context_name=preview_context,
        limit=source_limit,
    ):
        if getattr(record, "product", "") != profile.product:
            continue
        events.append(
            _activity_event(
                event_type="preview_pr_feedback",
                product=profile.product,
                context=preview_context,
                environment="preview",
                driver_id=profile.driver_id,
                action_id="preview_pr_feedback",
                status=str(getattr(record, "status")),
                occurred_at=str(getattr(record, "requested_at")),
                title=f"{profile.display_name} preview PR feedback",
                records=(_record_link("preview_pr_feedback", str(getattr(record, "feedback_id"))),),
            )
        )
    return tuple(events)


def _authz_policy_mentions_product(record: object, product: str) -> bool:
    policy = getattr(record, "policy", None)
    if policy is None:
        return False
    rules = (*getattr(policy, "github_actions", ()), *getattr(policy, "github_humans", ()))
    return any(product in getattr(rule, "products", ()) for rule in rules)


def _authz_policy_activity_events(
    *, record_store: object, profile: LaunchplaneProductProfileRecord, source_limit: int
) -> tuple[ProductActivityEvent, ...]:
    events: list[ProductActivityEvent] = []
    for record in _optional_records(record_store, "list_authz_policy_records", limit=source_limit):
        if not _authz_policy_mentions_product(record, profile.product):
            continue
        events.append(
            _activity_event(
                event_type="authz_policy",
                product=profile.product,
                context="launchplane",
                environment="",
                driver_id="launchplane",
                action_id="authz_policy.update",
                status=str(getattr(record, "status")),
                occurred_at=str(getattr(record, "updated_at")),
                title=f"{profile.display_name} authorization policy updated",
                summary=str(getattr(record, "source")),
                records=(_record_link("authz_policy", str(getattr(record, "record_id"))),),
            )
        )
    return tuple(events)


def _profile_provenance(profile: LaunchplaneProductProfileRecord) -> DataProvenance:
    return DataProvenance(
        source_kind="record",
        source_record_id=profile.product,
        recorded_at=profile.updated_at,
        refreshed_at=profile.updated_at,
        freshness_status="recorded",
        detail="Launchplane product profile record.",
    )


def _first_lane_context(profile: LaunchplaneProductProfileRecord) -> str:
    first_lane = next(iter(profile.lanes), None)
    return first_lane.context if first_lane is not None else ""


def _profile_anchor_repo(profile: LaunchplaneProductProfileRecord) -> str:
    _owner, separator, repo = profile.repository.strip().partition("/")
    if separator and repo.strip() and "/" not in repo.strip():
        return repo.strip()
    return profile.repository.strip()


def _lane_context_for_instance(
    *,
    profile: LaunchplaneProductProfileRecord,
    preferred_instances: tuple[str, ...],
) -> str:
    for preferred_instance in preferred_instances:
        for lane in profile.lanes:
            if lane.instance == preferred_instance and lane.context.strip():
                return lane.context
    return ""


def _lane_context_if_present(*, profile: LaunchplaneProductProfileRecord, instance: str) -> str:
    for lane in profile.lanes:
        if lane.instance == instance and lane.context.strip():
            return lane.context
    return ""


def _generic_web_prod_promotion_supported(profile: LaunchplaneProductProfileRecord) -> bool:
    testing_context = _lane_context_if_present(profile=profile, instance="testing")
    prod_context = _lane_context_if_present(profile=profile, instance="prod")
    return bool(testing_context and prod_context and testing_context == prod_context)


def _prod_lane_supported(profile: LaunchplaneProductProfileRecord) -> bool:
    return bool(_lane_context_if_present(profile=profile, instance="prod"))


def _product_action_authorization_context(
    *, profile: LaunchplaneProductProfileRecord, action: DriverActionDescriptor
) -> str:
    if action.scope == "preview" or action.action_id.startswith("preview_"):
        preview_context = profile.preview.context.strip()
        if preview_context:
            return preview_context
        return _lane_context_for_instance(profile=profile, preferred_instances=("prod", "testing"))
    if action.action_id in {"testing_deploy", "testing_verification"}:
        return _lane_context_for_instance(profile=profile, preferred_instances=("testing", "prod"))
    if action.route_path == "/v1/drivers/generic-web/prod-promotion":
        if _generic_web_prod_promotion_supported(profile):
            return _lane_context_if_present(profile=profile, instance="prod")
        return ""
    if action.route_path == "/v1/drivers/generic-web/prod-promotion-workflow":
        return _lane_context_for_instance(profile=profile, preferred_instances=("testing", "prod"))
    if action.route_path in {
        "/v1/drivers/generic-web/prod-rollback-plan",
        "/v1/drivers/generic-web/prod-rollback",
    }:
        return _lane_context_if_present(profile=profile, instance="prod")
    if action.route_path in {
        "/v1/drivers/odoo/prod-backup-gate",
        "/v1/drivers/odoo/prod-promotion",
        "/v1/drivers/odoo/prod-rollback",
        "/v1/drivers/verireel/prod-deploy",
        "/v1/drivers/verireel/prod-backup-gate",
        "/v1/drivers/verireel/prod-promotion",
        "/v1/drivers/verireel/prod-rollback",
    }:
        if _prod_lane_supported(profile):
            return _lane_context_if_present(profile=profile, instance="prod")
        return ""
    if action.action_id == "prod_promotion_workflow":
        return _lane_context_for_instance(profile=profile, preferred_instances=("prod", "testing"))
    if action.action_id == "prod_promotion":
        return _lane_context_if_present(profile=profile, instance="prod")
    if action.action_id == "prod_backup_gate" or action.action_id == "prod_rollback":
        return _lane_context_if_present(profile=profile, instance="prod")
    if action.action_id in {"stable_environment", "runtime_verification", "app_maintenance"}:
        return _lane_context_for_instance(profile=profile, preferred_instances=("prod", "testing"))
    if action.action_id == "stable_deploy":
        return _lane_context_for_instance(profile=profile, preferred_instances=("testing", "prod"))
    if action.scope == "context":
        preview_context = profile.preview.context.strip()
        if preview_context and profile.preview.enabled:
            return preview_context
        return ""
    if action.scope == "instance":
        return _lane_context_for_instance(profile=profile, preferred_instances=("prod", "testing"))
    return ""


def _product_action_context_resolver(
    *, profile: LaunchplaneProductProfileRecord
) -> Callable[[DriverActionDescriptor], str]:
    def resolve(action: DriverActionDescriptor) -> str:
        return _product_action_authorization_context(profile=profile, action=action)

    return resolve


def _lane_context_resolver(*, context: str) -> Callable[[DriverActionDescriptor], str]:
    def resolve(_action: DriverActionDescriptor) -> str:
        return context

    return resolve


def _find_lane(*, profile: LaunchplaneProductProfileRecord, environment: str) -> ProductLaneProfile:
    normalized_environment = environment.strip()
    for lane in profile.lanes:
        if lane.instance == normalized_environment:
            return lane
    raise FileNotFoundError(
        f"Product {profile.product!r} has no environment {normalized_environment!r}."
    )


def _build_environment_summary(
    *,
    record_store: ProductReadModelStore,
    profile: LaunchplaneProductProfileRecord,
    descriptor: DriverDescriptor | None,
    lane: ProductLaneProfile,
    action_allowed: ActionAllowed,
) -> ProductEnvironmentSummary:
    lane_summary = _read_product_lane_summary(
        record_store=record_store,
        profile=profile,
        lane=lane,
    )
    provenance = (
        lane_summary.provenance if lane_summary is not None else _missing_lane_provenance(lane)
    )
    return ProductEnvironmentSummary(
        environment=lane.instance,
        context=lane.context,
        base_url=lane.base_url,
        health_url=lane.health_url,
        prelaunch_rebuild_allowed=lane.odoo_prelaunch_rebuild.enabled,
        prelaunch_rebuild_data_source_mode=lane.odoo_prelaunch_rebuild.data_source_mode
        if lane.odoo_prelaunch_rebuild.enabled
        else "",
        prelaunch_rebuild_approval_issue_url=lane.odoo_prelaunch_rebuild.approval_issue_url,
        odoo_data_authority=lane.odoo_data_policy.data_authority,
        odoo_allowed_rebuild_sources=lane.odoo_data_policy.allowed_rebuild_sources,
        odoo_upstream_source=lane.odoo_data_policy.upstream_source,
        odoo_requires_backup_before_destroy=lane.odoo_data_policy.requires_backup_before_destroy,
        odoo_requires_restore_proof=lane.odoo_data_policy.requires_restore_proof,
        odoo_requires_runtime_identity=lane.odoo_data_policy.requires_runtime_identity,
        public_ingress=_public_ingress_summary(
            record_store=record_store,
            profile=profile,
            lane=lane,
        ),
        trust_state=provenance.freshness_status,
        provenance=provenance,
        available_actions=_action_availability(
            descriptor=descriptor,
            profile=profile,
            product=profile.product,
            previews_enabled=profile.preview.enabled,
            action_allowed=action_allowed,
            include_unsupported=False,
            context_resolver=_lane_context_resolver(context=lane.context),
        ),
    )


def _read_product_lane_summary(
    *,
    record_store: object,
    profile: LaunchplaneProductProfileRecord,
    lane: ProductLaneProfile,
) -> LaunchplaneLaneSummary | None:
    view = build_driver_context_view(
        record_store=record_store,
        context_name=lane.context,
        instance_name=lane.instance,
    )
    for driver in view.drivers:
        if driver.descriptor.product == profile.product or driver.driver_id == profile.driver_id:
            return driver.lane_summary
    return None


def _missing_lane_provenance(lane: ProductLaneProfile) -> DataProvenance:
    return DataProvenance(
        source_kind="record",
        freshness_status="missing",
        detail=f"Launchplane has not recorded lane evidence for {lane.context}/{lane.instance}.",
    )


def _build_preview_summary(
    *, record_store: object, profile: LaunchplaneProductProfileRecord
) -> ProductPreviewSummary:
    if not profile.preview.enabled:
        return ProductPreviewSummary(enabled=False)
    summaries: tuple[LaunchplanePreviewSummary | PreviewRecord, ...] = ()
    list_preview_summaries = getattr(record_store, "list_preview_summaries", None)
    list_preview_records = getattr(record_store, "list_preview_records", None)
    anchor_repo = _profile_anchor_repo(profile)
    if callable(list_preview_summaries):
        summaries = list_preview_summaries(
            context_name=profile.preview.context,
            anchor_repo=anchor_repo,
            preview_limit=None,
            generation_limit=1,
        )
    elif callable(list_preview_records):
        summaries = tuple(
            list_preview_records(
                context_name=profile.preview.context,
                anchor_repo=anchor_repo,
                limit=None,
            )
        )
    filtered_summaries: list[LaunchplanePreviewSummary | PreviewRecord] = []
    for summary in summaries:
        if isinstance(summary, LaunchplanePreviewSummary):
            preview = summary.preview
        else:
            preview = summary
        if preview.context != profile.preview.context:
            continue
        if preview.anchor_repo != anchor_repo:
            continue
        if preview.state == "destroyed":
            continue
        filtered_summaries.append(summary)
    summaries = tuple(filtered_summaries)
    latest_summary = next(iter(summaries), None)
    latest_preview_id = ""
    provenance = DataProvenance(
        source_kind="record",
        freshness_status="missing",
        detail="Launchplane has not recorded previews for this product profile.",
    )
    if latest_summary is not None:
        if isinstance(latest_summary, LaunchplanePreviewSummary):
            preview = latest_summary.preview
            provenance = latest_summary.provenance
        else:
            preview = latest_summary
            provenance = DataProvenance(
                source_kind="record",
                source_record_id=preview.preview_id,
                recorded_at=preview.updated_at,
                refreshed_at=preview.updated_at,
                freshness_status="recorded",
                detail="Launchplane preview identity record.",
            )
        latest_preview_id = preview.preview_id
    return ProductPreviewSummary(
        enabled=True,
        context=profile.preview.context,
        slug_template=profile.preview.slug_template,
        active_count=len(summaries),
        latest_preview_id=latest_preview_id,
        trust_state=provenance.freshness_status,
        provenance=provenance,
    )


def _public_ingress_summary(
    *,
    record_store: object,
    profile: LaunchplaneProductProfileRecord,
    lane: ProductLaneProfile,
) -> ProductPublicIngressSummary:
    records = _optional_records(
        record_store,
        "list_public_ingress_observation_records",
        product=profile.product,
        context_name=lane.context,
        instance_name=lane.instance,
        check_kind="public_http",
        limit=1,
    )
    latest = next(iter(records), None)
    if latest is None or not isinstance(latest, PublicIngressObservationRecord):
        return ProductPublicIngressSummary()
    incidents = _required_records(
        record_store,
        "list_public_ingress_incident_records",
        product=profile.product,
        context_name=lane.context,
        instance_name=lane.instance,
        check_kind="public_http",
        status="open",
        limit=1,
    )
    open_incident = next(
        (incident for incident in incidents if isinstance(incident, PublicIngressIncidentRecord)),
        None,
    )
    provenance = DataProvenance(
        source_kind="record",
        source_record_id=latest.record_id,
        recorded_at=latest.observed_at,
        refreshed_at=latest.observed_at,
        freshness_status=_public_ingress_freshness(latest.status),
        detail="Launchplane public ingress synthetic observation.",
    )
    return ProductPublicIngressSummary(
        status=latest.status,
        failure_code=latest.failure_code or "",
        observed_at=latest.observed_at,
        record_id=latest.record_id,
        summary=latest.summary,
        notification_sent=latest.notification_sent,
        incident_status=open_incident.status if open_incident is not None else "",
        incident_id=open_incident.incident_id if open_incident is not None else "",
        incident_opened_at=open_incident.opened_at if open_incident is not None else "",
        trust_state=provenance.freshness_status,
        provenance=provenance,
    )


def _public_ingress_freshness(status: str) -> FreshnessStatus:
    if status == "pass":
        return "verified"
    if status == "fail":
        return "stale"
    if status == "skipped":
        return "unsupported"
    return "missing"


def _action_availability(
    *,
    descriptor: DriverDescriptor | None,
    profile: LaunchplaneProductProfileRecord,
    product: str,
    previews_enabled: bool,
    action_allowed: ActionAllowed,
    include_unsupported: bool,
    context_resolver: Callable[[DriverActionDescriptor], str],
) -> tuple[ProductActionAvailability, ...]:
    descriptor_actions = {
        action.action_id: action
        for action in (effective_driver_actions(descriptor) if descriptor is not None else ())
        if action.operator_visible
    }
    action_ids = tuple(descriptor_actions)
    if include_unsupported:
        action_ids = tuple(dict.fromkeys((*action_ids, *OPERATOR_ACTION_IDS)))
    availability = []
    for action_id in action_ids:
        descriptor_action = descriptor_actions.get(action_id)
        if descriptor_action is None:
            label, safety, scope = OPERATOR_ACTION_IDS[action_id]
            availability.append(
                ProductActionAvailability(
                    action_id=action_id,
                    label=label,
                    safety=safety,
                    scope=scope,
                    enabled=False,
                    disabled_reasons=("Driver does not support this action.",),
                    trust_state="unsupported",
                )
            )
            continue
        availability.append(
            _availability_for_descriptor_action(
                action=descriptor_action,
                profile=profile,
                product=product,
                authorization_context=context_resolver(descriptor_action),
                previews_enabled=previews_enabled,
                action_allowed=action_allowed,
            )
        )
    return tuple(availability)


def _availability_for_descriptor_action(
    *,
    action: DriverActionDescriptor,
    profile: LaunchplaneProductProfileRecord,
    product: str,
    authorization_context: str,
    previews_enabled: bool,
    action_allowed: ActionAllowed,
) -> ProductActionAvailability:
    disabled_reasons: list[str] = []
    if not previews_enabled and (
        action.scope == "preview" or action.action_id in PREVIEW_PROFILE_REQUIRED_ACTION_IDS
    ):
        disabled_reasons.append("Product previews are not enabled.")
    support_reason = _action_support_reason(profile=profile, action=action)
    if support_reason:
        disabled_reasons.append(support_reason)
    authz_action = action.authz_action or ACTION_AUTHZ_BY_ROUTE.get(
        action.route_path, action.action_id
    )
    if not action_allowed(authz_action, product, authorization_context):
        disabled_reasons.append("Caller is not authorized for this action.")
    return ProductActionAvailability(
        action_id=action.action_id,
        label=action.label,
        description=action.description,
        safety=action.safety,
        scope=action.scope,
        method=action.method,
        route_path=action.route_path,
        authz_action=authz_action,
        alternate_authz_actions=action.alternate_authz_actions,
        enabled=not disabled_reasons,
        disabled_reasons=tuple(disabled_reasons),
        trust_state="recorded",
    )


def _action_support_reason(
    *, profile: LaunchplaneProductProfileRecord, action: DriverActionDescriptor
) -> str:
    if action.route_path == "/v1/drivers/generic-web/prod-promotion":
        if not _generic_web_prod_promotion_supported(profile):
            return "Generic web prod promotion requires testing and prod lanes to share a context."
        return ""
    if action.route_path in {
        "/v1/drivers/generic-web/prod-promotion-workflow",
        "/v1/drivers/generic-web/prod-rollback-plan",
        "/v1/drivers/generic-web/prod-rollback",
        "/v1/drivers/odoo/prod-backup-gate",
        "/v1/drivers/odoo/prod-promotion",
        "/v1/drivers/odoo/prod-rollback",
        "/v1/drivers/verireel/prod-deploy",
        "/v1/drivers/verireel/prod-backup-gate",
        "/v1/drivers/verireel/prod-promotion",
        "/v1/drivers/verireel/prod-rollback",
    }:
        if not _prod_lane_supported(profile):
            return "Product profile does not define a prod lane."
    return ""


def _runtime_setting_summaries(
    lane_summary: LaunchplaneLaneSummary | None,
) -> tuple[ProductRuntimeSettingSummary, ...]:
    if lane_summary is None:
        return ()
    return tuple(
        _runtime_setting_summary(record) for record in lane_summary.runtime_environment_records
    )


def _runtime_setting_summary(record: RuntimeEnvironmentRecord) -> ProductRuntimeSettingSummary:
    return ProductRuntimeSettingSummary(
        scope=record.scope,
        context=record.context,
        instance=record.instance,
        env_keys=tuple(sorted(record.env.keys())),
        env_value_count=len(record.env),
        updated_at=record.updated_at,
        source_label=record.source_label,
    )


def _secret_binding_summaries(
    lane_summary: LaunchplaneLaneSummary | None,
) -> tuple[ProductSecretBindingSummary, ...]:
    if lane_summary is None:
        return ()
    return tuple(_secret_binding_summary(binding) for binding in lane_summary.secret_bindings)


def _secret_binding_summary(binding: SecretBinding) -> ProductSecretBindingSummary:
    return ProductSecretBindingSummary(
        binding_id=binding.binding_id,
        secret_id=binding.secret_id,
        integration=binding.integration,
        binding_type=binding.binding_type,
        binding_key=binding.binding_key,
        context=binding.context,
        instance=binding.instance,
        status=binding.status,
        updated_at=binding.updated_at,
        trust_state="recorded" if binding.status == "configured" else "disabled",
    )


def _runtime_config_status_items(
    *,
    requirements: tuple[ProductRuntimeConfigRequirement, ...],
    lane: ProductLaneProfile,
    lane_summary: LaunchplaneLaneSummary | None,
) -> tuple[ProductRuntimeConfigStatusItem, ...]:
    applicable_requirements = tuple(
        requirement
        for requirement in requirements
        if _config_requirement_applies(
            requirement_context=requirement.context,
            requirement_instance=requirement.instance,
            lane=lane,
        )
    )
    if not applicable_requirements:
        return ()
    records = lane_summary.runtime_environment_records if lane_summary is not None else ()
    return tuple(
        _runtime_config_status_item(requirement=requirement, records=records, lane=lane)
        for requirement in applicable_requirements
    )


def _runtime_config_status_item(
    *,
    requirement: ProductRuntimeConfigRequirement,
    records: tuple[RuntimeEnvironmentRecord, ...],
    lane: ProductLaneProfile,
) -> ProductRuntimeConfigStatusItem:
    matching_record = next(
        (record for record in records if _runtime_record_provides_key(record, requirement.key)),
        None,
    )
    context = requirement.context or lane.context
    instance = requirement.instance or (lane.instance if requirement.context else "")
    if matching_record is None:
        return ProductRuntimeConfigStatusItem(
            key=requirement.key,
            status="missing",
            context=context,
            instance=instance,
            trust_state="missing",
        )
    return ProductRuntimeConfigStatusItem(
        key=requirement.key,
        status="configured",
        context=matching_record.context,
        instance=matching_record.instance,
        source_label=matching_record.source_label,
        updated_at=matching_record.updated_at,
        trust_state="recorded",
    )


def _runtime_record_provides_key(record: RuntimeEnvironmentRecord, key: str) -> bool:
    return key in record.env


def _managed_secret_config_status_items(
    *,
    requirements: tuple[ProductSecretConfigRequirement, ...],
    lane: ProductLaneProfile,
    lane_summary: LaunchplaneLaneSummary | None,
) -> tuple[ProductManagedSecretConfigStatusItem, ...]:
    applicable_requirements = tuple(
        requirement
        for requirement in requirements
        if _config_requirement_applies(
            requirement_context=requirement.context,
            requirement_instance=requirement.instance,
            lane=lane,
        )
    )
    if not applicable_requirements:
        return ()
    bindings = lane_summary.secret_bindings if lane_summary is not None else ()
    return tuple(
        _managed_secret_config_status_item(
            requirement=requirement,
            bindings=bindings,
            lane=lane,
        )
        for requirement in applicable_requirements
    )


def _managed_secret_config_status_item(
    *,
    requirement: ProductSecretConfigRequirement,
    bindings: tuple[SecretBinding, ...],
    lane: ProductLaneProfile,
) -> ProductManagedSecretConfigStatusItem:
    matching_binding = next(
        (
            binding
            for binding in bindings
            if binding.integration == requirement.integration
            and binding.binding_key == requirement.binding_key
        ),
        None,
    )
    context = requirement.context or lane.context
    instance = requirement.instance or (lane.instance if requirement.context else "")
    if matching_binding is None:
        return ProductManagedSecretConfigStatusItem(
            binding_key=requirement.binding_key,
            status="missing",
            integration=requirement.integration,
            context=context,
            instance=instance,
            trust_state="missing",
        )
    if matching_binding.status == "disabled":
        status: ProductConfigItemStatus = "disabled"
        trust_state: ProductSecretBindingTrustState = "disabled"
    else:
        status = "configured"
        trust_state = "recorded"
    return ProductManagedSecretConfigStatusItem(
        binding_key=requirement.binding_key,
        status=status,
        integration=matching_binding.integration,
        context=matching_binding.context,
        instance=matching_binding.instance,
        updated_at=matching_binding.updated_at,
        trust_state=trust_state,
    )


def _config_item_freshness(status: ProductConfigItemStatus) -> FreshnessStatus:
    if status == "configured":
        return "recorded"
    if status in {"disabled", "unvalidated"}:
        return "missing"
    if status == "stale":
        return "stale"
    if status == "unsupported":
        return "unsupported"
    return "missing"


def _config_requirement_applies(
    *, requirement_context: str, requirement_instance: str, lane: ProductLaneProfile
) -> bool:
    if not requirement_context:
        return True
    if requirement_context != lane.context:
        return False
    return not requirement_instance or requirement_instance == lane.instance


def _target_summary(lane_summary: LaunchplaneLaneSummary | None) -> ProductTargetSummary:
    if lane_summary is None:
        return ProductTargetSummary()
    expected_identity = None
    destination_health = None
    if lane_summary.inventory is not None:
        expected_identity = lane_summary.inventory.runtime_identity
        destination_health = lane_summary.inventory.destination_health
    elif lane_summary.latest_deployment is not None:
        expected_identity = lane_summary.latest_deployment.runtime_identity
        destination_health = lane_summary.latest_deployment.destination_health
    if lane_summary.provider_target is not None:
        return ProductTargetSummary(
            provider=lane_summary.provider_target.provider_id,
            target_type=lane_summary.provider_target.target_category,
            target_name=lane_summary.provider_target.display_name,
            target_id=lane_summary.provider_target.target_id,
            provider_target_type=lane_summary.provider_target.provider_target_type,
            target_id_recorded=True,
            artifact_manifest=lane_summary.artifact_manifest,
            expected_runtime_identity=expected_identity,
            observed_runtime_identity=destination_health.observed_runtime_identity
            if destination_health is not None
            else None,
            runtime_identity_status=destination_health.runtime_identity_status
            if destination_health is not None
            else "unchecked",
            runtime_identity_detail=destination_health.runtime_identity_detail
            if destination_health is not None
            else "",
            trust_state="recorded",
        )
    return ProductTargetSummary(
        artifact_manifest=lane_summary.artifact_manifest,
        expected_runtime_identity=expected_identity,
        observed_runtime_identity=destination_health.observed_runtime_identity
        if destination_health is not None
        else None,
        runtime_identity_status=destination_health.runtime_identity_status
        if destination_health is not None
        else "unchecked",
        runtime_identity_detail=destination_health.runtime_identity_detail
        if destination_health is not None
        else "",
        trust_state="missing",
    )


def _combine_trust_states(
    states: Iterable[FreshnessStatus], *, fallback: FreshnessStatus
) -> FreshnessStatus:
    ordered_states = tuple(state for state in states if state)
    if not ordered_states:
        return fallback
    priority: tuple[FreshnessStatus, ...] = (
        "missing",
        "stale",
        "recorded",
        "verified",
        "unsupported",
    )
    for status in priority:
        if status in ordered_states:
            return status
    return fallback
