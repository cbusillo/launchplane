from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, JsonValue

from control_plane.contracts.read_coverage import ReadCoverage


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
