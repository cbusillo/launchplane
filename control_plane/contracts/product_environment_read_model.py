from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from control_plane.contracts.data_provenance import DataProvenance, FreshnessStatus
from control_plane.contracts.driver_descriptor import DriverActionDescriptor, DriverDescriptor
from control_plane.contracts.lane_summary import LaunchplaneLaneSummary
from control_plane.contracts.preview_record import PreviewRecord
from control_plane.contracts.preview_summary import LaunchplanePreviewSummary
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductLaneProfile,
)
from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord
from control_plane.contracts.secret_record import SecretBinding
from control_plane.drivers.registry import build_driver_context_view, read_driver_descriptor


ActionAllowed = Callable[[str, str, str], bool]
ProductSecretBindingTrustState = FreshnessStatus | Literal["disabled"]


class ProductReadModelStore(Protocol):
    def list_product_profile_records(
        self,
        *,
        driver_id: str = "",
    ) -> tuple[LaunchplaneProductProfileRecord, ...]: ...

    def read_product_profile_record(self, product: str) -> LaunchplaneProductProfileRecord: ...


ACTION_AUTHZ_BY_ROUTE = {
    "/v1/drivers/generic-web/deploy": "generic_web_deploy.execute",
    "/v1/drivers/generic-web/prod-promotion": "generic_web_prod_promotion.execute",
    "/v1/drivers/generic-web/prod-promotion-workflow": "generic_web_prod_promotion.dispatch",
    "/v1/drivers/generic-web/preview-desired-state": "preview_desired_state.discover",
    "/v1/drivers/generic-web/preview-inventory": "preview_inventory.read",
    "/v1/drivers/generic-web/preview-refresh": "preview_refresh.execute",
    "/v1/drivers/generic-web/preview-readiness": "preview_readiness.evaluate",
    "/v1/drivers/generic-web/preview-destroy": "preview_destroy.execute",
    "/v1/drivers/odoo/artifact-publish-inputs": "odoo_artifact_publish_inputs.read",
    "/v1/drivers/odoo/artifact-publish": "odoo_artifact_publish.write",
    "/v1/drivers/odoo/post-deploy": "odoo_post_deploy.execute",
    "/v1/drivers/odoo/prod-backup-gate": "odoo_prod_backup_gate.execute",
    "/v1/drivers/odoo/prod-promotion": "odoo_prod_promotion.execute",
    "/v1/drivers/odoo/prod-rollback": "odoo_prod_rollback.execute",
    "/v1/drivers/verireel/testing-deploy": "verireel_testing_deploy.execute",
    "/v1/drivers/verireel/testing-verification": "deployment.write",
    "/v1/drivers/verireel/stable-environment": "verireel_stable_environment.read",
    "/v1/drivers/verireel/runtime-verification": "verireel_stable_environment.read",
    "/v1/drivers/verireel/app-maintenance": "verireel_app_maintenance.execute",
    "/v1/drivers/verireel/prod-deploy": "verireel_prod_deploy.execute",
    "/v1/drivers/verireel/prod-backup-gate": "verireel_prod_backup_gate.execute",
    "/v1/drivers/verireel/prod-promotion": "verireel_prod_promotion.execute",
    "/v1/drivers/verireel/prod-rollback": "verireel_prod_rollback.execute",
    "/v1/drivers/verireel/preview-refresh": "verireel_preview_refresh.execute",
    "/v1/drivers/verireel/preview-inventory": "verireel_preview_inventory.read",
    "/v1/drivers/verireel/preview-destroy": "verireel_preview_destroy.execute",
    "/v1/drivers/verireel/preview-verification": "preview_generation.write",
}

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
    target_id_recorded: bool = False
    trust_state: FreshnessStatus = "missing"


class ProductEnvironmentSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    environment: str
    context: str
    base_url: str = ""
    health_url: str = ""
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
    target: ProductTargetSummary
    runtime_settings: tuple[ProductRuntimeSettingSummary, ...] = ()
    managed_secrets: tuple[ProductSecretBindingSummary, ...] = ()
    available_actions: tuple[ProductActionAvailability, ...] = ()
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
        target=_target_summary(lane_summary),
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


