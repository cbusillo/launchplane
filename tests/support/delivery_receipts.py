from control_plane.contracts.idempotency_record import LaunchplaneIdempotencyRecord
from control_plane.production_delivery_monitor import Store, _candidate_key, _SCOPE, _ROUTE


def _write_receipt(store: Store, *, key: str, now: str, payload: dict[str, object]) -> None:
    store.write_idempotency_record(
        LaunchplaneIdempotencyRecord(
            record_id="production-delivery-" + _candidate_key(key),
            scope=_SCOPE,
            route_path=_ROUTE,
            idempotency_key=key,
            request_fingerprint="production-delivery-monitor-v1",
            response_status_code=200,
            response_trace_id="production-delivery-monitor:" + _candidate_key(key),
            recorded_at=now,
            response_payload=payload,
        )
    )
