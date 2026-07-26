from __future__ import annotations

from pathlib import Path
from typing import Protocol, cast

import click
from pydantic import Field, model_validator

from control_plane.contracts.durable_operation_authorization import (
    DurableOperationAuthorization,
)
from control_plane.contracts.odoo_prod_backup_restore import (
    OdooProdBackupRestoreApplyRequest,
    OdooProdBackupRestoreRequest,
)
from control_plane.contracts.odoo_prod_backup_restore_operation import (
    OdooProdBackupRestoreOperationRecord,
    build_odoo_prod_backup_restore_operation_id,
)
from control_plane.contracts.odoo_stable_bootstrap_operation import (
    OdooStableBootstrapOperationRecord,
)
from control_plane.contracts.odoo_stable_target_replacement_operation import (
    OdooStableTargetReplacementOperationRecord,
)
from control_plane.contracts.product_profile_record import ProductLaneProfile
from control_plane.drivers.dispatch import (
    _ProductRouteEnvelope,
    _validate_driver_envelope_product,
)
from control_plane.odoo_product_driver_http import (
    OdooProductMismatchError,
    OdooRouteDependencyError,
    resolve_odoo_product_route,
)
from control_plane.workflows.odoo_prod_backup_restore import (
    OdooProdBackupRestoreStore,
    build_odoo_prod_backup_restore_plan,
)


ODOO_PROD_BACKUP_RESTORE_PLAN_ROUTE = "/v1/drivers/odoo/prod-backup-restore-plan"
ODOO_PROD_BACKUP_RESTORE_APPLY_ROUTE = "/v1/drivers/odoo/prod-backup-restore-apply"


class OdooProdBackupRestoreProductMismatchError(OdooProductMismatchError):
    pass


class OdooProdBackupRestoreRouteDependencyError(OdooRouteDependencyError):
    pass


class OdooProdBackupRestoreIdempotencyKeyReusedError(ValueError):
    pass


class OdooProdBackupRestorePlanChangedError(ValueError):
    pass


class OdooProdBackupRestoreOperationActiveError(ValueError):
    def __init__(self, operation: OdooProdBackupRestoreOperationRecord) -> None:
        super().__init__(
            "An Odoo production backup restore operation is already active for this lane."
        )
        self.operation = operation


class OdooProdBackupRestoreLaneBusyError(ValueError):
    pass


class OdooProdBackupRestoreOperationStore(OdooProdBackupRestoreStore, Protocol):
    def create_odoo_prod_backup_restore_operation_record_if_no_active_lane(
        self, record: OdooProdBackupRestoreOperationRecord
    ) -> tuple[OdooProdBackupRestoreOperationRecord, bool]: ...

    def read_odoo_prod_backup_restore_operation_record(
        self, operation_id: str
    ) -> OdooProdBackupRestoreOperationRecord: ...

    def list_odoo_prod_backup_restore_operation_records(
        self,
        *,
        product: str = "",
        context_name: str = "",
        instance_name: str = "",
        idempotency_key: str = "",
        idempotency_scope: str = "",
        statuses: tuple[str, ...] = (),
        limit: int | None = None,
    ) -> tuple[OdooProdBackupRestoreOperationRecord, ...]: ...

    def list_odoo_stable_bootstrap_operation_records(
        self,
        *,
        product: str = "",
        context_name: str = "",
        instance_name: str = "",
        idempotency_key: str = "",
        statuses: tuple[str, ...] = (),
        limit: int | None = None,
    ) -> tuple[OdooStableBootstrapOperationRecord, ...]: ...

    def list_odoo_stable_target_replacement_operation_records(
        self,
        *,
        product: str = "",
        context_name: str = "",
        instance_name: str = "",
        idempotency_key: str = "",
        idempotency_scope: str = "",
        statuses: tuple[str, ...] = (),
        limit: int | None = None,
    ) -> tuple[OdooStableTargetReplacementOperationRecord, ...]: ...


class OdooProdBackupRestorePlanEnvelope(_ProductRouteEnvelope):
    schema_version: int = Field(default=1, ge=1)
    restore: OdooProdBackupRestoreRequest

    @model_validator(mode="after")
    def _validate_alignment(self) -> "OdooProdBackupRestorePlanEnvelope":
        _validate_driver_envelope_product(self.product, label="Odoo backup restore plan")
        if self.product.strip() != self.restore.product:
            raise ValueError("Odoo backup restore plan requires matching product values.")
        return self


class OdooProdBackupRestoreApplyEnvelope(_ProductRouteEnvelope):
    schema_version: int = Field(default=1, ge=1)
    restore: OdooProdBackupRestoreApplyRequest

    @model_validator(mode="after")
    def _validate_alignment(self) -> "OdooProdBackupRestoreApplyEnvelope":
        _validate_driver_envelope_product(self.product, label="Odoo backup restore apply")
        if self.product.strip() != self.restore.product:
            raise ValueError("Odoo backup restore apply requires matching product values.")
        return self


def resolve_odoo_prod_backup_restore_lane(
    *,
    record_store: object,
    product: str,
    context: str,
    instance: str,
) -> ProductLaneProfile:
    try:
        profile = resolve_odoo_product_route(
            record_store=record_store,
            product=product,
            context=context,
            instance=instance,
        )
    except OdooRouteDependencyError as error:
        raise OdooProdBackupRestoreRouteDependencyError from error
    except OdooProductMismatchError as error:
        raise OdooProdBackupRestoreProductMismatchError from error
    matching = tuple(
        lane
        for lane in profile.lanes
        if lane.instance.strip().lower() == "prod"
        and lane.context.strip().lower() == context.strip().lower()
    )
    if len(matching) != 1:
        raise OdooProdBackupRestoreProductMismatchError
    return matching[0]


