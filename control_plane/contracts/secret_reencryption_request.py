from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class SecretReencryptionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1, le=1)
    mode: Literal["dry-run", "apply"] = "dry-run"
    expected_plan_digest: str = ""
    reason: str = Field(min_length=1, max_length=240)
    source_label: str = Field(default="service-secret-rotation", min_length=1, max_length=96)

    @model_validator(mode="after")
    def _validate_request(self) -> "SecretReencryptionRequest":
        self.reason = self.reason.strip()
        self.source_label = self.source_label.strip()
        if not self.reason:
            raise ValueError("secret re-encryption request requires reason")
        if not self.source_label:
            raise ValueError("secret re-encryption request requires source_label")
        if self.mode == "apply":
            digest = self.expected_plan_digest.strip().lower()
            if len(digest) != 64 or any(
                character not in "0123456789abcdef" for character in digest
            ):
                raise ValueError("secret re-encryption apply requires a SHA-256 plan digest")
            self.expected_plan_digest = digest
        elif self.expected_plan_digest.strip():
            raise ValueError("secret re-encryption dry-run must not set expected_plan_digest")
        return self
