from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


BackupGateStatus = Literal["pending", "pass", "fail", "skipped"]


class BackupGateRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    record_id: str
    context: str
    instance: str
    created_at: str
    source: str
    required: bool = True
    status: BackupGateStatus = "pending"
    evidence: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_record(self) -> "BackupGateRecord":
        if not self.record_id.strip():
            raise ValueError("backup gate record requires record_id")
        if not self.context.strip():
            raise ValueError("backup gate record requires context")
        if not self.instance.strip():
            raise ValueError("backup gate record requires instance")
        if not self.created_at.strip():
            raise ValueError("backup gate record requires created_at")
        if not self.source.strip():
            raise ValueError("backup gate record requires source")
        if self.status == "pass" and not self.evidence:
            raise ValueError("passing backup gate record requires evidence")
        return self
