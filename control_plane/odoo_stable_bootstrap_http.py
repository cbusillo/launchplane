from __future__ import annotations

from typing import Protocol, cast

import click
from pydantic import Field, model_validator

from control_plane.contracts.odoo_stable_bootstrap import OdooStableBootstrapRequest
from control_plane.contracts.odoo_stable_bootstrap_operation import (
    OdooStableBootstrapOperationRecord,
    build_odoo_stable_bootstrap_operation_id,
)
from control_plane.drivers.dispatch import (
    _ProductRouteEnvelope,
    _validate_driver_envelope_product,
)
from control_plane.odoo_product_driver_http import (
    OdooProductMismatchError,
    OdooRouteDependencyError,
    resolve_odoo_product_route,
)


ODOO_STABLE_BOOTSTRAP_ROUTE = "/v1/drivers/odoo/stable-bootstrap"
ODOO_STABLE_BOOTSTRAP_ACTION = "odoo_stable_bootstrap.execute"


class OdooStableBootstrapProductMismatchError(OdooProductMismatchError):
    pass


class OdooStableBootstrapRouteDependencyError(OdooRouteDependencyError):
    pass


class OdooStableBootstrapIdempotencyKeyReusedError(ValueError):
    pass


class OdooStableBootstrapOperationActiveError(ValueError):
    def __init__(self, operation: OdooStableBootstrapOperationRecord) -> None:
        super().__init__(
            "An Odoo stable bootstrap operation is already active for this product/context/instance."
        )
        self.operation = operation


class OdooStableBootstrapOperationStore(Protocol):
    def write_odoo_stable_bootstrap_operation_record(
        self, record: OdooStableBootstrapOperationRecord
    ) -> object: ...

    def create_odoo_stable_bootstrap_operation_record_if_no_active_lane(
        self, record: OdooStableBootstrapOperationRecord
    ) -> tuple[OdooStableBootstrapOperationRecord, bool]: ...

    def read_odoo_stable_bootstrap_operation_record(
        self, operation_id: str
    ) -> OdooStableBootstrapOperationRecord: ...

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


class OdooStableBootstrapEnvelope(_ProductRouteEnvelope):
    schema_version: int = Field(default=1, ge=1)
    bootstrap: OdooStableBootstrapRequest

    @model_validator(mode="after")
    def _validate_alignment(self) -> "OdooStableBootstrapEnvelope":
        _validate_driver_envelope_product(self.product, label="Odoo stable bootstrap")
        if self.product.strip() != self.bootstrap.product.strip():
            raise ValueError("Odoo stable bootstrap requires matching product values.")
        return self


def resolve_odoo_stable_bootstrap_product_route(
    *,
    record_store: object,
    product: str,
    context: str,
    instance: str,
) -> None:
    try:
        resolve_odoo_product_route(
            record_store=record_store,
            product=product,
            context=context,
            instance=instance,
        )
    except OdooRouteDependencyError as error:
        raise OdooStableBootstrapRouteDependencyError from error
    except OdooProductMismatchError as error:
        raise OdooStableBootstrapProductMismatchError from error


def enqueue_odoo_stable_bootstrap_operation(
    *,
    record_store: object,
    request: OdooStableBootstrapEnvelope,
    idempotency_key: str,
    request_fingerprint: str,
    created_at: str,
) -> tuple[dict[str, object], dict[str, object]]:
    operation_store = odoo_stable_bootstrap_operation_store(record_store)
    existing_operation = find_odoo_stable_bootstrap_operation_by_idempotency_key(
        operation_store=operation_store,
        product=request.product,
        context=request.bootstrap.context,
        instance=request.bootstrap.instance,
        idempotency_key=idempotency_key,
    )
    if existing_operation is not None:
        if existing_operation.request_fingerprint != request_fingerprint:
            raise OdooStableBootstrapIdempotencyKeyReusedError
        return _stable_bootstrap_records(existing_operation), operation_payload(existing_operation)

    operation = build_odoo_stable_bootstrap_operation_record(
        bootstrap_request=request.bootstrap,
        idempotency_key=idempotency_key,
        request_fingerprint=request_fingerprint,
        created_at=created_at,
    )
    operation, created_operation = (
        operation_store.create_odoo_stable_bootstrap_operation_record_if_no_active_lane(operation)
    )
    if not created_operation:
        if operation.idempotency_key == idempotency_key:
            if operation.request_fingerprint != request_fingerprint:
                raise OdooStableBootstrapIdempotencyKeyReusedError
            return _stable_bootstrap_records(operation), operation_payload(operation)
        raise OdooStableBootstrapOperationActiveError(operation)

    return _stable_bootstrap_records(operation), operation_payload(operation)


def operation_payload(operation: OdooStableBootstrapOperationRecord) -> dict[str, object]:
    payload = operation.model_dump(mode="json")
    payload["poll_url"] = odoo_stable_bootstrap_operation_poll_url(operation.operation_id)
    return payload


def odoo_stable_bootstrap_operation_poll_url(operation_id: str) -> str:
    return f"/v1/drivers/odoo/stable-bootstrap/operations/{operation_id.strip()}"


def odoo_stable_bootstrap_operation_store(
    record_store: object,
) -> OdooStableBootstrapOperationStore:
    required_methods = (
        "write_odoo_stable_bootstrap_operation_record",
        "create_odoo_stable_bootstrap_operation_record_if_no_active_lane",
        "read_odoo_stable_bootstrap_operation_record",
        "list_odoo_stable_bootstrap_operation_records",
    )
    if all(hasattr(record_store, method_name) for method_name in required_methods):
        return cast(OdooStableBootstrapOperationStore, record_store)
    raise click.ClickException(
        "Odoo stable bootstrap operations require Launchplane operation-record storage."
    )


def find_odoo_stable_bootstrap_operation_by_idempotency_key(
    *,
    operation_store: OdooStableBootstrapOperationStore,
    product: str,
    context: str,
    instance: str,
    idempotency_key: str,
) -> OdooStableBootstrapOperationRecord | None:
    records = operation_store.list_odoo_stable_bootstrap_operation_records(
        product=product,
        context_name=context,
        instance_name=instance,
        idempotency_key=idempotency_key,
        limit=1,
    )
    return records[0] if records else None


def build_odoo_stable_bootstrap_operation_record(
    *,
    bootstrap_request: OdooStableBootstrapRequest,
    idempotency_key: str,
    request_fingerprint: str,
    created_at: str,
) -> OdooStableBootstrapOperationRecord:
    return OdooStableBootstrapOperationRecord(
        operation_id=build_odoo_stable_bootstrap_operation_id(
            product=bootstrap_request.product,
            context=bootstrap_request.context,
            instance=bootstrap_request.instance,
            created_at=created_at,
            idempotency_key=idempotency_key,
        ),
        product=bootstrap_request.product,
        context=bootstrap_request.context,
        instance=bootstrap_request.instance,
        idempotency_key=idempotency_key,
        request_fingerprint=request_fingerprint,
        request=bootstrap_request,
        status="pending",
        phase="created",
        created_at=created_at,
        updated_at=created_at,
    )


def _stable_bootstrap_records(
    operation: OdooStableBootstrapOperationRecord,
) -> dict[str, object]:
    records: dict[str, object] = {"odoo_stable_bootstrap_operation_id": operation.operation_id}
    if operation.deployment_record_id:
        records["deployment_record_id"] = operation.deployment_record_id
    return records
