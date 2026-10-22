from control_plane.contracts.idempotency_record import (
    LaunchplaneIdempotencyRecord,
    build_launchplane_mutation_reservation,
    build_launchplane_mutation_reservation_id,
)
from control_plane.storage.postgres import (
    ORIGINAL_MUTATION_INTENT_ROUTE,
    MutationReservationResult,
    PostgresRecordStore,
)


def reserve_historical_mutation(
    store: PostgresRecordStore,
    *,
    scope: str,
    route_path: str,
    idempotency_key: str,
    request_fingerprint: str,
    lease_owner: str,
    lease_seconds: int = 300,
    reconciliation_key: str = "",
    provider_target_key: str = "",
    original_intent: dict[str, object] | None = None,
) -> MutationReservationResult:
    with store._session_factory() as session:
        timestamp = store._database_mutation_timestamp(session)
        record = build_launchplane_mutation_reservation(
            scope=scope,
            route_path=route_path,
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint,
            lease_owner=lease_owner,
            reserved_at=timestamp,
            lease_expires_at=store._mutation_lease_expiry(
                observed_at=timestamp, lease_seconds=lease_seconds
            ),
            reconciliation_key=reconciliation_key,
            provider_target_key=provider_target_key,
        )
        session.add(store._idempotency_row(record))
        if original_intent:
            receipt = LaunchplaneIdempotencyRecord(
                record_id=build_launchplane_mutation_reservation_id(
                    scope=scope,
                    route_path=ORIGINAL_MUTATION_INTENT_ROUTE,
                    idempotency_key=record.record_id,
                ),
                scope=scope,
                route_path=ORIGINAL_MUTATION_INTENT_ROUTE,
                idempotency_key=record.record_id,
                request_fingerprint=request_fingerprint,
                response_status_code=202,
                response_trace_id=lease_owner,
                recorded_at=timestamp,
                response_payload={
                    "reservation_id": record.record_id,
                    "original_intent": original_intent,
                },
            )
            session.add(store._idempotency_row(receipt))
        session.commit()
    return MutationReservationResult(status="acquired", record=record)
