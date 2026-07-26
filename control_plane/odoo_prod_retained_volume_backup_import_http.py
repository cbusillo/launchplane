from __future__ import annotations

from typing import Protocol, cast

import click
from pydantic import Field, model_validator

from control_plane.contracts.durable_operation_authorization import (
    DurableOperationAuthorization,
)
from control_plane.contracts.odoo_prod_backup_restore_operation import (
    OdooProdBackupRestoreOperationRecord,
)
from control_plane.contracts.odoo_prod_retained_volume_backup_import import (
    OdooProdRetainedVolumeBackupImportApplyRequest,
    OdooProdRetainedVolumeBackupImportPlan,
    OdooProdRetainedVolumeBackupImportRequest,
)
from control_plane.contracts.odoo_prod_retained_volume_backup_import_operation import (
    OdooProdRetainedVolumeBackupImportOperationKind,
    OdooProdRetainedVolumeBackupImportOperationRecord,
    build_odoo_prod_retained_volume_backup_import_operation_id,
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
from control_plane.odoo_stable_lane import OdooStableLaneOperationConflictError
from control_plane.workflows.odoo_prod_retained_volume_backup_import import (
    OdooProdRetainedVolumeBackupImportStore,
)


ODOO_PROD_RETAINED_VOLUME_BACKUP_IMPORT_PLAN_ROUTE = (
    "/v1/drivers/odoo/prod-retained-volume-backup-import-plan"
)
ODOO_PROD_RETAINED_VOLUME_BACKUP_IMPORT_APPLY_ROUTE = (
    "/v1/drivers/odoo/prod-retained-volume-backup-import-apply"
)
ODOO_PROD_RETAINED_VOLUME_BACKUP_IMPORT_OPERATION_STATUS_ROUTE = (
    "/v1/drivers/odoo/prod-retained-volume-backup-import/operations/{operation_id}"
)


class OdooProdRetainedVolumeBackupImportProductMismatchError(OdooProductMismatchError):
    pass


class OdooProdRetainedVolumeBackupImportRouteDependencyError(OdooRouteDependencyError):
    pass


class OdooProdRetainedVolumeBackupImportIdempotencyKeyReusedError(ValueError):
    pass


class OdooProdRetainedVolumeBackupImportPlanChangedError(ValueError):
    pass


class OdooProdRetainedVolumeBackupImportOperationActiveError(ValueError):
    def __init__(self, operation: OdooProdRetainedVolumeBackupImportOperationRecord) -> None:
        super().__init__(
            "An Odoo retained-volume backup import operation is already active for this lane."
        )
        self.operation = operation


class OdooProdRetainedVolumeBackupImportLaneBusyError(ValueError):
    pass


class OdooProdRetainedVolumeBackupImportPlanEnvelope(_ProductRouteEnvelope):
    schema_version: int = Field(default=1, ge=1)
    backup_import: OdooProdRetainedVolumeBackupImportRequest

    @model_validator(mode="after")
    def _validate_alignment(self) -> "OdooProdRetainedVolumeBackupImportPlanEnvelope":
        _validate_driver_envelope_product(
            self.product,
            label="Odoo retained-volume backup import plan",
        )
        if self.product.strip() != self.backup_import.product:
            raise ValueError(
                "Odoo retained-volume backup import plan requires matching product values."
            )
        return self


class OdooProdRetainedVolumeBackupImportApplyEnvelope(_ProductRouteEnvelope):
    schema_version: int = Field(default=1, ge=1)
    backup_import: OdooProdRetainedVolumeBackupImportApplyRequest

    @model_validator(mode="after")
    def _validate_alignment(self) -> "OdooProdRetainedVolumeBackupImportApplyEnvelope":
        _validate_driver_envelope_product(
            self.product,
            label="Odoo retained-volume backup import apply",
        )
        if self.product.strip() != self.backup_import.product:
            raise ValueError(
                "Odoo retained-volume backup import apply requires matching product values."
            )
        return self


class OdooProdRetainedVolumeBackupImportOperationStore(
    OdooProdRetainedVolumeBackupImportStore, Protocol
):
    def create_odoo_prod_retained_volume_backup_import_operation_record_if_no_active_lane(
        self, record: OdooProdRetainedVolumeBackupImportOperationRecord
    ) -> tuple[OdooProdRetainedVolumeBackupImportOperationRecord, bool]: ...

    def read_odoo_prod_retained_volume_backup_import_operation_record(
        self, operation_id: str
    ) -> OdooProdRetainedVolumeBackupImportOperationRecord: ...

    def list_odoo_prod_retained_volume_backup_import_operation_records(
        self,
        *,
        operation_kind: str = "",
        product: str = "",
        context_name: str = "",
        instance_name: str = "",
        idempotency_key: str = "",
        idempotency_scope: str = "",
        statuses: tuple[str, ...] = (),
        limit: int | None = None,
    ) -> tuple[OdooProdRetainedVolumeBackupImportOperationRecord, ...]: ...

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


def resolve_odoo_prod_retained_volume_backup_import_lane(
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
        raise OdooProdRetainedVolumeBackupImportRouteDependencyError from error
    except OdooProductMismatchError as error:
        raise OdooProdRetainedVolumeBackupImportProductMismatchError from error
    matching = tuple(
        lane
        for lane in profile.lanes
        if lane.instance.strip().lower() == "prod"
        and lane.context.strip().lower() == context.strip().lower()
    )
    if len(matching) != 1:
        raise OdooProdRetainedVolumeBackupImportProductMismatchError
    return matching[0]


def enqueue_odoo_prod_retained_volume_backup_import_plan_operation(
    *,
    record_store: object,
    request: OdooProdRetainedVolumeBackupImportPlanEnvelope,
    idempotency_key: str,
    idempotency_scope: str,
    request_fingerprint: str,
    created_at: str,
    authorization: DurableOperationAuthorization,
) -> tuple[dict[str, object], dict[str, object]]:
    operation_store = odoo_prod_retained_volume_backup_import_operation_store(record_store)
    existing = find_odoo_prod_retained_volume_backup_import_operation_by_idempotency_key(
        operation_store=operation_store,
        operation_kind="plan",
        idempotency_key=idempotency_key,
        idempotency_scope=idempotency_scope,
    )
    if existing is not None:
        if existing.request_fingerprint != request_fingerprint:
            raise OdooProdRetainedVolumeBackupImportIdempotencyKeyReusedError
        return _operation_records(existing), operation_payload(existing)
    _require_lane_not_busy(
        operation_store=operation_store,
        product=request.backup_import.product,
        context=request.backup_import.context,
        instance=request.backup_import.instance,
    )
    operation = OdooProdRetainedVolumeBackupImportOperationRecord(
        schema_version=2,
        operation_id=build_odoo_prod_retained_volume_backup_import_operation_id(
            operation_kind="plan",
            product=request.backup_import.product,
            context=request.backup_import.context,
            created_at=created_at,
            idempotency_key=idempotency_key,
            idempotency_scope=idempotency_scope,
        ),
        operation_kind="plan",
        product=request.backup_import.product,
        context=request.backup_import.context,
        instance=request.backup_import.instance,
        idempotency_key=idempotency_key,
        idempotency_scope=idempotency_scope,
        request_fingerprint=request_fingerprint,
        request=request.backup_import,
        authorization=authorization,
        status="pending",
        phase="created",
        created_at=created_at,
        updated_at=created_at,
    )
    try:
        persisted, created = (
            operation_store.create_odoo_prod_retained_volume_backup_import_operation_record_if_no_active_lane(
                operation
            )
        )
    except OdooStableLaneOperationConflictError as error:
        raise OdooProdRetainedVolumeBackupImportLaneBusyError(str(error)) from error
    if not created:
        raise OdooProdRetainedVolumeBackupImportOperationActiveError(persisted)
    return _operation_records(persisted), operation_payload(persisted)


def enqueue_odoo_prod_retained_volume_backup_import_apply_operation(
    *,
    record_store: object,
    request: OdooProdRetainedVolumeBackupImportApplyEnvelope,
    idempotency_key: str,
    idempotency_scope: str,
    request_fingerprint: str,
    created_at: str,
    authorization: DurableOperationAuthorization,
) -> tuple[dict[str, object], dict[str, object]]:
    operation_store = odoo_prod_retained_volume_backup_import_operation_store(record_store)
    existing = find_odoo_prod_retained_volume_backup_import_operation_by_idempotency_key(
        operation_store=operation_store,
        operation_kind="apply",
        idempotency_key=idempotency_key,
        idempotency_scope=idempotency_scope,
    )
    if existing is not None:
        if existing.request_fingerprint != request_fingerprint:
            raise OdooProdRetainedVolumeBackupImportIdempotencyKeyReusedError
        return _operation_records(existing), operation_payload(existing)

    apply_request = request.backup_import
    try:
        plan_operation = (
            operation_store.read_odoo_prod_retained_volume_backup_import_operation_record(
                apply_request.plan_operation_id
            )
        )
    except FileNotFoundError as error:
        raise OdooProdRetainedVolumeBackupImportPlanChangedError(
            "The reviewed retained-volume backup import plan operation was not found."
        ) from error
    if (
        plan_operation.operation_kind != "plan"
        or plan_operation.status != "pass"
        or not isinstance(plan_operation.result, OdooProdRetainedVolumeBackupImportPlan)
        or plan_operation.result.plan_status != "ready"
    ):
        raise OdooProdRetainedVolumeBackupImportPlanChangedError(
            "The reviewed retained-volume backup import plan operation is not ready."
        )
    reviewed_plan = plan_operation.result
    if reviewed_plan.plan_fingerprint != apply_request.plan_fingerprint:
        raise OdooProdRetainedVolumeBackupImportPlanChangedError(
            "The retained-volume backup import plan fingerprint changed before apply."
        )
    plan_request = OdooProdRetainedVolumeBackupImportRequest.model_validate(
        apply_request.model_dump(
            mode="json",
            exclude={
                "plan_operation_id",
                "plan_fingerprint",
                "confirmation",
                "timeout_seconds",
            },
        )
    )
    if plan_operation.request != plan_request:
        raise OdooProdRetainedVolumeBackupImportPlanChangedError(
            "The retained-volume backup import apply request differs from the reviewed plan."
        )
    _require_lane_not_busy(
        operation_store=operation_store,
        product=apply_request.product,
        context=apply_request.context,
        instance=apply_request.instance,
    )
    operation = OdooProdRetainedVolumeBackupImportOperationRecord(
        schema_version=2,
        operation_id=build_odoo_prod_retained_volume_backup_import_operation_id(
            operation_kind="apply",
            product=apply_request.product,
            context=apply_request.context,
            created_at=created_at,
            idempotency_key=idempotency_key,
            idempotency_scope=idempotency_scope,
        ),
        operation_kind="apply",
        product=apply_request.product,
        context=apply_request.context,
        instance=apply_request.instance,
        idempotency_key=idempotency_key,
        idempotency_scope=idempotency_scope,
        request_fingerprint=request_fingerprint,
        request=apply_request,
        plan=reviewed_plan,
        authorization=authorization,
        status="pending",
        phase="created",
        created_at=created_at,
        updated_at=created_at,
    )
    try:
        persisted, created = (
            operation_store.create_odoo_prod_retained_volume_backup_import_operation_record_if_no_active_lane(
                operation
            )
        )
    except OdooStableLaneOperationConflictError as error:
        raise OdooProdRetainedVolumeBackupImportLaneBusyError(str(error)) from error
    if not created:
        raise OdooProdRetainedVolumeBackupImportOperationActiveError(persisted)
    return _operation_records(persisted), operation_payload(persisted)


def odoo_prod_retained_volume_backup_import_operation_store(
    record_store: object,
) -> OdooProdRetainedVolumeBackupImportOperationStore:
    required_methods = (
        "read_product_profile_record",
        "read_dokploy_target_record",
        "read_dokploy_target_id_record",
        "read_environment_inventory",
        "read_artifact_manifest",
        "read_backup_gate_record",
        "write_backup_gate_record",
        "list_runtime_environment_records",
        "create_odoo_prod_retained_volume_backup_import_operation_record_if_no_active_lane",
        "read_odoo_prod_retained_volume_backup_import_operation_record",
        "list_odoo_prod_retained_volume_backup_import_operation_records",
        "list_odoo_prod_backup_restore_operation_records",
        "list_odoo_stable_bootstrap_operation_records",
        "list_odoo_stable_target_replacement_operation_records",
    )
    if all(callable(getattr(record_store, method_name, None)) for method_name in required_methods):
        return cast(OdooProdRetainedVolumeBackupImportOperationStore, record_store)
    raise click.ClickException(
        "Odoo retained-volume backup import requires DB-backed operation-record storage."
    )


def find_odoo_prod_retained_volume_backup_import_operation_by_idempotency_key(
    *,
    operation_store: OdooProdRetainedVolumeBackupImportOperationStore,
    operation_kind: OdooProdRetainedVolumeBackupImportOperationKind,
    idempotency_key: str,
    idempotency_scope: str,
) -> OdooProdRetainedVolumeBackupImportOperationRecord | None:
    records = operation_store.list_odoo_prod_retained_volume_backup_import_operation_records(
        operation_kind=operation_kind,
        idempotency_key=idempotency_key,
        idempotency_scope=idempotency_scope,
        limit=1,
    )
    return records[0] if records else None


def operation_payload(
    operation: OdooProdRetainedVolumeBackupImportOperationRecord,
) -> dict[str, object]:
    payload = operation.model_dump(mode="json")
    payload["poll_url"] = ODOO_PROD_RETAINED_VOLUME_BACKUP_IMPORT_OPERATION_STATUS_ROUTE.format(
        operation_id=operation.operation_id
    )
    return payload


def _operation_records(
    operation: OdooProdRetainedVolumeBackupImportOperationRecord,
) -> dict[str, object]:
    records: dict[str, object] = {
        "odoo_prod_retained_volume_backup_import_operation_id": operation.operation_id,
        "backup_record_id": operation.request.backup_record_id,
    }
    if operation.operation_kind == "plan":
        records["plan_operation_id"] = operation.operation_id
    return records


def _require_lane_not_busy(
    *,
    operation_store: OdooProdRetainedVolumeBackupImportOperationStore,
    product: str,
    context: str,
    instance: str,
) -> None:
    active_imports = operation_store.list_odoo_prod_retained_volume_backup_import_operation_records(
        product=product,
        context_name=context,
        instance_name=instance,
        statuses=("pending", "running"),
        limit=1,
    )
    active_restores = operation_store.list_odoo_prod_backup_restore_operation_records(
        product=product,
        context_name=context,
        instance_name=instance,
        statuses=("pending", "running"),
        limit=1,
    )
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
    if active_imports or active_restores or active_bootstraps or active_replacements:
        raise OdooProdRetainedVolumeBackupImportLaneBusyError(
            "Odoo retained-volume backup import cannot overlap another stable-lane operation."
        )
