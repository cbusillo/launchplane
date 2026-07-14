from __future__ import annotations

from typing import Protocol, cast

import click
from pydantic import Field, model_validator

from control_plane.contracts.odoo_stable_target_replacement import (
    OdooStableTargetReplacementApplyRequest,
)
from control_plane.contracts.odoo_stable_target_replacement_operation import (
    OdooStableTargetReplacementOperationRecord,
    build_odoo_stable_target_replacement_operation_id,
)
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductLaneProfile,
)
from control_plane.drivers.dispatch import (
    _ProductRouteEnvelope,
    _validate_driver_envelope_product,
)
from control_plane.odoo_product_driver_http import (
    OdooProductMismatchError,
    OdooRouteDependencyError,
    product_profile_uses_odoo_driver,
)


ODOO_TARGET_REPLACEMENT_APPLY_ROUTE = "/v1/drivers/odoo/target-replacement-apply"


class OdooTargetReplacementApplyProductMismatchError(OdooProductMismatchError):
    pass


class OdooTargetReplacementApplyRouteDependencyError(OdooRouteDependencyError):
    pass


class OdooTargetReplacementApplyIdempotencyKeyReusedError(ValueError):
    pass


class OdooTargetReplacementApplyOperationActiveError(ValueError):
    def __init__(self, operation: OdooStableTargetReplacementOperationRecord) -> None:
        super().__init__(
            "An Odoo target replacement operation is already active for this product/context/instance."
        )
        self.operation = operation


class OdooTargetReplacementApplyOperationStore(Protocol):
    def write_odoo_stable_target_replacement_operation_record(
        self, record: OdooStableTargetReplacementOperationRecord
    ) -> object: ...

    def create_odoo_stable_target_replacement_operation_record_if_no_active_lane(
        self, record: OdooStableTargetReplacementOperationRecord
    ) -> tuple[OdooStableTargetReplacementOperationRecord, bool]: ...

    def read_odoo_stable_target_replacement_operation_record(
        self, operation_id: str
    ) -> OdooStableTargetReplacementOperationRecord: ...

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


class OdooTargetReplacementApplyEnvelope(_ProductRouteEnvelope):
    schema_version: int = Field(default=1, ge=1)
    replacement: OdooStableTargetReplacementApplyRequest

    @model_validator(mode="after")
    def _validate_alignment(self) -> "OdooTargetReplacementApplyEnvelope":
        _validate_driver_envelope_product(self.product, label="Odoo target replacement apply")
        if self.product.strip() != self.replacement.product.strip():
            raise ValueError("Odoo target replacement apply requires matching product values.")
        return self


def resolve_odoo_target_replacement_apply_lane(
    *, record_store: object, product: str, instance: str
) -> ProductLaneProfile:
    read_profile = getattr(record_store, "read_product_profile_record", None)
    if not callable(read_profile):
        raise ValueError("Product driver validation requires product profile storage.")
    try:
        profile = read_profile(product.strip())
    except FileNotFoundError as error:
        raise OdooTargetReplacementApplyRouteDependencyError from error
    if not isinstance(profile, LaunchplaneProductProfileRecord):
        profile = LaunchplaneProductProfileRecord.model_validate(profile)
    if not product_profile_uses_odoo_driver(profile):
        raise OdooTargetReplacementApplyProductMismatchError(
            "Product is not configured for the requested Odoo driver route."
        )
    normalized_instance = instance.strip()
    for lane in profile.lanes:
        if lane.instance.strip() == normalized_instance:
            return lane
    raise OdooTargetReplacementApplyProductMismatchError(
        "Product profile does not own the requested Odoo driver lane."
    )


