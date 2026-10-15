"""Value-free lane evidence for the service's disposable rebuild drill."""

from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


class OdooTestingDrillStorageBudget(BaseModel):
    """Admin-supplied working headroom, never an inferred source-size guarantee."""

    model_config = ConfigDict(extra="forbid", strict=True)

    database_bytes: int = Field(gt=0)
    filestore_bytes: int = Field(gt=0)
    staging_bytes: int = Field(gt=0)
    reserve_bytes: int = Field(gt=0)


class OdooTestingDrillEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["queued", "pass", "fail", "skipped"]
    observed_at: str
    operation_id: str = ""
    deployment_record_id: str = ""
    step: str = ""
    trace: str = ""
    alert_delivered: bool = False
    disposal_pending: int = Field(default=0, ge=0)
    storage_status: Literal["unknown", "pass", "insufficient", "unconfigured"] = "unknown"
