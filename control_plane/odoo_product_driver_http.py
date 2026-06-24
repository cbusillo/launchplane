from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.drivers.registry import read_driver_descriptor


ODOO_DRIVER_ID = "odoo"


class OdooProductMismatchError(ValueError):
    pass


class OdooRouteDependencyError(ValueError):
    pass


def resolve_odoo_product_route(
    *,
    record_store: object,
    product: str,
    context: str = "",
    instance: str = "",
) -> LaunchplaneProductProfileRecord | None:
    normalized_product = product.strip()
    if normalized_product == ODOO_DRIVER_ID:
        return None
    read_profile = getattr(record_store, "read_product_profile_record", None)
    if not callable(read_profile):
        raise ValueError("Product driver validation requires product profile storage.")
    try:
        profile = read_profile(normalized_product)
    except FileNotFoundError as error:
        raise OdooRouteDependencyError from error
    if not isinstance(profile, LaunchplaneProductProfileRecord):
        profile = LaunchplaneProductProfileRecord.model_validate(profile)
    if not product_profile_uses_odoo_driver(profile):
        raise OdooProductMismatchError(
            "Product is not configured for the requested Odoo driver route."
        )
    if context.strip() or instance.strip():
        for lane in profile.lanes:
            if (
                lane.context.strip() == context.strip()
                and lane.instance.strip() == instance.strip()
            ):
                return profile
        raise OdooProductMismatchError(
            "Product profile does not own the requested Odoo driver lane."
        )
    return profile


def product_profile_uses_odoo_driver(profile: LaunchplaneProductProfileRecord) -> bool:
    profile_driver_id = profile.driver_id.strip()
    if profile_driver_id == ODOO_DRIVER_ID:
        return True
    try:
        descriptor = read_driver_descriptor(profile_driver_id)
    except FileNotFoundError:
        return False
    return descriptor.base_driver_id == ODOO_DRIVER_ID
