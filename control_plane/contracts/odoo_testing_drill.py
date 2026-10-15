"""Value-free lane evidence for the service's disposable rebuild drill."""

from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator
import re


class OdooTestingDrillStorageBudget(BaseModel):
    """Admin-supplied working headroom, never an inferred source-size guarantee."""

    model_config = ConfigDict(extra="forbid", strict=True)

    database_bytes: int = Field(gt=0)
    filestore_bytes: int = Field(gt=0)
    staging_bytes: int = Field(gt=0)
    reserve_bytes: int = Field(gt=0)
    retained_copy_bindings: tuple[str, ...] = Field(
        default=(), strict=False, exclude_if=lambda value: not value
    )

    @field_validator("retained_copy_bindings")
    @classmethod
    def validate_retained_bindings(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)) or any(
            not re.fullmatch(r"[a-f0-9]{64}", value) for value in values
        ):
            raise ValueError("Retained copies require unique original committed copy bindings.")
        return values


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
    disposal_attention: int = Field(default=0, ge=0)
    disposal_pending_bindings: tuple[str, ...] = ()
    storage_status: Literal["unknown", "pass", "insufficient", "unconfigured"] = "unknown"
