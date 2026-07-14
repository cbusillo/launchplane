from __future__ import annotations

from pydantic import Field, model_validator

from control_plane.contracts.odoo_stable_target_replacement import (
    OdooStableTargetReplacementRequest,
)
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductLaneProfile,
)
from control_plane.drivers.dispatch import (
    _ProductRouteEnvelope,
    _validate_driver_envelope_product,
)
from control_plane.odoo_product_driver_http import (
    OdooProductMismatchError,
    OdooRouteDependencyError,
    product_profile_uses_odoo_driver,
)


ODOO_TARGET_REPLACEMENT_PLAN_ROUTE = "/v1/drivers/odoo/target-replacement-plan"


class OdooTargetReplacementPlanProductMismatchError(OdooProductMismatchError):
    pass


class OdooTargetReplacementPlanRouteDependencyError(OdooRouteDependencyError):
    pass


class OdooTargetReplacementPlanEnvelope(_ProductRouteEnvelope):
    schema_version: int = Field(default=1, ge=1)
    replacement: OdooStableTargetReplacementRequest

    @model_validator(mode="after")
    def _validate_alignment(self) -> "OdooTargetReplacementPlanEnvelope":
        _validate_driver_envelope_product(self.product, label="Odoo target replacement plan")
        if self.product.strip() != self.replacement.product.strip():
            raise ValueError("Odoo target replacement plan requires matching product values.")
        return self


def resolve_odoo_target_replacement_plan_lane(
    *, record_store: object, product: str, instance: str
) -> ProductLaneProfile:
    read_profile = getattr(record_store, "read_product_profile_record", None)
    if not callable(read_profile):
        raise ValueError("Product driver validation requires product profile storage.")
    try:
        profile = read_profile(product.strip())
    except FileNotFoundError as error:
        raise OdooTargetReplacementPlanRouteDependencyError from error
    if not isinstance(profile, LaunchplaneProductProfileRecord):
        profile = LaunchplaneProductProfileRecord.model_validate(profile)
    if not product_profile_uses_odoo_driver(profile):
        raise OdooTargetReplacementPlanProductMismatchError(
            "Product is not configured for the requested Odoo driver route."
        )
    normalized_instance = instance.strip()
    for lane in profile.lanes:
        if lane.instance.strip() == normalized_instance:
            return lane
    raise OdooTargetReplacementPlanProductMismatchError(
        "Product profile does not own the requested Odoo driver lane."
    )
