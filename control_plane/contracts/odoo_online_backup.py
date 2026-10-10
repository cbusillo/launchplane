"""Bounded evidence for a coherent online Odoo recovery tuple."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class OdooProdBackupCaptureEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal[2] = 2
    backup_nonce: str = Field(pattern=r"^[0-9a-f]{64}$")
    backup_record_id: str = Field(min_length=1)
    database_name: str = Field(min_length=1)
    database_dump_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    filestore_archive_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    database_dump_size: int = Field(gt=0)
    filestore_archive_size: int = Field(gt=0)
    consistency_protocol: Literal["postgres-exported-snapshot-odoo-hardlinks-v1"]
    postgres_snapshot_id: str = Field(pattern=r"^[0-9A-Fa-f]+-[0-9A-Fa-f]+-[0-9]+$")
    recovery_point_at: str
    image_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    attachment_count: int = Field(ge=0)
    attachment_file_count: int = Field(ge=0)
    attachment_inventory_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _validate_online_tuple(self) -> "OdooProdBackupCaptureEvidence":
        if datetime.fromisoformat(self.recovery_point_at).utcoffset() is None:
            raise ValueError("Online backup recovery point must include its timezone.")
        if self.attachment_file_count > self.attachment_count:
            raise ValueError("Online backup file count exceeds referenced attachments.")
        return self
