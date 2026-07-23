from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.drivers.registry import read_driver_descriptor


ODOO_DRIVER_ID = "odoo"


class OdooProductMismatchError(ValueError):
    pass


class OdooRouteDependencyError(ValueError):
    pass


def _list_product_profiles(
    record_store: object,
) -> tuple[LaunchplaneProductProfileRecord, ...]:
    list_profiles = getattr(record_store, "list_product_profile_records", None)
    if not callable(list_profiles):
        raise ValueError("Product driver validation requires product profile listing.")
    try:
        return tuple(list_profiles(driver_id=""))
    except FileNotFoundError:
        return ()


def _profile_owns_context(*, profile: LaunchplaneProductProfileRecord, context: str) -> bool:
    if any(lane.context.strip() == context for lane in profile.lanes):
        return True
    return profile.preview.enabled and profile.preview.context.strip() == context


def _require_unambiguous_profile_ownership(
    *,
    record_store: object,
    profile: LaunchplaneProductProfileRecord,
    targets: tuple[tuple[str, str], ...],
) -> None:
    profiles = _list_product_profiles(record_store)
    for target_context, target_instance in targets:
        owner_products = {
            candidate.product
            for candidate in profiles
            if (
                any(
                    lane.context.strip() == target_context
                    and lane.instance.strip() == target_instance
                    for lane in candidate.lanes
                )
                if target_instance
                else _profile_owns_context(profile=candidate, context=target_context)
            )
        }
        if owner_products != {profile.product}:
            raise OdooProductMismatchError(
                "Odoo product profile lane ownership is missing or ambiguous."
            )


def resolve_odoo_product_route(
    *,
    record_store: object,
    product: str,
    context: str = "",
    instance: str = "",
    instances: tuple[str, ...] = (),
) -> LaunchplaneProductProfileRecord:
    normalized_product = product.strip()
    if not normalized_product or normalized_product == ODOO_DRIVER_ID:
        raise OdooProductMismatchError(
            "Odoo driver routes require a DB-backed product profile, not the base driver id."
        )
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
    normalized_context = context.strip()
    normalized_instance = instance.strip()
    normalized_instances = tuple(candidate.strip() for candidate in instances if candidate.strip())
    target_instances = tuple(
        dict.fromkeys(
            (normalized_instance,) + normalized_instances
            if normalized_instance
            else normalized_instances
        )
    )
    if target_instances:
        resolved_targets: list[tuple[str, str]] = []
        for target_instance in target_instances:
            matching_lanes = tuple(
                lane
                for lane in profile.lanes
                if (not normalized_context or lane.context.strip() == normalized_context)
                and lane.instance.strip() == target_instance
            )
            if len(matching_lanes) != 1:
                raise OdooProductMismatchError(
                    "Product profile does not own one unambiguous requested Odoo driver lane."
                )
            lane = matching_lanes[0]
            resolved_targets.append((lane.context.strip(), lane.instance.strip()))
        _require_unambiguous_profile_ownership(
            record_store=record_store,
            profile=profile,
            targets=tuple(resolved_targets),
        )
        return profile
    if normalized_context:
        if any(lane.context.strip() == normalized_context for lane in profile.lanes):
            _require_unambiguous_profile_ownership(
                record_store=record_store,
                profile=profile,
                targets=((normalized_context, ""),),
            )
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
