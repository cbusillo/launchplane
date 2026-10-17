"""Rebuild checkpoints in existing record storage, separate from provider fences."""

from typing import Protocol

from control_plane.contracts.idempotency_record import (
    LaunchplaneIdempotencyRecord,
    build_launchplane_mutation_reservation_id,
)
from control_plane.contracts.testing_rebuild import TestingRebuildRecord

REBUILD_CHECKPOINT_SCOPE = "launchplane:testing-rebuild-checkpoint:v1"
REBUILD_CHECKPOINT_ROUTE = "service:testing-rebuild-checkpoint"


class RebuildRecordStore(Protocol):
    def read_idempotency_record(
        self, *, scope: str, route_path: str, idempotency_key: str
    ) -> LaunchplaneIdempotencyRecord | None: ...
    def write_idempotency_record(self, record: LaunchplaneIdempotencyRecord) -> object: ...


class RecordTestingRebuildStore:
    """The caller holds the lane/provider lease across load and save.

    These are evidence checkpoints, never mutation reservations or permission
    to reclaim a provider effect. Existing durable provider fences remain the
    authority for whether an interrupted dispatch can be reconciled or retried.
    """

    def __init__(self, store: RebuildRecordStore) -> None:
        self.store = store

    def load(self, operation_id: str) -> TestingRebuildRecord:
        row = self.store.read_idempotency_record(
            scope=REBUILD_CHECKPOINT_SCOPE,
            route_path=REBUILD_CHECKPOINT_ROUTE,
            idempotency_key=operation_id,
        )
        if row is None:
            raise FileNotFoundError(operation_id)
        record = TestingRebuildRecord.model_validate(row.response_payload)
        if (
            record.plan.operation_id != operation_id
            or row.request_fingerprint != record.plan.digest
        ):
            raise ValueError("The stored rebuild checkpoint has a different operation binding.")
        return record

    def save(self, record: TestingRebuildRecord) -> None:
        record = TestingRebuildRecord.model_validate(record.model_dump())
        try:
            current = self.load(record.plan.operation_id)
        except FileNotFoundError:
            current = None
        if current is not None and (
            current.plan != record.plan or current.began_at != record.began_at
        ):
            raise ValueError("Cannot replace a rebuild's original operation or plan.")
        self.store.write_idempotency_record(
            LaunchplaneIdempotencyRecord(
                record_id=build_launchplane_mutation_reservation_id(
                    scope=REBUILD_CHECKPOINT_SCOPE,
                    route_path=REBUILD_CHECKPOINT_ROUTE,
                    idempotency_key=record.plan.operation_id,
                ),
                scope=REBUILD_CHECKPOINT_SCOPE,
                route_path=REBUILD_CHECKPOINT_ROUTE,
                idempotency_key=record.plan.operation_id,
                request_fingerprint=record.plan.digest,
                created_at=record.began_at.isoformat(),
                updated_at=record.updated_at.isoformat(),
                response_status_code=200,
                response_trace_id="testing-rebuild:" + record.plan.operation_id,
                recorded_at=record.updated_at.isoformat(),
                response_payload=record.model_dump(mode="json"),
            )
        )
