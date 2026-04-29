from typing import Literal

from pydantic import BaseModel, ConfigDict


FreshnessStatus = Literal["verified", "recorded", "stale", "missing", "unsupported"]
SourceKind = Literal["record", "provider", "descriptor", "unsupported"]


class DataProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_kind: SourceKind
    source_record_id: str = ""
    recorded_at: str = ""
    refreshed_at: str = ""
    freshness_status: FreshnessStatus
    stale_after: str = ""
    detail: str = ""
