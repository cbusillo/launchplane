"""Join a generic-web testing inventory to its current qualification evidence."""

from typing import Protocol

import click

from control_plane.contracts.deployment_record import DeploymentRecord, deployment_record_passed
from control_plane.contracts.environment_inventory import EnvironmentInventory
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductLaneProfile,
)


class GenericWebSourceQualificationStore(Protocol):
    def read_product_profile_record(self, product: str) -> LaunchplaneProductProfileRecord: ...

    def read_environment_inventory(
        self, *, context_name: str, instance_name: str
    ) -> EnvironmentInventory: ...

    def read_deployment_record(self, record_id: str) -> DeploymentRecord: ...


class GenericWebSourceUnqualified(ValueError):
    """Testing no longer proves that the candidate passed its complete deployment."""


class GenericWebPromotionSourceRefused(click.ClickException):
    """Fresh forward admission refused; it is not a failed production attempt."""

    failure_code = "generic_web.source_unqualified"


def qualified_generic_web_source_inventory(
    *,
    store: GenericWebSourceQualificationStore,
    profile: LaunchplaneProductProfileRecord,
    lane: ProductLaneProfile,
) -> EnvironmentInventory:
    """Read fresh evidence; acceptance remains bound to the artifact, not this record ID."""
    from control_plane.workflows.generic_web_deploy import product_profile_uses_generic_web_base

    try:
        current_profile = store.read_product_profile_record(profile.product)
        current_lane = next(
            (item for item in current_profile.lanes if item.instance.strip() == "testing"), None
        )
        if (
            current_profile.product != profile.product
            or not product_profile_uses_generic_web_base(current_profile)
            or current_lane is None
            or (current_lane.context.strip(), current_lane.instance.strip())
            != (lane.context, lane.instance)
        ):
            raise GenericWebSourceUnqualified(
                "The product's testing source changed before release."
            )
        inventory = store.read_environment_inventory(
            context_name=lane.context, instance_name=lane.instance
        )
        record = store.read_deployment_record(inventory.deployment_record_id)
    except FileNotFoundError as error:
        raise GenericWebSourceUnqualified(
            "Testing requires current source inventory and its referenced deployment record, "
            "bound to a current product lane."
        ) from error
    identity = inventory.runtime_identity
    target = record.deployed_target
    artifact = inventory.artifact_identity
    if (
        lane.instance != "testing"
        or inventory.context != lane.context
        or inventory.instance != lane.instance
        or record.context != lane.context
        or record.instance != lane.instance
        or record.record_id != inventory.deployment_record_id
        or not deployment_record_passed(record)
        or inventory.deploy.status != "pass"
        or artifact is None
        or record.artifact_identity != artifact
        or record.source_git_ref != inventory.source_git_ref
        or identity is None
        or record.runtime_identity != identity
        or identity.product != profile.product
        or identity.context != lane.context
        or identity.instance != lane.instance
        or identity.environment_kind != "stable"
        or identity.deployment_record_id != record.record_id
        or identity.artifact_id != artifact.artifact_id
        or identity.source_git_ref != record.source_git_ref
        or target is None
        or target.provider_id != record.deploy.provider_id
        or target.target_category != record.deploy.target_category
        or target.provider_target_type != record.deploy.provider_target_type
        or any(
            getattr(inventory.deploy, field) != getattr(record.deploy, field)
            for field in (
                "provider_id",
                "target_name",
                "target_type",
                "target_category",
                "provider_target_type",
                "deployment_id",
            )
        )
        or (
            record.resolved_target is not None
            and (
                record.resolved_target.target_id != target.target_id
                or record.resolved_target.target_name != record.deploy.target_name
                or record.resolved_target.target_type != record.deploy.target_type
            )
        )
    ):
        raise GenericWebSourceUnqualified(
            "Testing's referenced deployment is not qualified for the current source inventory."
        )
    return inventory
