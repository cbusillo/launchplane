from __future__ import annotations

from pathlib import Path

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
from control_plane.workflows.odoo_app_maintenance import (
    OdooAppMaintenanceRequest,
    execute_odoo_app_maintenance,
)


ODOO_APP_MAINTENANCE_ROUTE = "/v1/drivers/odoo/app-maintenance"


class OdooAppMaintenanceProductMismatchError(OdooProductMismatchError):
    pass


class OdooAppMaintenanceRouteDependencyError(OdooRouteDependencyError):
    pass


class OdooAppMaintenanceEnvelope(_ProductRouteEnvelope):
    schema_version: int = Field(default=1, ge=1)
    maintenance: OdooAppMaintenanceRequest

    @model_validator(mode="after")
    def _validate_alignment(self) -> "OdooAppMaintenanceEnvelope":
        _validate_driver_envelope_product(self.product, label="Odoo app maintenance")
        return self


def resolve_odoo_app_maintenance_product_route(
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
        raise OdooAppMaintenanceRouteDependencyError from error
    except OdooProductMismatchError as error:
        raise OdooAppMaintenanceProductMismatchError from error


def execute_odoo_app_maintenance_result(
    *,
    control_plane_root: Path,
    record_store: object,
    request: OdooAppMaintenanceEnvelope,
) -> tuple[dict[str, object], dict[str, object]]:
    driver_result = execute_odoo_app_maintenance(
        control_plane_root=control_plane_root,
        record_store=record_store,
        request=request.maintenance,
    )
    records: dict[str, object] = {
        "transition": (
            f"odoo-app-maintenance:{driver_result.context}:{driver_result.instance}:deploy"
        )
    }
    return records, driver_result.model_dump(mode="json")


def should_store_odoo_app_maintenance_idempotency(
    driver_result: dict[str, object],
) -> bool:
    return driver_result.get("maintenance_status") == "pass"
