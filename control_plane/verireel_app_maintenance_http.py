"""Testing and preview maintenance for the declared VeriReel extensions."""

from pathlib import Path
from pydantic import Field, model_validator
from control_plane.drivers.dispatch import _ProductRouteEnvelope, _validate_driver_envelope_product
from control_plane.workflows.verireel_app_maintenance import (
    VeriReelAppMaintenanceRequest,
    execute_verireel_app_maintenance,
)
from control_plane.drivers.verireel_postgres_transport import (
    VeriReelRouteDependencyError as VeriReelRouteDependencyError,
    VeriReelProductMismatchError as VeriReelProductMismatchError,
)
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.workflows.generic_web_post_deploy import declares_verireel_testing_extension

VERIREEL_APP_MAINTENANCE_ROUTE = "/v1/drivers/verireel/app-maintenance"


class VeriReelAppMaintenanceEnvelope(_ProductRouteEnvelope):
    schema_version: int = Field(default=1, ge=1)
    maintenance: VeriReelAppMaintenanceRequest

    @model_validator(mode="after")
    def _validate_alignment(self) -> "VeriReelAppMaintenanceEnvelope":
        _validate_driver_envelope_product(self.product, label="VeriReel app maintenance")
        return self


def apply_verireel_app_maintenance_result(
    *,
    control_plane_root: Path,
    request: VeriReelAppMaintenanceEnvelope,
) -> tuple[dict[str, object], dict[str, object]]:
    driver_result = execute_verireel_app_maintenance(
        control_plane_root=control_plane_root,
        request=request.maintenance,
    )
    return {}, driver_result.model_dump(mode="json")


def resolve_verireel_route_authorization(
    *, record_store: object, product: str, context: str, instance: str = ""
) -> tuple[str, str]:
    read_profile = getattr(record_store, "read_product_profile_record", None)
    if not callable(read_profile):
        raise VeriReelRouteDependencyError("Product profile storage is required.")
    try:
        profile = read_profile(product.strip())
    except FileNotFoundError as error:
        raise VeriReelRouteDependencyError from error
    if not isinstance(profile, LaunchplaneProductProfileRecord):
        profile = LaunchplaneProductProfileRecord.model_validate(profile)
    stable = declares_verireel_testing_extension(profile, instance=instance) and any(
        lane.context == context and lane.instance == instance for lane in profile.lanes
    )
    preview = (
        profile.driver_id == "generic-web"
        and instance == "preview"
        and profile.preview.enabled
        and profile.preview.context == context
        and profile.preview.transport_extension == "verireel-postgres"
    )
    if not profile.is_active or not (stable or preview):
        raise VeriReelProductMismatchError("Product does not declare maintenance for this lane.")
    return profile.product, context


def should_store_verireel_result_idempotency(result: dict[str, object]) -> bool:
    return result.get("maintenance_status") == "pass"
