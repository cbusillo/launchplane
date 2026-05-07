from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from control_plane.contracts.product_environment_read_model import (
    ActionAllowed,
    ProductReadModelStore,
    build_product_activity_read_model,
    build_product_environment_detail,
    build_product_site_overview,
    build_product_site_overviews,
)


@dataclass(frozen=True)
class ProductEnvironmentReadServiceResult:
    payload: dict[str, object]
    authorization_product: str
    authorization_context: str
    denial_message: str


def is_product_environment_detail_request(params: Mapping[str, str]) -> bool:
    return any(key in params for key in ("activity", "environment", "product"))


def build_product_profile_list_service_payload(
    *, record_store: ProductReadModelStore, driver_id: str
) -> dict[str, object]:
    profiles = record_store.list_product_profile_records(driver_id=driver_id)
    return {
        "driver_id": driver_id,
        "profiles": [profile.model_dump(mode="json") for profile in profiles],
    }


def build_product_environment_read_service_result(
    *,
    record_store: ProductReadModelStore,
    params: Mapping[str, str],
    action_allowed: ActionAllowed,
) -> ProductEnvironmentReadServiceResult:
    if params.get("activity") == "true":
        activity = build_product_activity_read_model(
            record_store=record_store,
            product=params["product"],
        )
        return ProductEnvironmentReadServiceResult(
            payload={"activity": activity.model_dump(mode="json")},
            authorization_product=activity.product,
            authorization_context="launchplane",
            denial_message="Workflow cannot read the requested product activity.",
        )

    if "environment" in params:
        detail = build_product_environment_detail(
            record_store=record_store,
            product=params["product"],
            environment=params["environment"],
            action_allowed=action_allowed,
        )
        return ProductEnvironmentReadServiceResult(
            payload={"environment": detail.model_dump(mode="json")},
            authorization_product=detail.product,
            authorization_context=detail.context,
            denial_message="Workflow cannot read the requested product environment.",
        )

    if "product" in params:
        overview = build_product_site_overview(
            record_store=record_store,
            product=params["product"],
            action_allowed=action_allowed,
        )
        return ProductEnvironmentReadServiceResult(
            payload={"product": overview.model_dump(mode="json")},
            authorization_product=overview.product,
            authorization_context="launchplane",
            denial_message="Workflow cannot read the requested product overview.",
        )

    raise ValueError("Product environment read result requires product parameters.")


def build_product_environment_list_service_payload(
    *,
    record_store: ProductReadModelStore,
    action_allowed: ActionAllowed,
) -> dict[str, object]:

    overviews = build_product_site_overviews(
        record_store=record_store,
        action_allowed=action_allowed,
    )
    return {
        "products": [overview.model_dump(mode="json") for overview in overviews],
    }
