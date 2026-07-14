from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import cast

from control_plane.contracts.product_environment_read_model import (
    ActionAllowed,
    ProductEnvironmentReadModelStore,
    ProductReadModelStore,
    ProductSiteOverview,
    build_product_activity_read_model,
    build_product_environment_config_status,
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


class ProductReadModelStoreCapabilityError(RuntimeError):
    pass


_PRODUCT_ENVIRONMENT_READ_MODEL_STORE_METHODS = (
    "list_product_profile_records",
    "read_product_profile_record",
    "read_lane_summary",
    "read_route_binding_record",
    "list_deployment_records",
    "list_promotion_records",
    "list_backup_gate_records",
    "list_preview_records",
    "list_preview_summaries",
    "list_preview_desired_state_records",
    "list_preview_lifecycle_cleanup_records",
    "list_preview_pr_feedback_records",
    "list_public_ingress_observation_records",
    "list_public_ingress_incident_records",
    "list_authz_policy_records",
)


def require_product_environment_read_model_store(
    record_store: object,
) -> ProductEnvironmentReadModelStore:
    missing_methods = tuple(
        method_name
        for method_name in _PRODUCT_ENVIRONMENT_READ_MODEL_STORE_METHODS
        if not callable(getattr(record_store, method_name, None))
    )
    if missing_methods:
        missing = ", ".join(missing_methods)
        raise ProductReadModelStoreCapabilityError(
            "Product environment reads require DB-backed Launchplane storage; "
            f"missing store method(s): {missing}."
        )
    return cast(ProductEnvironmentReadModelStore, record_store)


def is_product_environment_detail_request(params: Mapping[str, str]) -> bool:
    return any(
        key in params
        for key in ("activity", "config_status", "environment", "environments", "product")
    )


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
    record_store: ProductEnvironmentReadModelStore,
    params: Mapping[str, str],
    action_allowed: ActionAllowed,
) -> ProductEnvironmentReadServiceResult:
    product = params.get("product", "").strip()
    if not product:
        raise ValueError("Product environment read result requires product parameters.")
    try:
        record_store.read_product_profile_record(product)
    except FileNotFoundError as error:
        raise FileNotFoundError(f"Product '{product}' was not found.") from error

    if params.get("activity") == "true":
        activity = build_product_activity_read_model(
            record_store=record_store,
            product=product,
        )
        return ProductEnvironmentReadServiceResult(
            payload={"activity": activity.model_dump(mode="json")},
            authorization_product=activity.product,
            authorization_context="launchplane",
            denial_message="Workflow cannot read the requested product activity.",
        )

    if params.get("config_status") == "true":
        environment = params["environment"]
        try:
            config_status = build_product_environment_config_status(
                record_store=record_store,
                product=product,
                environment=environment,
            )
        except FileNotFoundError as error:
            raise FileNotFoundError(
                f"Product '{product}' has no environment '{environment}'."
            ) from error
        return ProductEnvironmentReadServiceResult(
            payload={"config_status": config_status.model_dump(mode="json")},
            authorization_product=config_status.product,
            authorization_context=config_status.context,
            denial_message="Workflow cannot read the requested product environment config status.",
        )

    if "environment" in params:
        environment = params["environment"]
        try:
            detail = build_product_environment_detail(
                record_store=record_store,
                product=product,
                environment=environment,
                action_allowed=action_allowed,
            )
        except FileNotFoundError as error:
            raise FileNotFoundError(
                f"Product '{product}' has no environment '{environment}'."
            ) from error
        return ProductEnvironmentReadServiceResult(
            payload={"environment": detail.model_dump(mode="json")},
            authorization_product=detail.product,
            authorization_context=detail.context,
            denial_message="Workflow cannot read the requested product environment.",
        )

    if params.get("environments") == "true":
        overview = build_product_site_overview(
            record_store=record_store,
            product=product,
            action_allowed=action_allowed,
        )
        return ProductEnvironmentReadServiceResult(
            payload=_product_environments_payload(overview),
            authorization_product=overview.product,
            authorization_context="launchplane",
            denial_message="Workflow cannot list the requested product environments.",
        )

    if "product" in params:
        overview = build_product_site_overview(
            record_store=record_store,
            product=product,
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
    record_store: ProductEnvironmentReadModelStore,
    action_allowed: ActionAllowed,
) -> dict[str, object]:

    overviews = build_product_site_overviews(
        record_store=record_store,
        action_allowed=action_allowed,
    )
    return {
        "products": [overview.model_dump(mode="json") for overview in overviews],
    }


def _product_environments_payload(overview: ProductSiteOverview) -> dict[str, object]:
    return {
        "product": overview.product,
        "display_name": overview.display_name,
        "repository": overview.repository,
        "driver_id": overview.driver_id,
        "base_driver_id": overview.base_driver_id,
        "environments": [
            environment.model_dump(mode="json") for environment in overview.environments
        ],
        "trust_state": overview.trust_state,
        "provenance": overview.provenance.model_dump(mode="json"),
    }
