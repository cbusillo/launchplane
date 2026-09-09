from __future__ import annotations

from pydantic import TypeAdapter

from control_plane.contracts.canonical_json import canonical_json_bytes
from control_plane.contracts.ordinary_agent import (
    OrdinaryAgentEffectRecord,
    OrdinaryAgentEligibilityResult,
    OrdinaryAgentPolicySnapshot,
    StoredOrdinaryAgentResult,
)


class OrdinaryAgentResultConflictError(ValueError):
    pass


_RESULT_ADAPTER: TypeAdapter[StoredOrdinaryAgentResult] = TypeAdapter(
    OrdinaryAgentEligibilityResult | OrdinaryAgentEffectRecord
)


class TestOrdinaryAgentEvidenceStore:
    __test__ = False

    def __init__(self, *, snapshots: tuple[OrdinaryAgentPolicySnapshot, ...] = ()):
        self._snapshots = {snapshot.record_id: snapshot for snapshot in snapshots}
        self._results: dict[str, bytes] = {}

    def read_snapshot(self, record_id: str) -> OrdinaryAgentPolicySnapshot | None:
        return self._snapshots.get(record_id)

    def put_result(self, result: StoredOrdinaryAgentResult) -> StoredOrdinaryAgentResult:
        payload = canonical_json_bytes(result.model_dump(mode="json"))
        existing = self._results.get(result.record_id)
        if existing is not None:
            if existing != payload:
                raise OrdinaryAgentResultConflictError(
                    f"ordinary agent result record ID conflict: {result.record_id}"
                )
            return self.get_result(result.record_id) or result
        self._results[result.record_id] = payload
        return self.get_result(result.record_id) or result

    def get_result(self, record_id: str) -> StoredOrdinaryAgentResult | None:
        payload = self._results.get(record_id)
        if payload is None:
            return None
        return _RESULT_ADAPTER.validate_json(payload)
