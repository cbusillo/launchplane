from pathlib import Path
from typing import cast

from pydantic import Field, model_validator

from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.drivers.dispatch import (
    _ProductRouteEnvelope,
    _validate_driver_envelope_product,
)
from control_plane.drivers.registry import read_driver_descriptor
from control_plane.workflows.odoo_prod_backup_gate import (
    OdooProdBackupGateRequest,
    OdooProdBackupGateStore,
    execute_odoo_prod_backup_gate,
)


ODOO_PROD_BACKUP_GATE_ROUTE = "/v1/drivers/odoo/prod-backup-gate"
ODOO_PROD_BACKUP_GATE_ACTION = "odoo_prod_backup_gate.execute"
ODOO_DRIVER_ID = "odoo"


class OdooProdBackupGateProductMismatchError(ValueError):
    pass


class OdooProdBackupGateRouteDependencyError(ValueError):
    pass


class OdooProdBackupGateEnvelope(_ProductRouteEnvelope):
    schema_version: int = Field(default=1, ge=1)
    backup_gate: OdooProdBackupGateRequest

    @model_validator(mode="after")
    def _validate_alignment(self) -> "OdooProdBackupGateEnvelope":
        _validate_driver_envelope_product(self.product, label="Odoo prod backup gate")
        return self


def resolve_odoo_prod_backup_gate_product_route(
    *,
    record_store: object,
    product: str,
    context: str,
    instance: str,
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
        raise OdooProdBackupGateRouteDependencyError from error
    if not isinstance(profile, LaunchplaneProductProfileRecord):
        profile = LaunchplaneProductProfileRecord.model_validate(profile)
    if not _product_profile_uses_odoo_driver(profile):
        raise OdooProdBackupGateProductMismatchError(
            "Product is not configured for the requested Odoo driver route."
        )
    for lane in profile.lanes:
        if lane.context.strip() == context.strip() and lane.instance.strip() == instance.strip():
            return profile
    raise OdooProdBackupGateProductMismatchError(
        "Product profile does not own the requested Odoo driver lane."
    )


def execute_odoo_prod_backup_gate_result(
    *,
    control_plane_root: Path,
    record_store: object,
    request: OdooProdBackupGateEnvelope,
) -> tuple[dict[str, object], dict[str, object]]:
    driver_result = execute_odoo_prod_backup_gate(
        control_plane_root=control_plane_root,
        record_store=cast(OdooProdBackupGateStore, record_store),
        request=request.backup_gate,
    )
    records: dict[str, object] = {"backup_record_id": driver_result.backup_record_id}
    result: dict[str, object] = {
        "backup_record_id": driver_result.backup_record_id,
        "backup_status": driver_result.backup_status,
        "backup_root": driver_result.backup_root,
        "database_dump_path": driver_result.database_dump_path,
        "filestore_archive_path": driver_result.filestore_archive_path,
        "manifest_path": driver_result.manifest_path,
    }
    return records, result


def should_store_odoo_prod_backup_gate_idempotency(
    driver_result: dict[str, object],
) -> bool:
    return str(driver_result.get("backup_status", "")).strip() == "pass"


def _product_profile_uses_odoo_driver(profile: LaunchplaneProductProfileRecord) -> bool:
    profile_driver_id = profile.driver_id.strip()
    if profile_driver_id == ODOO_DRIVER_ID:
        return True
    try:
        descriptor = read_driver_descriptor(profile_driver_id)
    except FileNotFoundError:
        return False
    return descriptor.base_driver_id == ODOO_DRIVER_ID
