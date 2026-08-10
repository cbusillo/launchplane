import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

PreviewPrFeedbackRemediationMode = Literal["dry-run", "apply"]
PreviewPrFeedbackRemediationAction = Literal["delete_comment", "replace_comment", "none"]
PreviewPrFeedbackRemediationOutcome = Literal[
    "planned", "deleted", "replaced", "already_absent", "failed"
]
PreviewPrFeedbackTerminalStatus = Literal[
    "destroyed", "failed", "cleanup_failed", "unsupported", "cleared"
]


class PreviewPrFeedbackRemediationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    mode: PreviewPrFeedbackRemediationMode = "dry-run"
    product: str
    context: str
    repository: str
    pull_request_url: str
    terminal_status: PreviewPrFeedbackTerminalStatus
    reason: str
    related_issue: str
    confirmation: str = ""

    @model_validator(mode="after")
    def _validate_request(self) -> "PreviewPrFeedbackRemediationRequest":
        for field_name in (
            "product",
            "context",
            "repository",
            "pull_request_url",
            "reason",
            "related_issue",
        ):
            value = getattr(self, field_name).strip()
            if not value:
                raise ValueError(f"preview PR feedback remediation requires {field_name}")
            setattr(self, field_name, value)
        self.confirmation = self.confirmation.strip()
        if self.mode == "apply" and self.confirmation != self.expected_confirmation:
            raise ValueError("preview PR feedback remediation apply requires exact confirmation")
        return self

    @property
    def expected_confirmation(self) -> str:
        return f"remediate preview feedback {self.pull_request_url} to {self.terminal_status}"

    def continuity_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "product": self.product,
            "context": self.context,
            "repository": self.repository,
            "pull_request_url": self.pull_request_url,
            "terminal_status": self.terminal_status,
            "reason": self.reason,
            "related_issue": self.related_issue,
        }

    @property
    def continuity_sha256(self) -> str:
        canonical = json.dumps(self.continuity_payload(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class PreviewPrFeedbackObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state: Literal["present", "absent"]
    digest_sha256: str
    body_sha256: str = ""
    excerpt: str = Field(default="", max_length=500)
    marker: str = ""
    comment_id: int = Field(default=0, ge=0)
    comment_url: str = ""
    comment_author_id: int = Field(default=0, ge=0)
    comment_author_login: str = ""
    token_actor_id: int = Field(ge=1)
    token_actor_login: str


class PreviewPrFeedbackMutationEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    attempted: bool = False
    mutated: bool = False
    method: Literal["", "DELETE", "PATCH"] = ""
    comment_id: int = Field(default=0, ge=0)
    before_digest_sha256: str = ""
    after_digest_sha256: str = ""
    verified_absent: bool = False
    error_message: str = ""


class PreviewPrFeedbackRemediationRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    remediation_id: str
    product: str
    context: str
    repository: str
    pull_request_url: str
    pull_request_number: int = Field(ge=1)
    mode: PreviewPrFeedbackRemediationMode
    terminal_status: PreviewPrFeedbackTerminalStatus
    actor: str
    reason: str
    related_issue: str
    trace_id: str
    idempotency_key: str
    requested_at: str
    continuity_sha256: str
    observation: PreviewPrFeedbackObservation
    planned_action: PreviewPrFeedbackRemediationAction
    outcome: PreviewPrFeedbackRemediationOutcome
    mutation_evidence: PreviewPrFeedbackMutationEvidence = Field(
        default_factory=PreviewPrFeedbackMutationEvidence
    )
    companion_feedback_id: str = ""

    @model_validator(mode="after")
    def _validate_record(self) -> "PreviewPrFeedbackRemediationRecord":
        required = (
            self.remediation_id,
            self.product,
            self.context,
            self.repository,
            self.pull_request_url,
            self.actor,
            self.reason,
            self.related_issue,
            self.trace_id,
            self.idempotency_key,
            self.requested_at,
        )
        if not all(value.strip() for value in required):
            raise ValueError("preview PR feedback remediation audit fields must be complete")
        for digest in (self.continuity_sha256, self.observation.digest_sha256):
            if len(digest) != 64 or any(
                character not in "0123456789abcdef" for character in digest
            ):
                raise ValueError("preview PR feedback remediation digests must be SHA-256")
        if self.mode == "dry-run" and self.outcome != "planned":
            raise ValueError("preview PR feedback remediation dry-run outcome must be planned")
        if self.mode == "apply" and self.outcome == "planned":
            raise ValueError("preview PR feedback remediation apply requires a terminal outcome")
        return self


def build_preview_pr_feedback_remediation_id(*, trace_id: str) -> str:
    return f"preview-pr-feedback-remediation-{trace_id.strip()}"
