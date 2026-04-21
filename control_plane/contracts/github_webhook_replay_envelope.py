from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class GitHubWebhookReplayCapture(BaseModel):
    model_config = ConfigDict(extra="forbid")

    recorded_at: str = ""
    source: str = ""
    headers: dict[str, str] = Field(default_factory=dict)
    evidence: dict[str, object] | None = None

    @model_validator(mode="after")
    def _validate_capture(self) -> "GitHubWebhookReplayCapture":
        if self.source and not self.source.strip():
            raise ValueError("GitHub webhook replay capture source must not be blank")
        return self

    def header_value(self, name: str) -> str:
        normalized_name = name.strip().lower()
        if not normalized_name:
            return ""
        for header_name, header_value in self.headers.items():
            if header_name.strip().lower() == normalized_name:
                return header_value.strip()
        return ""


class GitHubWebhookReplayEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    event_name: str = ""
    signature_256: str = ""
    allow_unsigned: bool = False
    delivery_id: str = ""
    delivery_source: str = ""
    payload_text: str = ""
    payload: dict[str, object] | None = None
    capture: GitHubWebhookReplayCapture | None = None
    adapter: Literal["github_webhook"] = "github_webhook"

    def resolved_event_name(self) -> str:
        return self.event_name.strip() or self._capture_header_value("X-GitHub-Event")

    def resolved_signature_256(self) -> str:
        return self.signature_256.strip() or self._capture_header_value("X-Hub-Signature-256")

    def resolved_delivery_id(self) -> str:
        return self.delivery_id.strip() or self._capture_header_value("X-GitHub-Delivery")

    def resolved_delivery_source(self) -> str:
        if self.delivery_source.strip():
            return self.delivery_source.strip()
        if self.capture and self.capture.source.strip():
            return self.capture.source.strip()
        return "replay-envelope"

    def replay_capture_payload(self) -> dict[str, object] | None:
        if self.capture is None:
            return None
        payload: dict[str, object] = {}
        if self.capture.recorded_at.strip():
            payload["recorded_at"] = self.capture.recorded_at.strip()
        if self.capture.source.strip():
            payload["source"] = self.capture.source.strip()
        if self.capture.headers:
            payload["headers"] = dict(self.capture.headers)
        if self.capture.evidence is not None:
            payload["evidence"] = self.capture.evidence
        return payload or None

    def _capture_header_value(self, name: str) -> str:
        if self.capture is None:
            return ""
        return self.capture.header_value(name)

    @model_validator(mode="after")
    def _validate_envelope(self) -> "GitHubWebhookReplayEnvelope":
        resolved_event_name = self.resolved_event_name()
        if not resolved_event_name:
            raise ValueError("GitHub webhook replay envelope requires event_name")
        if self.delivery_source and not self.delivery_source.strip():
            raise ValueError("GitHub webhook replay envelope delivery_source must not be blank")
        if self.capture is not None:
            capture_event_name = self._capture_header_value("X-GitHub-Event")
            if self.event_name.strip() and capture_event_name and self.event_name.strip() != capture_event_name:
                raise ValueError(
                    "GitHub webhook replay envelope event_name conflicts with capture header X-GitHub-Event"
                )
            capture_signature = self._capture_header_value("X-Hub-Signature-256")
            if self.signature_256.strip() and capture_signature and self.signature_256.strip() != capture_signature:
                raise ValueError(
                    "GitHub webhook replay envelope signature_256 conflicts with capture header X-Hub-Signature-256"
                )
            capture_delivery_id = self._capture_header_value("X-GitHub-Delivery")
            if self.delivery_id.strip() and capture_delivery_id and self.delivery_id.strip() != capture_delivery_id:
                raise ValueError(
                    "GitHub webhook replay envelope delivery_id conflicts with capture header X-GitHub-Delivery"
                )
        if not self.payload_text.strip() and self.payload is None:
            raise ValueError("GitHub webhook replay envelope requires payload_text or payload")
        if not self.allow_unsigned and not self.payload_text.strip():
            raise ValueError(
                "Signed GitHub webhook replay requires payload_text so Launchplane can verify the original bytes."
            )
        return self