def build_product_activity_read_model(
    *, record_store: ProductReadModelStore, product: str, limit: int = 50
) -> ProductActivityReadModel:
    profile = record_store.read_product_profile_record(product)
    events: list[ProductActivityEvent] = []
    for lane in profile.lanes:
        events.extend(
            _deployment_activity_events(record_store=record_store, profile=profile, lane=lane)
        )
        events.extend(
            _promotion_activity_events(record_store=record_store, profile=profile, lane=lane)
        )
        events.extend(
            _backup_gate_activity_events(record_store=record_store, profile=profile, lane=lane)
        )
    if profile.preview.context.strip():
        events.extend(_preview_activity_events(record_store=record_store, profile=profile))
        events.extend(_preview_context_activity_events(record_store=record_store, profile=profile))
    events.extend(_authz_policy_activity_events(record_store=record_store, profile=profile))
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


def _deployment_activity_events(
    *, record_store: object, profile: LaunchplaneProductProfileRecord, lane: ProductLaneProfile
) -> tuple[ProductActivityEvent, ...]:
    events: list[ProductActivityEvent] = []
    for record in _optional_records(
        record_store,
        "list_deployment_records",
        context_name=lane.context,
        instance_name=lane.instance,
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
    *, record_store: object, profile: LaunchplaneProductProfileRecord, lane: ProductLaneProfile
) -> tuple[ProductActivityEvent, ...]:
    events: list[ProductActivityEvent] = []
    for record in _optional_records(
        record_store,
        "list_promotion_records",
        context_name=lane.context,
        to_instance_name=lane.instance,
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
    *, record_store: object, profile: LaunchplaneProductProfileRecord, lane: ProductLaneProfile
) -> tuple[ProductActivityEvent, ...]:
    events: list[ProductActivityEvent] = []
    for record in _optional_records(
        record_store,
        "list_backup_gate_records",
        context_name=lane.context,
        instance_name=lane.instance,
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
    *, record_store: object, profile: LaunchplaneProductProfileRecord
) -> tuple[ProductActivityEvent, ...]:
    events: list[ProductActivityEvent] = []
    for record in _optional_records(
        record_store,
        "list_preview_records",
        context_name=profile.preview.context,
        anchor_repo=_profile_anchor_repo(profile),
    ):
        state = str(getattr(record, "state"))
        action_id = "preview_destroy" if state == "destroyed" else "preview_refresh"
        events.append(
            _activity_event(
                event_type="preview",
                product=profile.product,
                context=profile.preview.context,
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
    *, record_store: object, profile: LaunchplaneProductProfileRecord
) -> tuple[ProductActivityEvent, ...]:
    events: list[ProductActivityEvent] = []
    preview_context = profile.preview.context
    for record in _optional_records(
        record_store,
        "list_preview_desired_state_records",
        context_name=preview_context,
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
    *, record_store: object, profile: LaunchplaneProductProfileRecord
) -> tuple[ProductActivityEvent, ...]:
    events: list[ProductActivityEvent] = []
    for record in _optional_records(record_store, "list_authz_policy_records"):
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
    if action.route_path in {
        "/v1/drivers/generic-web/prod-promotion-workflow",
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
        for action in (descriptor.actions if descriptor is not None else ())
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
    authz_action = ACTION_AUTHZ_BY_ROUTE.get(action.route_path, action.action_id)
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


def _target_summary(lane_summary: LaunchplaneLaneSummary | None) -> ProductTargetSummary:
    if lane_summary is None or lane_summary.dokploy_target is None:
        return ProductTargetSummary()
    return ProductTargetSummary(
        target_type=lane_summary.dokploy_target.target_type,
        target_name=lane_summary.dokploy_target.target_name,
        target_id_recorded=lane_summary.dokploy_target_id is not None,
        trust_state="recorded",
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