def enqueue_odoo_prod_backup_restore_operation(
    *,
    control_plane_root: Path,
    record_store: object,
    request: OdooProdBackupRestoreApplyEnvelope,
    idempotency_key: str,
    idempotency_scope: str,
    request_fingerprint: str,
    created_at: str,
    authorization: DurableOperationAuthorization,
) -> tuple[dict[str, object], dict[str, object]]:
    operation_store = odoo_prod_backup_restore_operation_store(record_store)
    existing = find_odoo_prod_backup_restore_operation_by_idempotency_key(
        operation_store=operation_store,
        idempotency_key=idempotency_key,
        idempotency_scope=idempotency_scope,
    )
    if existing is not None:
        if existing.request_fingerprint != request_fingerprint:
            raise OdooProdBackupRestoreIdempotencyKeyReusedError
        return _operation_records(existing), odoo_prod_backup_restore_operation_payload(existing)

    restore_request = request.restore
    plan = build_odoo_prod_backup_restore_plan(
        control_plane_root=control_plane_root,
        record_store=operation_store,
        request=OdooProdBackupRestoreRequest.model_validate(
            restore_request.model_dump(
                mode="json",
                exclude={
                    "plan_fingerprint",
                    "confirmation",
                    "no_cache",
                    "timeout_seconds",
                    "health_timeout_seconds",
                },
            )
        ),
    )
    if plan.plan_status != "ready":
        raise ValueError(
            "Odoo production backup restore requires a ready plan: " + "; ".join(plan.blockers)
        )
    if plan.plan_fingerprint != restore_request.plan_fingerprint:
        raise OdooProdBackupRestorePlanChangedError(
            "Odoo production backup restore plan changed before apply."
        )
    _require_lane_not_busy(
        operation_store=operation_store,
        product=restore_request.product,
        context=restore_request.context,
        instance=restore_request.instance,
    )
    operation = OdooProdBackupRestoreOperationRecord(
        operation_id=build_odoo_prod_backup_restore_operation_id(
            product=restore_request.product,
            context=restore_request.context,
            created_at=created_at,
            idempotency_key=idempotency_key,
            idempotency_scope=idempotency_scope,
        ),
        product=restore_request.product,
        context=restore_request.context,
        instance=restore_request.instance,
        idempotency_key=idempotency_key,
        idempotency_scope=idempotency_scope,
        request_fingerprint=request_fingerprint,
        request=restore_request,
        plan=plan,
        authorization=authorization,
        status="pending",
        phase="created",
        created_at=created_at,
        updated_at=created_at,
    )
    persisted, created = (
        operation_store.create_odoo_prod_backup_restore_operation_record_if_no_active_lane(
            operation
        )
    )
    if not created:
        raise OdooProdBackupRestoreOperationActiveError(persisted)
    return _operation_records(persisted), odoo_prod_backup_restore_operation_payload(persisted)


def odoo_prod_backup_restore_operation_store(
    record_store: object,
) -> OdooProdBackupRestoreOperationStore:
    required_methods = (
        "read_product_profile_record",
        "read_dokploy_target_record",
        "read_dokploy_target_id_record",
        "read_environment_inventory",
        "read_artifact_manifest",
        "read_backup_gate_record",
        "list_runtime_environment_records",
        "create_odoo_prod_backup_restore_operation_record_if_no_active_lane",
        "read_odoo_prod_backup_restore_operation_record",
        "list_odoo_prod_backup_restore_operation_records",
        "list_odoo_stable_bootstrap_operation_records",
        "list_odoo_stable_target_replacement_operation_records",
    )
    if all(callable(getattr(record_store, method_name, None)) for method_name in required_methods):
        return cast(OdooProdBackupRestoreOperationStore, record_store)
    raise click.ClickException(
        "Odoo production backup restore requires DB-backed operation-record storage."
    )


def find_odoo_prod_backup_restore_operation_by_idempotency_key(
    *,
    operation_store: OdooProdBackupRestoreOperationStore,
    idempotency_key: str,
    idempotency_scope: str,
) -> OdooProdBackupRestoreOperationRecord | None:
    records = operation_store.list_odoo_prod_backup_restore_operation_records(
        idempotency_key=idempotency_key,
        idempotency_scope=idempotency_scope,
        limit=1,
    )
    return records[0] if records else None


def _require_lane_not_busy(
    *,
    operation_store: OdooProdBackupRestoreOperationStore,
    product: str,
    context: str,
    instance: str,
) -> None:
    active_bootstraps = operation_store.list_odoo_stable_bootstrap_operation_records(
        product=product,
        context_name=context,
        instance_name=instance,
        statuses=("pending", "running"),
        limit=1,
    )
    active_replacements = operation_store.list_odoo_stable_target_replacement_operation_records(
        product=product,
        context_name=context,
        instance_name=instance,
        statuses=("pending", "running"),
        limit=1,
    )
    if active_bootstraps or active_replacements:
        raise OdooProdBackupRestoreLaneBusyError(
            "Odoo production backup restore cannot overlap another stable-lane operation."
        )


def odoo_prod_backup_restore_operation_payload(
    operation: OdooProdBackupRestoreOperationRecord,
) -> dict[str, object]:
    return operation.model_dump(mode="json")


def _operation_records(
    operation: OdooProdBackupRestoreOperationRecord,
) -> dict[str, object]:
    records: dict[str, object] = {
        "odoo_prod_backup_restore_operation_id": operation.operation_id,
    }
    if operation.deployment_record_id:
        records["deployment_record_id"] = operation.deployment_record_id
    if operation.result is not None and operation.result.release_tuple_id:
        records["release_tuple_id"] = operation.result.release_tuple_id
    return records
