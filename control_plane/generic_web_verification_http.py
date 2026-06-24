from __future__ import annotations

from pathlib import Path

import click

from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductLaneProfile,
)
from control_plane.drivers.generic_web_dispatch import (
    GenericWebStableVerificationEnvelope,
    _GENERIC_WEB_STABLE_VERIFICATION_ROUTE,
    _apply_generic_web_stable_verification_records,
)
from control_plane.drivers.generic_web_preview_dispatch import (
    GenericWebPreviewVerificationEnvelope,
    _GENERIC_WEB_PREVIEW_VERIFICATION_ROUTE,
    _apply_generic_web_preview_verification_records,
)
from control_plane.workflows.generic_web_deploy import product_profile_uses_generic_web_base


GENERIC_WEB_STABLE_VERIFICATION_ROUTE = _GENERIC_WEB_STABLE_VERIFICATION_ROUTE.route_path
GENERIC_WEB_PREVIEW_VERIFICATION_ROUTE = _GENERIC_WEB_PREVIEW_VERIFICATION_ROUTE.route_path
GENERIC_WEB_STABLE_VERIFICATION_ACTION = "deployment.write"
GENERIC_WEB_PREVIEW_VERIFICATION_ACTION = "preview_generation.write"

__all__ = [
    "GENERIC_WEB_PREVIEW_VERIFICATION_ACTION",
    "GENERIC_WEB_PREVIEW_VERIFICATION_ROUTE",
    "GENERIC_WEB_STABLE_VERIFICATION_ACTION",
    "GENERIC_WEB_STABLE_VERIFICATION_ROUTE",
    "GenericWebPreviewVerificationEnvelope",
    "GenericWebStableVerificationEnvelope",
    "GenericWebVerificationProductMismatchError",
    "GenericWebVerificationRouteDependencyError",
    "apply_generic_web_preview_verification_result",
    "apply_generic_web_stable_verification_result",
    "generic_web_verification_response_records",
    "resolve_generic_web_preview_verification_profile",
    "resolve_generic_web_stable_verification_lane",
    "should_store_generic_web_verification_idempotency",
]

_GENERIC_WEB_VERIFICATION_RESPONSE_RECORD_KEYS = frozenset(
    {
        "deployment_record_id",
        "generation_id",
        "generic_web_preview_verification",
        "inventory_record_id",
        "preview_generation_id",
        "preview_id",
        "preview_state",
        "promotion_record_id",
        "transition",
        "verification_status",
        "verified_at",
    }
)


class GenericWebVerificationProductMismatchError(ValueError):
    pass


class GenericWebVerificationRouteDependencyError(ValueError):
    pass


def resolve_generic_web_stable_verification_lane(
    *, record_store: object, product: str, context: str, instance: str
) -> tuple[LaunchplaneProductProfileRecord, ProductLaneProfile]:
    profile = _read_generic_web_verification_profile(record_store=record_store, product=product)
    normalized_context = context.strip()
    normalized_instance = instance.strip()
    for lane in profile.lanes:
        if (
            lane.context.strip() == normalized_context
            and lane.instance.strip() == normalized_instance
        ):
            return profile, lane
    raise GenericWebVerificationProductMismatchError(
        "Product profile does not own the requested driver lane."
    )


def resolve_generic_web_preview_verification_profile(
    *, record_store: object, product: str, context: str
) -> LaunchplaneProductProfileRecord:
    profile = _read_generic_web_verification_profile(record_store=record_store, product=product)
    if not profile.preview.enabled:
        raise click.ClickException(
            f"Product {profile.product!r} does not have generic-web previews enabled."
        )
    if not profile.preview.context.strip():
        raise click.ClickException(
            f"Product {profile.product!r} does not define a preview context."
        )
    if profile.preview.context.strip() != context.strip():
        raise GenericWebVerificationProductMismatchError(
            "Product profile does not own the requested preview context."
        )
    return profile


def apply_generic_web_stable_verification_result(
    *, record_store: object, request: GenericWebStableVerificationEnvelope
) -> dict[str, object]:
    return _apply_generic_web_stable_verification_records(
        record_store=record_store,
        request=request.verification,
    )


def apply_generic_web_preview_verification_result(
    *,
    control_plane_root: Path,
    record_store: object,
    request: GenericWebPreviewVerificationEnvelope,
) -> dict[str, object]:
    return _apply_generic_web_preview_verification_records(
        control_plane_root_path=control_plane_root,
        record_store=record_store,
        request=request.verification,
    )


def generic_web_verification_response_records(
    result: dict[str, object],
) -> dict[str, object]:
    records: dict[str, object] = {}
    for key, value in result.items():
        if key not in _GENERIC_WEB_VERIFICATION_RESPONSE_RECORD_KEYS:
            continue
        if key.endswith("_preview_verification") and isinstance(value, dict):
            records[key] = value
            continue
        records[key] = str(value)
    return records


def should_store_generic_web_verification_idempotency(result: dict[str, object]) -> bool:
    return not _result_contains_status(result, "blocked") and not _result_contains_status(
        result, "fail"
    )


def _result_contains_status(value: object, status: str) -> bool:
    if isinstance(value, str):
        return value == status
    if isinstance(value, dict):
        return any(_result_contains_status(nested_value, status) for nested_value in value.values())
    if isinstance(value, (list, tuple)):
        return any(_result_contains_status(nested_value, status) for nested_value in value)
    return False


def _read_generic_web_verification_profile(
    *, record_store: object, product: str
) -> LaunchplaneProductProfileRecord:
    read_profile = getattr(record_store, "read_product_profile_record", None)
    if not callable(read_profile):
        raise GenericWebVerificationRouteDependencyError(
            "Product driver validation requires product profile storage."
        )
    try:
        profile = read_profile(product.strip())
    except FileNotFoundError as error:
        raise GenericWebVerificationRouteDependencyError from error
    if not isinstance(profile, LaunchplaneProductProfileRecord):
        profile = LaunchplaneProductProfileRecord.model_validate(profile)
    if not product_profile_uses_generic_web_base(profile):
        raise GenericWebVerificationProductMismatchError(
            "Product profile is not compatible with the requested driver route."
        )
    return profile
