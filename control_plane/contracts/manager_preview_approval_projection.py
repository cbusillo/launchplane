from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.manager_preview_approval import (
    ManagerPreviewApprovalDecision,
)


MANAGER_PREVIEW_APPROVAL_CHECK_NAME = "manager-preview-approval"
MANAGER_PREVIEW_APPROVAL_COMMENT_MARKER = "<!-- launchplane-manager-preview-approval -->"

ManagerPreviewApprovalCommandAction = Literal[
    "approved",
    "changes_requested",
    "revoked",
]
ManagerPreviewApprovalCheckState = Literal["pending", "success", "failure", "error"]


class ManagerPreviewApprovalCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: ManagerPreviewApprovalCommandAction
    fingerprint: str
    reason: str = ""

    @model_validator(mode="after")
    def _validate_command(self) -> "ManagerPreviewApprovalCommand":
        self.fingerprint = self.fingerprint.strip().lower()
        self.reason = self.reason.strip()
        if len(self.fingerprint) != 64 or any(
            character not in "0123456789abcdef" for character in self.fingerprint
        ):
            raise ValueError("Manager preview approval command requires a SHA-256 fingerprint.")
        if self.action in {"changes_requested", "revoked"} and not self.reason:
            raise ValueError(f"Manager preview approval action {self.action!r} requires a reason.")
        if self.action == "approved" and self.reason:
            raise ValueError("Manager preview approval approve command cannot include a reason.")
        return self


class ManagerPreviewApprovalProjection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    required: bool
    product: str
    context: str
    repository: str
    pr_number: int = Field(ge=1)
    pr_url: str
    pr_state: str
    head_sha: str
    preview_id: str = ""
    preview_url: str = ""
    serving_generation_id: str = ""
    artifact_id: str = ""
    artifact_image_digest: str = ""
    manifest_fingerprint: str = ""
    binding_sha256: str = ""
    decision: ManagerPreviewApprovalDecision
    check_state: ManagerPreviewApprovalCheckState
    check_description: str
    comment_markdown: str

    @model_validator(mode="after")
    def _validate_projection(self) -> "ManagerPreviewApprovalProjection":
        if self.schema_version != 1:
            raise ValueError("Unsupported manager preview approval projection schema version.")
        for field_name in (
            "product",
            "context",
            "repository",
            "pr_url",
            "pr_state",
            "head_sha",
            "check_description",
            "comment_markdown",
        ):
            normalized = str(getattr(self, field_name)).strip()
            if not normalized:
                raise ValueError(f"Manager preview approval projection requires {field_name}.")
            setattr(self, field_name, normalized)
        for field_name in (
            "preview_id",
            "preview_url",
            "serving_generation_id",
            "artifact_id",
            "artifact_image_digest",
            "manifest_fingerprint",
            "binding_sha256",
        ):
            setattr(self, field_name, str(getattr(self, field_name)).strip())
        if len(self.check_description) > 140:
            raise ValueError(
                "Manager preview approval check description cannot exceed 140 characters."
            )
        if self.required and not self.binding_sha256 and self.decision.status != "unavailable":
            raise ValueError("Required manager preview approval projection requires a fingerprint.")
        return self


class ManagerPreviewApprovalReconcileEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    repository: str
    pr_number: int = Field(ge=1)

    @model_validator(mode="after")
    def _validate_request(self) -> "ManagerPreviewApprovalReconcileEnvelope":
        self.repository = self.repository.strip()
        if not self.repository or self.repository.count("/") != 1:
            raise ValueError("Manager preview approval reconciliation requires owner/repository.")
        return self