def enqueue_odoo_target_replacement_apply_operation(
    *,
    record_store: object,
    request: OdooTargetReplacementApplyEnvelope,
    context: str,
    idempotency_key: str,
    idempotency_scope: str,
    request_fingerprint: str,
    created_at: str,
) -> tuple[dict[str, object], dict[str, object]]:
    operation_store = odoo_target_replacement_apply_operation_store(record_store)
    existing_operation = find_odoo_target_replacement_apply_operation_by_idempotency_key(
        operation_store=operation_store,
        idempotency_key=idempotency_key,
        idempotency_scope=idempotency_scope,
    )
    if existing_operation is not None:
        if existing_operation.request_fingerprint != request_fingerprint:
            raise OdooTargetReplacementApplyIdempotencyKeyReusedError
        return _target_replacement_apply_records(existing_operation), operation_payload(
            existing_operation
        )

    operation = build_odoo_target_replacement_apply_operation_record(
        replacement_request=request.replacement,
        context=context,
        idempotency_key=idempotency_key,
        idempotency_scope=idempotency_scope,
        request_fingerprint=request_fingerprint,
        created_at=created_at,
    )
    operation, created_operation = (
        operation_store.create_odoo_stable_target_replacement_operation_record_if_no_active_lane(
            operation
        )
    )
    if not created_operation:
        if (
            operation.idempotency_key == idempotency_key
            and operation.idempotency_scope == idempotency_scope
        ):
            if operation.request_fingerprint != request_fingerprint:
                raise OdooTargetReplacementApplyIdempotencyKeyReusedError
            return _target_replacement_apply_records(operation), operation_payload(operation)
        raise OdooTargetReplacementApplyOperationActiveError(operation)

    return _target_replacement_apply_records(operation), operation_payload(operation)


def operation_payload(operation: OdooStableTargetReplacementOperationRecord) -> dict[str, object]:
    payload = operation.model_dump(mode="json")
    payload["poll_url"] = odoo_target_replacement_operation_poll_url(operation.operation_id)
    return payload


def odoo_target_replacement_operation_poll_url(operation_id: str) -> str:
    return f"/v1/drivers/odoo/target-replacement/operations/{operation_id.strip()}"


def odoo_target_replacement_apply_operation_store(
    record_store: object,
) -> OdooTargetReplacementApplyOperationStore:
    required_methods = (
        "write_odoo_stable_target_replacement_operation_record",
        "create_odoo_stable_target_replacement_operation_record_if_no_active_lane",
        "read_odoo_stable_target_replacement_operation_record",
        "list_odoo_stable_target_replacement_operation_records",
    )
    if all(hasattr(record_store, method_name) for method_name in required_methods):
        return cast(OdooTargetReplacementApplyOperationStore, record_store)
    raise click.ClickException(
        "Odoo stable target replacement operations require Launchplane operation-record storage."
    )


def find_odoo_target_replacement_apply_operation_by_idempotency_key(
    *,
    operation_store: OdooTargetReplacementApplyOperationStore,
    idempotency_key: str,
    idempotency_scope: str,
) -> OdooStableTargetReplacementOperationRecord | None:
    records = operation_store.list_odoo_stable_target_replacement_operation_records(
        idempotency_key=idempotency_key,
        idempotency_scope=idempotency_scope,
        limit=1,
    )
    return records[0] if records else None


def build_odoo_target_replacement_apply_operation_record(
    *,
    replacement_request: OdooStableTargetReplacementApplyRequest,
    context: str,
    idempotency_key: str,
    idempotency_scope: str,
    request_fingerprint: str,
    created_at: str,
) -> OdooStableTargetReplacementOperationRecord:
    return OdooStableTargetReplacementOperationRecord(
        operation_id=build_odoo_stable_target_replacement_operation_id(
            product=replacement_request.product,
            context=context,
            instance=replacement_request.instance,
            created_at=created_at,
            idempotency_key=idempotency_key,
            idempotency_scope=idempotency_scope,
        ),
        product=replacement_request.product,
        context=context,
        instance=replacement_request.instance,
        idempotency_key=idempotency_key,
        idempotency_scope=idempotency_scope,
        request_fingerprint=request_fingerprint,
        request=replacement_request,
        status="pending",
        phase="created",
        created_at=created_at,
        updated_at=created_at,
    )


def _target_replacement_apply_records(
    operation: OdooStableTargetReplacementOperationRecord,
) -> dict[str, object]:
    records: dict[str, object] = {
        "odoo_stable_target_replacement_operation_id": operation.operation_id
    }
    if operation.deployment_record_id:
        records["deployment_record_id"] = operation.deployment_record_id
    if operation.result is not None and operation.result.release_tuple_id:
        records["release_tuple_id"] = operation.result.release_tuple_id
    return records
