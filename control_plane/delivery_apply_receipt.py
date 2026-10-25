"""Exact completed-receipt lookup for DB-only service delivery Applies."""

from collections.abc import Callable
from datetime import UTC, datetime
import hashlib
import json

from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError

from control_plane.contracts.idempotency_record import LaunchplaneIdempotencyRecord
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.storage.product_authority_bundle import ProductAuthorityBundle


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


def apply_reviewed_service_bundle(
    *,
    store: PostgresRecordStore,
    request: BaseModel,
    actor: str,
    trace_id: str,
    idempotency_key: str,
    scope: str,
    route_path: str,
    plan: Callable[[], tuple[dict[str, object], ProductAuthorityBundle]],
) -> dict[str, object]:
    fields = request.model_dump(mode="json")
    applying = fields["mode"] == "apply"

    def completed_original() -> dict[str, object] | None:
        stored = read_delivery_apply_receipt(
            store=store,
            request=request,
            actor=actor,
            scope=scope,
            route_path=route_path,
            idempotency_key=idempotency_key,
        )
        return dict(stored.response_payload) if stored is not None else None

    if applying:
        if not idempotency_key.strip():
            raise ValueError("Apply requires Idempotency-Key.")
        if (previous := completed_original()) is not None:
            return previous
    try:
        summary, bundle = plan()
        summary["trace_id"] = trace_id
        if not applying:
            return summary
        if fields["expected_plan_digest"] != summary["plan_digest"]:
            raise ValueError("The configuration changed since the reviewed dry-run.")
        receipt = LaunchplaneIdempotencyRecord(
            record_id=f"idempotency-{trace_id}",
            scope=scope,
            route_path=route_path,
            idempotency_key=idempotency_key,
            request_fingerprint=delivery_request_fingerprint(request),
            response_status_code=200,
            response_trace_id=trace_id,
            recorded_at=datetime.now(UTC).isoformat(),
            response_payload=summary,
        )
        store.write_product_authority_bundle(
            bundle.model_copy(update={"idempotency_record": receipt})
        )
        return summary
    except (ValueError, FileNotFoundError, IntegrityError):
        if applying and (previous := completed_original()) is not None:
            return previous
        raise
