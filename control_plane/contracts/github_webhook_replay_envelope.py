from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator


class GitHubWebhookReplayEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    event_name: str
    signature_256: str = ""
    allow_unsigned: bool = False
    payload_text: str = ""
    payload: dict[str, object] | None = None
    adapter: Literal["github_webhook"] = "github_webhook"

    @model_validator(mode="after")
    def _validate_envelope(self) -> "GitHubWebhookReplayEnvelope":
        if not self.event_name.strip():
            raise ValueError("GitHub webhook replay envelope requires event_name")
        if not self.payload_text.strip() and self.payload is None:
            raise ValueError("GitHub webhook replay envelope requires payload_text or payload")
        if not self.allow_unsigned and not self.payload_text.strip():
            raise ValueError(
                "Signed GitHub webhook replay requires payload_text so Harbor can verify the original bytes."
            )
        return self
