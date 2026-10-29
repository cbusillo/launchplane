from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, field_validator


_SAFE_NAME = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}")
_SHA256 = re.compile(r"[a-f0-9]{64}")


class RetainedVolumeBinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    volume_name: str
    server_id: str
    application_id: str
    application_name: str
    volume_identity_sha256: str = ""

    @field_validator("volume_name", "application_id", "application_name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        normalized = value.strip()
        if _SAFE_NAME.fullmatch(normalized) is None:
            raise ValueError("Retained volume custody requires a bounded path-safe name.")
        return normalized

    @field_validator("volume_identity_sha256")
    @classmethod
    def _validate_identity(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized and _SHA256.fullmatch(normalized) is None:
            raise ValueError("Retained volume identity must be empty or a SHA-256 digest.")
        return normalized

    @field_validator("server_id")
    @classmethod
    def _validate_server(cls, value: str) -> str:
        return cls._validate_name(value) if value else ""
