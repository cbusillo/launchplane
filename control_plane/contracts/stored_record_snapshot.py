from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


SNAPSHOT_MAX_ROWS = 10_000
SNAPSHOT_MAX_BYTES = 16 * 1024 * 1024
# Full policy copies at ~400 revisions exceed the wire budget. Compression
# preserves every revision; expansion remains bounded independently of the wire.
SNAPSHOT_MAX_EXPANDED_BYTES = 128 * 1024 * 1024
SNAPSHOT_MAX_AGE_SECONDS = 300
STORED_RECORD_SNAPSHOT_ROUTE = "/v1/service/stored-record-snapshot"


class StoredRecordSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    captured_at: str
    database_schema_revision: str
    tables: dict[str, list[dict[str, Any]]]


class CompressedStoredRecordSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[2] = 2
    captured_at: str
    database_schema_revision: str
    encoding: Literal["gzip-ndjson-base64"] = "gzip-ndjson-base64"
    table_counts: dict[str, int] = Field(default_factory=dict)
    expanded_bytes: int = Field(ge=1, le=SNAPSHOT_MAX_EXPANDED_BYTES)
    rows_gzip_base64: str


class StoredRecordSnapshotResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    snapshot: StoredRecordSnapshot | CompressedStoredRecordSnapshot


class StoredRecordPreflightResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    records_checked: int = Field(ge=1)
    database_schema_revision: str
