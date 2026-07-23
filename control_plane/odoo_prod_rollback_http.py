from pathlib import Path
from typing import cast

from pydantic import Field, model_validator

from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.drivers.dispatch import (
    _ProductRouteEnvelope,
    _validate_driver_envelope_product,
)
from control_plane.odoo_product_driver_http import (
    OdooProductMismatchError,
    OdooRouteDependencyError,
    resolve_odoo_product_route,
)
from control_plane.workflows.odoo_prod_rollback import (
    OdooProdRollbackRequest,
    OdooProdRollbackStore,
    execute_odoo_prod_rollback,
)


ODOO_PROD_ROLLBACK_ROUTE = "/v1/drivers/odoo/prod-rollback"


class OdooProdRollbackProductMismatchError(OdooProductMismatchError):
    pass


class OdooProdRollbackRouteDependencyError(OdooRouteDependencyError):
    pass


class OdooProdRollbackEnvelope(_ProductRouteEnvelope):
    schema_version: int = Field(default=1, ge=1)
    rollback: OdooProdRollbackRequest

    @model_validator(mode="after")
    def _validate_alignment(self) -> "OdooProdRollbackEnvelope":
        _validate_driver_envelope_product(self.product, label="Odoo prod rollback")
        return self


def resolve_odoo_prod_rollback_product_route(
    *,
    record_store: object,
    product: str,
    context: str,
    instance: str,
) -> LaunchplaneProductProfileRecord:
    try:
        return resolve_odoo_product_route(
            record_store=record_store,
            product=product,
            context=context,
            instance=instance,
        )
    except OdooRouteDependencyError as error:
        raise OdooProdRollbackRouteDependencyError from error
    except OdooProductMismatchError as error:
        raise OdooProdRollbackProductMismatchError from error


def execute_odoo_prod_rollback_result(
    *,
    control_plane_root: Path,
    record_store: object,
    request: OdooProdRollbackEnvelope,
) -> tuple[dict[str, object], dict[str, object]]:
    driver_result = execute_odoo_prod_rollback(
        control_plane_root=control_plane_root,
        record_store=cast(OdooProdRollbackStore, record_store),
        product=request.product,
        request=request.rollback,
    )
    records: dict[str, object] = {
        "promotion_record_id": driver_result.promotion_record_id,
        "deployment_record_id": driver_result.deployment_record_id,
        "release_tuple_id": driver_result.release_tuple_id,
    }
    result: dict[str, object] = {
        "promotion_record_id": driver_result.promotion_record_id,
        "deployment_record_id": driver_result.deployment_record_id,
        "release_tuple_id": driver_result.release_tuple_id,
        "rollback_status": driver_result.rollback_status,
        "rollback_health_status": driver_result.rollback_health_status,
        "rollback_started_at": driver_result.rollback_started_at,
        "rollback_finished_at": driver_result.rollback_finished_at,
        "post_deploy_status": driver_result.post_deploy_status,
    }
    return records, result


def should_store_odoo_prod_rollback_idempotency(driver_result: dict[str, object]) -> bool:
    return not any(
        str(value).strip() == "fail"
        for key, value in driver_result.items()
        if key.endswith("_status")
    )
