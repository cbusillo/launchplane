from __future__ import annotations

from typing import Protocol, cast

from control_plane.contracts.artifact_identity import ArtifactIdentityManifest
from control_plane.contracts.driver_descriptor import DriverActionDescriptor
from control_plane.contracts.lane_summary import LaunchplaneLaneSummary
from control_plane.contracts.product_environment_read_model import (
    ProductEnvironmentReadModelStore,
    build_product_environment_config_status,
)
from control_plane.contracts.product_operational_readiness import (
    ProductOperationalReadiness,
    ProductOperationalReadinessInputs,
    build_product_operational_readiness,
)
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductLaneProfile,
)
from control_plane.contracts.product_topology_read_model import (
    build_product_environment_topology,
)
from control_plane.drivers.registry import (
    build_lane_summary_provenance,
    effective_driver_actions,
    read_driver_descriptor,
)
from control_plane.production_backup_authority import (
    require_production_backup_authority_store,
    resolve_production_backup_authority,
)
from control_plane.service_auth import LaunchplaneIdentity


class ProductOperationalReadinessStore(ProductEnvironmentReadModelStore, Protocol):
    def read_artifact_manifest(self, artifact_id: str) -> ArtifactIdentityManifest: ...


class ProductOperationalReadinessStoreCapabilityError(RuntimeError):
    pass


_PRODUCT_OPERATIONAL_READINESS_STORE_METHODS = (
    "list_product_profile_records",
    "read_product_profile_record",
    "read_lane_summary",
    "read_route_binding_record",
    "list_public_ingress_observation_records",
    "list_public_ingress_incident_records",
    "list_authz_policy_records",
    "read_artifact_manifest",
)


def require_product_operational_readiness_store(
    record_store: object,
) -> ProductOperationalReadinessStore:
    missing_methods = tuple(
        method_name
        for method_name in _PRODUCT_OPERATIONAL_READINESS_STORE_METHODS
        if not callable(getattr(record_store, method_name, None))
    )
    if missing_methods:
        missing = ", ".join(missing_methods)
        raise ProductOperationalReadinessStoreCapabilityError(
            "Operational readiness requires DB-backed Launchplane storage; "
            f"missing store method(s): {missing}."
        )
    return cast(ProductOperationalReadinessStore, record_store)


def resolve_product_operational_readiness_lane(
    *,
    record_store: ProductOperationalReadinessStore,
    product: str,
    context: str,
    instance: str,
) -> tuple[LaunchplaneProductProfileRecord, ProductLaneProfile]:
    try:
        profile = record_store.read_product_profile_record(product)
    except FileNotFoundError as error:
        raise FileNotFoundError(f"Product '{product}' was not found.") from error
    matching_lanes = tuple(
        lane for lane in profile.lanes if lane.context == context and lane.instance == instance
    )
    if len(matching_lanes) != 1:
        raise FileNotFoundError(f"Product '{product}' has no exact lane '{context}/{instance}'.")
    return profile, matching_lanes[0]


def build_product_operational_readiness_service_result(
    *,
    record_store: ProductOperationalReadinessStore,
    profile: LaunchplaneProductProfileRecord,
    lane: ProductLaneProfile,
    identity: LaunchplaneIdentity,
    requested_action: str,
    requested_artifact_id: str,
    expected_current_artifact_id: str,
    generated_at: str,
) -> ProductOperationalReadiness:
    normalized_action = requested_action.strip()
    if not normalized_action:
        raise ValueError("Operational readiness requires an action.")
    action = _read_driver_action(profile=profile, requested_action=normalized_action)
    lane_summary = _read_lane_summary(record_store=record_store, lane=lane)
    topology = build_product_environment_topology(
        record_store=record_store,
        profile=profile,
        lane=lane,
        lane_summary=lane_summary,
    )
    config_status = build_product_environment_config_status(
        record_store=record_store,
        product=profile.product,
        environment=lane.instance,
    )
    active_policy_records = tuple(record_store.list_authz_policy_records(status="active", limit=2))
    artifact_manifest = _read_artifact_manifest(
        record_store=record_store,
        artifact_id=requested_artifact_id,
    )
    current_inventory_artifact_id = _read_current_inventory_artifact_id(lane_summary)
    production_backup_authority = None
    if action is not None and action.requires_production_backup_policy:
        try:
            production_backup_authority = resolve_production_backup_authority(
                record_store=require_production_backup_authority_store(record_store),
                product=profile.product,
                context=lane.context,
                instance=lane.instance,
                promotion_action=action.authz_action,
                generated_at=generated_at,
            )
        except TypeError:
            production_backup_authority = None
    return build_product_operational_readiness(
        ProductOperationalReadinessInputs(
            profile=profile,
            lane=lane,
            requested_action=normalized_action,
            action=action,
            identity=identity,
            active_policy_records=active_policy_records,
            lane_summary=lane_summary,
            runtime_settings=config_status.runtime_settings,
            managed_secrets=config_status.managed_secrets,
            topology=topology,
            requested_artifact_id=requested_artifact_id.strip(),
            expected_current_artifact_id=expected_current_artifact_id.strip(),
            current_inventory_artifact_id=current_inventory_artifact_id,
            artifact_manifest=artifact_manifest,
            generated_at=generated_at,
            production_backup_authority=production_backup_authority,
        )
    )


def _read_driver_action(
    *,
    profile: LaunchplaneProductProfileRecord,
    requested_action: str,
) -> DriverActionDescriptor | None:
    try:
        descriptor = read_driver_descriptor(profile.driver_id)
    except FileNotFoundError:
        return None
    matches = tuple(
        action
        for action in effective_driver_actions(descriptor)
        if requested_action in (action.authz_action, *action.alternate_authz_actions)
    )
    if len(matches) > 1:
        raise ValueError(
            f"Driver '{profile.driver_id}' declares ambiguous authz action '{requested_action}'."
        )
    return matches[0] if matches else None


def _read_lane_summary(
    *, record_store: ProductOperationalReadinessStore, lane: ProductLaneProfile
) -> LaunchplaneLaneSummary | None:
    try:
        summary = record_store.read_lane_summary(
            context_name=lane.context,
            instance_name=lane.instance,
        )
    except FileNotFoundError:
        return None
    return summary.model_copy(update={"provenance": build_lane_summary_provenance(summary)})


def _read_artifact_manifest(
    *, record_store: ProductOperationalReadinessStore, artifact_id: str
) -> ArtifactIdentityManifest | None:
    normalized_artifact_id = artifact_id.strip()
    if not normalized_artifact_id:
        return None
    try:
        return record_store.read_artifact_manifest(normalized_artifact_id)
    except FileNotFoundError:
        return None


def _read_current_inventory_artifact_id(
    lane_summary: LaunchplaneLaneSummary | None,
) -> str | None:
    if lane_summary is None or lane_summary.inventory is None:
        return None
    inventory = lane_summary.inventory
    return inventory.artifact_identity.artifact_id if inventory.artifact_identity else ""
