from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SECURITY_UPDATE_MAX_AGE_SECONDS = 2 * 24 * 60 * 60
SECURITY_UPDATE_COMPLETION_SECONDS = 30
HOST_SECURITY_UPDATE_CHECK = "host-security-updates"


class HostSecurityUpdateEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    status: Literal["fresh", "stale", "missing", "unknown"] = "unknown"
    observed_at: float = Field(ge=0)
    completed_at: float | None = Field(default=None, ge=0)
    server_id: str = ""
    provider_target_sha256: str = ""

    @model_validator(mode="after")
    def validate_completion(self) -> "HostSecurityUpdateEvidence":
        if self.status in {"fresh", "stale"} and (
            self.completed_at is None or self.completed_at > self.observed_at
        ):
            raise ValueError("Verified update evidence requires a past completion.")
        if self.status in {"unknown", "missing"} and self.completed_at is not None:
            raise ValueError("Unverified update evidence cannot include a completion.")
        return self
