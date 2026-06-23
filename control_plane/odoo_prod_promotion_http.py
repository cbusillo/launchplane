from pathlib import Path
from typing import cast

from pydantic import BaseModel, Field, model_validator

from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.drivers.dispatch import (
    _ProductRouteEnvelope,
    _validate_driver_envelope_product,
)
from control_plane.drivers.registry import read_driver_descriptor
from control_plane.workflows.odoo_prod_promotion_inputs import (
    OdooProdPromotionInputsRequest,
    OdooProdPromotionInputsResult,
    OdooProdPromotionInputsStore,
    resolve_odoo_prod_promotion_inputs,
)
from control_plane.workflows.odoo_prod_promotion_run import (
    OdooProdPromotionRunRequest,
    OdooProdPromotionRunResult,
    OdooProdPromotionRunStore,
    execute_odoo_prod_promotion_run,
)


ODOO_PROD_PROMOTION_INPUTS_ROUTE = "/v1/drivers/odoo/prod-promotion-inputs"
ODOO_PROD_PROMOTION_RUN_ROUTE = "/v1/drivers/odoo/prod-promotion-run"
ODOO_PROD_PROMOTION_INPUTS_ACTION = "odoo_prod_promotion_inputs.read"
ODOO_PROD_PROMOTION_RUN_ACTION = "odoo_prod_promotion_run.execute"
ODOO_DRIVER_ID = "odoo"


class OdooProdPromotionProductMismatchError(ValueError):
    pass


class OdooProdPromotionRouteDependencyError(ValueError):
    pass


class OdooProdPromotionInputsEnvelope(_ProductRouteEnvelope):
    schema_version: int = Field(default=1, ge=1)
    inputs: OdooProdPromotionInputsRequest

    @model_validator(mode="after")
    def _validate_alignment(self) -> "OdooProdPromotionInputsEnvelope":
        _validate_driver_envelope_product(self.product, label="Odoo prod promotion inputs")
        return self


class OdooProdPromotionRunEnvelope(_ProductRouteEnvelope):
    schema_version: int = Field(default=1, ge=1)
    run: OdooProdPromotionRunRequest

    @model_validator(mode="after")
    def _validate_alignment(self) -> "OdooProdPromotionRunEnvelope":
        _validate_driver_envelope_product(self.product, label="Odoo prod promotion run")
        return self


def resolve_odoo_prod_promotion_product_route(
    *,
    record_store: object,
    product: str,
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
        raise OdooProdPromotionRouteDependencyError from error
    if not isinstance(profile, LaunchplaneProductProfileRecord):
        profile = LaunchplaneProductProfileRecord.model_validate(profile)
    if not _product_profile_uses_odoo_driver(profile):
        raise OdooProdPromotionProductMismatchError(
            "Product is not configured for the requested Odoo driver route."
        )
    return profile


def resolve_odoo_prod_promotion_inputs_result(
    *,
    record_store: object,
    request: OdooProdPromotionInputsEnvelope,
) -> tuple[dict[str, object], dict[str, object]]:
    driver_result = resolve_odoo_prod_promotion_inputs(
        record_store=cast(OdooProdPromotionInputsStore, record_store),
        request=request.inputs,
    )
    return _prod_promotion_inputs_records(driver_result), cast(
        dict[str, object], driver_result.model_dump(mode="json")
    )


def execute_odoo_prod_promotion_run_result(
    *,
    control_plane_root: Path,
    state_dir: Path,
    database_url: str | None,
    record_store: object,
    request: OdooProdPromotionRunEnvelope,
) -> tuple[dict[str, object], dict[str, object]]:
    run_request = request.run.model_copy(update={"product": request.product})
    driver_result = execute_odoo_prod_promotion_run(
        control_plane_root=control_plane_root,
        state_dir=state_dir,
        database_url=database_url,
        record_store=cast(OdooProdPromotionRunStore, record_store),
        request=run_request,
    )
    return _prod_promotion_run_records(driver_result), cast(
        dict[str, object], driver_result.model_dump(mode="json")
    )


def driver_result_contains_status(
    driver_result: BaseModel | dict[str, object], status: str
) -> bool:
    if isinstance(driver_result, BaseModel):
        items = driver_result.model_dump(mode="json").items()
    else:
        items = driver_result.items()
    return status in (
        str(value).strip() for key, value in items if key.endswith("_status") or key == "status"
    )


def should_store_prod_promotion_idempotency(driver_result: dict[str, object]) -> bool:
    if driver_result_contains_status(driver_result, "blocked"):
        return False
    return not driver_result_contains_status(driver_result, "fail")


def _product_profile_uses_odoo_driver(profile: LaunchplaneProductProfileRecord) -> bool:
    profile_driver_id = profile.driver_id.strip()
    if profile_driver_id == ODOO_DRIVER_ID:
        return True
    try:
        descriptor = read_driver_descriptor(profile_driver_id)
    except FileNotFoundError:
        return False
    return descriptor.base_driver_id == ODOO_DRIVER_ID


def _prod_promotion_inputs_records(
    driver_result: OdooProdPromotionInputsResult,
) -> dict[str, object]:
    return {
        "artifact_id": driver_result.artifact_id,
        "backup_record_id": driver_result.backup_record_id,
        "release_tuple_id": driver_result.release_tuple_id,
    }


def _prod_promotion_run_records(
    driver_result: OdooProdPromotionRunResult,
) -> dict[str, object]:
    return {
        "artifact_id": driver_result.artifact_id,
        "backup_record_id": driver_result.backup_record_id,
        "promotion_record_id": driver_result.promotion_record_id,
        "deployment_record_id": driver_result.deployment_record_id,
        "release_tuple_id": driver_result.release_tuple_id,
        "request_id": driver_result.request_id,
    }
