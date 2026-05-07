from __future__ import annotations

from dataclasses import dataclass

from control_plane import product_config as control_plane_product_config
from control_plane.product_config import ProductConfigMode, ProductConfigStore


@dataclass(frozen=True)
class ProductConfigServiceError:
    status_code: int
    code: str
    message: str


def apply_product_config_service_request(
    *,
    record_store: ProductConfigStore,
    payload: dict[str, object],
    mode: ProductConfigMode,
    actor: str,
    source_label: str,
) -> tuple[dict[str, object] | None, ProductConfigServiceError | None]:
    try:
        return (
            control_plane_product_config.apply_product_config_bundle(
                record_store=record_store,
                payload=payload,
                mode=mode,
                actor=actor,
                source_label=source_label,
            ),
            None,
        )
    except control_plane_product_config.ProductConfigError as error:
        return None, product_config_service_error(error)


def product_config_service_error(
    error: control_plane_product_config.ProductConfigError,
) -> ProductConfigServiceError:
    error_code = error.code
    error_message = "Product config request failed validation."
    status_code = 400
    if error_code == "secret_configuration_required":
        status_code = 503
        error_message = "Launchplane service is missing required secret write configuration."
    if error_code == "runtime_key_safety_unavailable":
        status_code = 503
        error_message = "Launchplane runtime key-safety policy is unavailable."
    if error_code == "runtime_key_safety_failed":
        error_message = "Product config runtime key-safety gate failed."
    return ProductConfigServiceError(
        status_code=status_code,
        code=error_code,
        message=error_message,
    )
