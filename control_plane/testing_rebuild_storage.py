"""Rebuild checkpoints in existing record storage, separate from provider fences."""

from typing import Protocol

from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.idempotency_record import (
    LaunchplaneIdempotencyRecord,
    build_launchplane_mutation_reservation_id,
)
from control_plane.contracts.testing_rebuild import TestingRebuildPlan, TestingRebuildRecord

REBUILD_CHECKPOINT_SCOPE = "launchplane:testing-rebuild-checkpoint:v1"
REBUILD_CHECKPOINT_ROUTE = "service:testing-rebuild-checkpoint"


class RebuildRecordStore(Protocol):
    def read_idempotency_record(
        self, *, scope: str, route_path: str, idempotency_key: str
    ) -> LaunchplaneIdempotencyRecord | None: ...
    def compare_and_write_testing_rebuild_checkpoint(
        self,
        expected: TestingRebuildRecord | None,
        replacement: LaunchplaneIdempotencyRecord,
    ) -> None: ...


class RecordTestingRebuildStore:
    """The caller holds the lane/provider lease across load and save.

    These are evidence checkpoints, never mutation reservations or permission
    to reclaim a provider effect. Existing durable provider fences remain the
    authority for whether an interrupted dispatch can be reconciled or retried.
    """

    def __init__(self, store: RebuildRecordStore) -> None:
        self.store = store

    def load(self, plan: TestingRebuildPlan) -> TestingRebuildRecord:
        key = canonical_json_sha256(
            {"product": plan.product, "context": plan.context, "operation": plan.operation_id}
        )
        row = self.store.read_idempotency_record(
            scope=REBUILD_CHECKPOINT_SCOPE,
            route_path=REBUILD_CHECKPOINT_ROUTE,
            idempotency_key=key,
        )
        if row is None:
            raise FileNotFoundError(plan.operation_id)
        record = TestingRebuildRecord.model_validate(row.response_payload)
        if (record.plan.product, record.plan.context, record.plan.operation_id) != (
            plan.product,
            plan.context,
            plan.operation_id,
        ) or row.request_fingerprint != record.plan.digest:
            raise ValueError("The stored rebuild checkpoint has a different operation binding.")
        return record

    def save(self, record: TestingRebuildRecord) -> None:
        record = TestingRebuildRecord.model_validate(record.model_dump())
        try:
            current = self.load(record.plan)
        except FileNotFoundError:
            current = None
        require_rebuild_successor(current, record)
        key = canonical_json_sha256(
            {
                "product": record.plan.product,
                "context": record.plan.context,
                "operation": record.plan.operation_id,
            }
        )
        self.store.compare_and_write_testing_rebuild_checkpoint(
            current,
            LaunchplaneIdempotencyRecord(
                record_id=build_launchplane_mutation_reservation_id(
                    scope=REBUILD_CHECKPOINT_SCOPE,
                    route_path=REBUILD_CHECKPOINT_ROUTE,
                    idempotency_key=key,
                ),
                scope=REBUILD_CHECKPOINT_SCOPE,
                route_path=REBUILD_CHECKPOINT_ROUTE,
                idempotency_key=key,
                request_fingerprint=record.plan.digest,
                created_at=record.began_at.isoformat(),
                updated_at=record.updated_at.isoformat(),
                response_status_code=200,
                response_trace_id="testing-rebuild:" + record.plan.operation_id,
                recorded_at=record.updated_at.isoformat(),
                response_payload=record.model_dump(mode="json"),
            ),
        )


def require_rebuild_successor(
    current: TestingRebuildRecord | None, record: TestingRebuildRecord
) -> None:
    """Check monotonic evidence before and within the checkpoint transaction."""
    if current is not None and (current.plan != record.plan or current.began_at != record.began_at):
        raise ValueError("Cannot replace a rebuild's original operation or plan.")
    if current is not None:
        if current.step in {"complete", "failed"}:
            if record != current:
                raise ValueError("A terminal rebuild receipt cannot be replaced.")
            return
        for flag in ("publication_started", "retirement_started", "rollback_started"):
            if getattr(current, flag) and not getattr(record, flag):
                raise ValueError("A rebuild cannot forget an effect intent.")
        steps = (
            "preparing",
            "prepared",
            "switching",
            "publishing",
            "retiring",
            "complete",
            "rolling_back",
            "failed",
        )
        if steps.index(record.step) < steps.index(current.step):
            raise ValueError("A rebuild checkpoint cannot regress.")
        if current.retirement_recovery is not None and (
            record.retirement_recovery != current.retirement_recovery
        ):
            raise ValueError("A rebuild cannot replace its retirement recovery proof.")
        if current.preparation is not None and record.preparation != current.preparation:
            raise ValueError("A rebuild cannot replace its independent-copy receipt.")
        if current.error and record.error != current.error:
            raise ValueError("A rebuild cannot replace its recorded failure.")
        if (
            current.private_readiness_failed_at is not None
            and record.private_readiness_failed_at != current.private_readiness_failed_at
        ):
            raise ValueError("A rebuild cannot replace its first private probe failure.")
