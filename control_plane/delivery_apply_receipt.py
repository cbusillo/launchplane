"""Exact completed-receipt lookup for DB-only service delivery Applies."""

import hashlib
import json

from pydantic import BaseModel

from control_plane.contracts.idempotency_record import LaunchplaneIdempotencyRecord
from control_plane.storage.postgres import PostgresRecordStore


def delivery_request_fingerprint(request: BaseModel) -> str:
    # Preserve the serialization used by already persisted receipts.
    return hashlib.sha256(
        json.dumps(request.model_dump(mode="json"), sort_keys=True).encode()
    ).hexdigest()


def read_delivery_apply_receipt(
    *,
    store: PostgresRecordStore,
    request: BaseModel,
    actor: str,
    scope: str,
    route_path: str,
    idempotency_key: str,
) -> LaunchplaneIdempotencyRecord | None:
    receipt = store.read_idempotency_record(
        scope=scope,
        route_path=route_path,
        idempotency_key=idempotency_key,
    )
    if receipt is None:
        return None
    payload = receipt.response_payload
    original = request.model_dump(mode="json")
    if (
        receipt.scope != scope
        or receipt.route_path != route_path
        or receipt.idempotency_key != idempotency_key
        or receipt.request_fingerprint != delivery_request_fingerprint(request)
        or receipt.state != "completed"
        or receipt.response_status_code != 200
        or not receipt.response_trace_id
        or payload.get("trace_id") != receipt.response_trace_id
        or payload.get("status") != "ok"
        or payload.get("actor") != actor
        or payload.get("mode") != "apply"
        or payload.get("plan_digest") != original.get("expected_plan_digest")
        or payload.get("reason") != original.get("reason")
    ):
        raise ValueError("Service delivery Apply idempotency conflict.")
    return receipt
