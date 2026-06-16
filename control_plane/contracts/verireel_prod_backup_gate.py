from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

DEFAULT_VERIREEL_PROD_BACKUP_GATE_TIMEOUT_SECONDS = 1800


class VeriReelProdBackupGateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    context: str = "verireel"
    instance: str = "prod"
    backup_record_id: str
    timeout_seconds: int = Field(
        default=DEFAULT_VERIREEL_PROD_BACKUP_GATE_TIMEOUT_SECONDS, ge=1
    )

    @model_validator(mode="after")
    def _validate_request(self) -> "VeriReelProdBackupGateRequest":
        if not self.context.strip():
            raise ValueError("VeriReel prod backup gate requires context.")
        if self.instance != "prod":
            raise ValueError("VeriReel prod backup gate requires instance 'prod'.")
        if not self.backup_record_id.strip():
            raise ValueError("VeriReel prod backup gate requires backup_record_id.")
        return self


class VeriReelProdBackupGateWorkerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    context: str
    instance: str
    backup_record_id: str
    timeout_seconds: int = Field(
        default=DEFAULT_VERIREEL_PROD_BACKUP_GATE_TIMEOUT_SECONDS, ge=1
    )


class VeriReelProdBackupGateWorkerResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    status: str
    snapshot_name: str = ""
    started_at: str = ""
    finished_at: str = ""
    detail: str = ""
    evidence: dict[str, str] = Field(default_factory=dict)


class VeriReelProdBackupGateResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    backup_record_id: str
    backup_status: str
    backup_started_at: str = ""
    backup_finished_at: str = ""
    snapshot_name: str = ""
    error_message: str = ""
