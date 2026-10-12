from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


SNAPSHOT_MAX_ROWS = 10_000
SNAPSHOT_MAX_BYTES = 16 * 1024 * 1024
SNAPSHOT_MAX_AGE_SECONDS = 300
STORED_RECORD_SNAPSHOT_ROUTE = "/v1/service/stored-record-snapshot"


class StoredRecordSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    captured_at: str
    database_schema_revision: str
    tables: dict[str, list[dict[str, Any]]]


class StoredRecordSnapshotResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    snapshot: StoredRecordSnapshot


class StoredRecordPreflightResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    records_checked: int = Field(ge=1)
    database_schema_revision: str
