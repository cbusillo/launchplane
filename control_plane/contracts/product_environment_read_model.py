from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from control_plane.contracts.data_provenance import DataProvenance, FreshnessStatus
from control_plane.contracts.driver_descriptor import DriverActionDescriptor, DriverDescriptor
from control_plane.contracts.lane_summary import LaunchplaneLaneSummary
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductLaneProfile,
)
from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord
from control_plane.contracts.secret_record import SecretBinding
from control_plane.drivers.registry import build_driver_context_view, read_driver_descriptor


ActionAllowed = Callable[[str, str, str], bool]


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
    "/v1/drivers/odoo/artifact-publish": "odoo_artifact_publish.execute",
    "/v1/drivers/odoo/post-deploy": "odoo_post_deploy.execute",
    "/v1/drivers/odoo/prod-backup-gate": "odoo_prod_backup_gate.execute",
    "/v1/drivers/odoo/prod-promotion": "odoo_prod_promotion.execute",
    "/v1/drivers/odoo/prod-rollback": "odoo_prod_rollback.execute",
    "/v1/drivers/verireel/testing-deploy": "verireel_testing_deploy.execute",
    "/v1/drivers/verireel/testing-verification": "verireel_testing_verification.execute",
    "/v1/drivers/verireel/stable-environment": "verireel_stable_environment.read",
    "/v1/drivers/verireel/runtime-verification": "verireel_runtime_verification.evaluate",
    "/v1/drivers/verireel/app-maintenance": "verireel_app_maintenance.execute",
    "/v1/drivers/verireel/prod-deploy": "verireel_prod_deploy.execute",
    "/v1/drivers/verireel/prod-backup-gate": "verireel_prod_backup_gate.execute",
    "/v1/drivers/verireel/prod-promotion": "verireel_prod_promotion.execute",
    "/v1/drivers/verireel/prod-rollback": "verireel_prod_rollback.execute",
    "/v1/drivers/verireel/preview-refresh": "verireel_preview_refresh.execute",
    "/v1/drivers/verireel/preview-inventory": "verireel_preview_inventory.read",
    "/v1/drivers/verireel/preview-destroy": "verireel_preview_destroy.execute",
    "/v1/drivers/verireel/preview-verification": "verireel_preview_verification.write",
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
    trust_state: FreshnessStatus


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
    action_context = profile.preview.context or _first_lane_context(profile) or "launchplane"
    available_actions = _action_availability(
        descriptor=descriptor,
        product=profile.product,
        context=action_context,
        previews_enabled=profile.preview.enabled,
        action_allowed=action_allowed,
        include_unsupported=True,
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
            product=profile.product,
            context=lane.context,
            previews_enabled=profile.preview.enabled,
            action_allowed=action_allowed,
            include_unsupported=True,
        ),
        warnings=warnings,
        trust_state=provenance.freshness_status,
        provenance=provenance,
    )


def _read_profile_descriptor(
    profile: LaunchplaneProductProfileRecord,
) -> tuple[DriverDescriptor | None, str]:
    try:
        return read_driver_descriptor(profile.driver_id), ""
    except FileNotFoundError:
        return None, f"Product driver {profile.driver_id!r} is not registered in Launchplane."


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
            product=profile.product,
            context=lane.context,
            previews_enabled=profile.preview.enabled,
            action_allowed=action_allowed,
            include_unsupported=False,
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
    summaries = ()
    list_preview_summaries = getattr(record_store, "list_preview_summaries", None)
    list_preview_records = getattr(record_store, "list_preview_records", None)
    if callable(list_preview_summaries):
        summaries = list_preview_summaries(
            context_name=profile.preview.context,
            generation_limit=1,
        )
    elif callable(list_preview_records):
        summaries = tuple(list_preview_records(context_name=profile.preview.context, limit=10))
    latest = next(iter(summaries), None)
    latest_preview_id = ""
    provenance = DataProvenance(
        source_kind="record",
        freshness_status="missing",
        detail="Launchplane has not recorded previews for this product profile.",
    )
    if latest is not None:
        preview = getattr(latest, "preview", latest)
        latest_preview_id = preview.preview_id
        provenance = getattr(latest, "provenance", None) or DataProvenance(
            source_kind="record",
            source_record_id=preview.preview_id,
            recorded_at=preview.updated_at,
            refreshed_at=preview.updated_at,
            freshness_status="recorded",
            detail="Launchplane preview identity record.",
        )
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
    product: str,
    context: str,
    previews_enabled: bool,
    action_allowed: ActionAllowed,
    include_unsupported: bool,
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
                product=product,
                context=context,
                previews_enabled=previews_enabled,
                action_allowed=action_allowed,
            )
        )
    return tuple(availability)


def _availability_for_descriptor_action(
    *,
    action: DriverActionDescriptor,
    product: str,
    context: str,
    previews_enabled: bool,
    action_allowed: ActionAllowed,
) -> ProductActionAvailability:
    disabled_reasons: list[str] = []
    if action.scope == "preview" and not previews_enabled:
        disabled_reasons.append("Product previews are not enabled.")
    authz_action = ACTION_AUTHZ_BY_ROUTE.get(action.route_path, action.action_id)
    if not action_allowed(authz_action, product, context):
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
        trust_state="recorded" if binding.status == "configured" else "missing",
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
