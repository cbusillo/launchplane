from control_plane import product_config as control_plane_product_config
from control_plane.product_config import ProductConfigMode, ProductConfigStore
from control_plane.product_config_service import (
    ProductConfigServiceError,
    product_config_service_error,
)
from control_plane.storage.product_authority_bundle import RuntimeEnvironmentConflictError


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
    except (
        control_plane_product_config.ProductConfigError,
        RuntimeEnvironmentConflictError,
    ) as error:
        return None, product_config_service_error(error)
