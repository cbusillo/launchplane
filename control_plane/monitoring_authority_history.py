from typing import Literal
from collections.abc import Iterable

from pydantic import AwareDatetime, BaseModel, ConfigDict, JsonValue

from control_plane.contracts.read_coverage import ReadCoverage
from control_plane.contracts.canonical_json import nonvalidating_compact_sorted_json_sha256
from control_plane.lane_health_history import HistoryObservation


class MonitoringAuthorityUnavailable(RuntimeError):
    pass


class MonitoringAuthorityQueryError(ValueError):
    pass


class MonitoringAuthorityBatchStart(BaseModel):
    model_config = ConfigDict(extra="forbid")
    started_at: AwareDatetime
    cluster_id: str
    epoch_sequence: int
    capture_errors: tuple[str, ...]


class MonitoringAuthorityLaneEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    product: str
    context: str
    instance: str
    observation_ids: tuple[str, ...]
    observations_sha256: str

    def confirmed_observations(
        self, records: Iterable[HistoryObservation]
    ) -> tuple[HistoryObservation, ...]:
        selected = tuple(record for record in records if record.record_id in self.observation_ids)
        if (
            tuple(sorted(record.record_id for record in selected)) != self.observation_ids
            or monitoring_observations_sha256(selected) != self.observations_sha256
        ):
            raise ValueError("The completed batch's retained observations are missing or changed")
        return selected


def monitoring_observations_sha256(records: Iterable[HistoryObservation]) -> str:
    return nonvalidating_compact_sorted_json_sha256(
        [
            record.model_dump(mode="json")
            for record in sorted(records, key=lambda row: row.record_id)
        ]
    )


class MonitoringAuthorityRevision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sequence: int
    recorded_at: AwareDatetime
    transaction_id: int
    source: str
    source_key: str
    product: str
    context: str
    instance: str
    operation: Literal["epoch", "seed", "insert", "update", "delete", "confirmation"]
    scope_visible: bool = True
    material_sha256: str
    payload: dict[str, JsonValue]


class MonitoringAuthorityHistory(BaseModel):
    model_config = ConfigDict(extra="forbid")
    product: str
    read_at: AwareDatetime
    cluster_id: str
    settled_before_transaction: int
    epoch: MonitoringAuthorityRevision | None
    confirmed_since: AwareDatetime | None
    capture_errors: tuple[str, ...]
    coverage: ReadCoverage
    cursor: str | None
    next_cursor: str | None
    revisions: tuple[MonitoringAuthorityRevision, ...]


class MonitoringAuthorityHistoryResponse(BaseModel):
    trace_id: str
    history: MonitoringAuthorityHistory
