from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import re
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.artifact_dependency_provenance import (
    normalize_artifact_sha256_digest,
)
from control_plane.contracts.repository_human_admission import (
    RepositoryHumanRolePolicyProvenance,
)
from control_plane.contracts.runtime_identity import RuntimeIdentity


MANAGER_PREVIEW_APPROVAL_READ_ACTION = "manager_preview_approval.read"
MANAGER_PREVIEW_APPROVAL_WRITE_ACTION = "manager_preview_approval.write"

ManagerPreviewApprovalAction = Literal[
    "approved",
    "changes_requested",
    "revoked",
    "superseded",
    "invalidated",
]
ManagerPreviewApprovalDecisionStatus = Literal[
    "pending",
    "approved",
    "changes_requested",
    "revoked",
    "stale",
    "unavailable",
]
ManagerPreviewApprovalReasonCode = Literal[
    "approval_missing",
    "approval_valid",
    "changes_requested",
    "approval_revoked",
    "approval_stale",
    "preview_inactive",
    "serving_generation_missing",
    "serving_generation_mismatch",
    "generation_not_ready",
    "generation_verification_failed",
    "preview_identity_mismatch",
    "artifact_identity_missing",
    "runtime_identity_missing",
    "runtime_identity_mismatch",
    "policy_unavailable",
]
ManagerPreviewApprovalEventWriteStatus = Literal["written", "replayed"]

_GIT_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_MANAGER_ACTIONS = frozenset({"approved", "changes_requested", "revoked"})
_SYSTEM_ACTIONS = frozenset({"superseded", "invalidated"})


class ManagerPreviewApprovalBinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    product: str
    context: str
    repository: str
    pr_number: int = Field(ge=1)
    pr_url: str
    head_sha: str
    preview_id: str
    serving_generation_id: str
    artifact_id: str
    artifact_image_digest: str
    manifest_fingerprint: str
    preview_url: str
    runtime_identity: RuntimeIdentity
    runtime_identity_sha256: str = ""
    binding_sha256: str = ""

    @model_validator(mode="after")
    def _validate_binding(self) -> "ManagerPreviewApprovalBinding":
        if self.schema_version != 1:
            raise ValueError("Unsupported manager preview approval binding schema version.")
        self.product = _required_token(self.product, "product").lower()
        self.context = _required_token(self.context, "context").lower()
        self.repository = _required_token(self.repository, "repository").lower()
        self.pr_url = _required_http_url(self.pr_url, "pr_url")
        self.head_sha = _required_token(self.head_sha, "head_sha").lower()
        self.preview_id = _required_token(self.preview_id, "preview_id")
        self.serving_generation_id = _required_token(
            self.serving_generation_id, "serving_generation_id"
        )
        self.artifact_id = _required_token(self.artifact_id, "artifact_id")
        self.artifact_image_digest = normalize_artifact_sha256_digest(
            self.artifact_image_digest,
            label="manager preview approval artifact_image_digest",
        )
        self.manifest_fingerprint = _required_token(
            self.manifest_fingerprint, "manifest_fingerprint"
        )
        self.preview_url = _required_http_url(self.preview_url, "preview_url")
        if _GIT_SHA_PATTERN.fullmatch(self.head_sha) is None:
            raise ValueError(
                "manager preview approval binding head_sha must be an exact lowercase Git SHA"
            )

        runtime_identity = self.runtime_identity
        expected_runtime_values = {
            "product": self.product,
            "context": self.context,
            "artifact_id": self.artifact_id,
            "source_git_ref": self.head_sha,
        }
        mismatches = [
            field_name
            for field_name, expected_value in expected_runtime_values.items()
            if str(getattr(runtime_identity, field_name)).strip().lower() != expected_value.lower()
        ]
        if runtime_identity.environment_kind.strip().lower() != "preview":
            mismatches.append("environment_kind")
        if runtime_identity.preview_id.strip() != self.preview_id:
            mismatches.append("preview_id")
        if (
            runtime_identity.preview_generation_id.strip()
            and runtime_identity.preview_generation_id != self.serving_generation_id
        ):
            mismatches.append("preview_generation_id")
        if mismatches:
            raise ValueError(
                "manager preview approval runtime identity mismatched fields: "
                + ", ".join(mismatches)
            )
        image_digest = immutable_image_digest(runtime_identity.image_reference)
        if image_digest != self.artifact_image_digest:
            raise ValueError(
                "manager preview approval runtime image does not match artifact_image_digest"
            )

        computed_runtime_identity_sha256 = runtime_identity_sha256(runtime_identity)
        if self.runtime_identity_sha256:
            self.runtime_identity_sha256 = self.runtime_identity_sha256.strip().lower()
            if self.runtime_identity_sha256 != computed_runtime_identity_sha256:
                raise ValueError(
                    "manager preview approval runtime_identity_sha256 does not match payload"
                )
        else:
            self.runtime_identity_sha256 = computed_runtime_identity_sha256

        computed_binding_sha256 = manager_preview_approval_binding_sha256(self)
        if self.binding_sha256:
            self.binding_sha256 = self.binding_sha256.strip().lower()
            if self.binding_sha256 != computed_binding_sha256:
                raise ValueError("manager preview approval binding_sha256 does not match payload")
        else:
            self.binding_sha256 = computed_binding_sha256
        return self


class ManagerPreviewApprovalAuthorization(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    role: Literal["manager"] = "manager"
    action: Literal["manager_preview_approval.write"] = "manager_preview_approval.write"
    manager_github_id: int = Field(ge=1)
    manager_login: str
    managed_set_id: str
    managed_rule_id: str
    policy_record_id: str
    policy_revision: int = Field(ge=1)
    policy_schema_version: Literal[2] = 2
    policy_sha256: str
    policy_source: str
    role_policy_provenance: RepositoryHumanRolePolicyProvenance | None = None
    authorized_at: str

    @model_validator(mode="after")
    def _validate_authorization(self) -> "ManagerPreviewApprovalAuthorization":
        if self.schema_version != 1:
            raise ValueError("Unsupported manager preview approval authorization schema version.")
        for field_name in (
            "manager_login",
            "managed_set_id",
            "managed_rule_id",
            "policy_record_id",
            "policy_source",
        ):
            setattr(self, field_name, _required_token(str(getattr(self, field_name)), field_name))
        self.policy_sha256 = self.policy_sha256.strip().lower()
        if _SHA256_PATTERN.fullmatch(self.policy_sha256) is None:
            raise ValueError("manager preview approval authorization requires a policy SHA-256")
        self.authorized_at = _normalize_utc_timestamp(self.authorized_at, "authorized_at")
        return self


class ManagerPreviewApprovalEventRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    event_id: str = ""
    approval_id: str = ""
    binding: ManagerPreviewApprovalBinding
    action: ManagerPreviewApprovalAction
    occurred_at: str
    source_event_kind: str
    source_event_id: str
    reason: str = ""
    authorization: ManagerPreviewApprovalAuthorization | None = None

    @model_validator(mode="after")
    def _validate_event(self) -> "ManagerPreviewApprovalEventRecord":
        if self.schema_version != 1:
            raise ValueError("Unsupported manager preview approval event schema version.")
        self.occurred_at = _normalize_utc_timestamp(self.occurred_at, "occurred_at")
        self.source_event_kind = _required_token(self.source_event_kind, "source_event_kind")
        self.source_event_id = _required_token(self.source_event_id, "source_event_id")
        self.reason = self.reason.strip()
        if self.action != "approved" and not self.reason:
            raise ValueError(f"manager preview approval action {self.action!r} requires a reason")
        if self.action in _MANAGER_ACTIONS:
            if self.authorization is None:
                raise ValueError(
                    f"manager preview approval action {self.action!r} requires authorization"
                )
            if self.authorization.authorized_at != self.occurred_at:
                raise ValueError(
                    "manager preview approval authorization and event timestamps must match"
                )
        elif self.action in _SYSTEM_ACTIONS and self.authorization is not None:
            raise ValueError(
                f"manager preview approval action {self.action!r} must be system-authored"
            )

        computed_approval_id = build_manager_preview_approval_id(
            binding_sha256=self.binding.binding_sha256
        )
        if self.approval_id:
            self.approval_id = self.approval_id.strip()
            if self.approval_id != computed_approval_id:
                raise ValueError("manager preview approval approval_id does not match binding")
        else:
            self.approval_id = computed_approval_id

        computed_event_id = build_manager_preview_approval_event_id(
            binding_sha256=self.binding.binding_sha256,
            action=self.action,
            source_event_kind=self.source_event_kind,
            source_event_id=self.source_event_id,
        )
        if self.event_id:
            self.event_id = self.event_id.strip()
            if self.event_id != computed_event_id:
                raise ValueError("manager preview approval event_id does not match event payload")
        else:
            self.event_id = computed_event_id
        return self

    @property
    def manager_github_id(self) -> int:
        return self.authorization.manager_github_id if self.authorization is not None else 0

    @property
    def manager_login(self) -> str:
        return self.authorization.manager_login if self.authorization is not None else ""

    @property
    def policy_record_id(self) -> str:
        return self.authorization.policy_record_id if self.authorization is not None else ""

    @property
    def policy_sha256(self) -> str:
        return self.authorization.policy_sha256 if self.authorization is not None else ""


class ManagerPreviewApprovalDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    status: ManagerPreviewApprovalDecisionStatus
    reason_code: ManagerPreviewApprovalReasonCode
    reason: str
    current_binding_sha256: str = ""
    event_id: str = ""
    approval_id: str = ""
    manager_github_id: int = Field(default=0, ge=0)
    manager_login: str = ""
    evaluated_at: str

    @model_validator(mode="after")
    def _validate_decision(self) -> "ManagerPreviewApprovalDecision":
        if self.schema_version != 1:
            raise ValueError("Unsupported manager preview approval decision schema version.")
        self.reason = _required_token(self.reason, "reason")
        self.evaluated_at = _normalize_utc_timestamp(self.evaluated_at, "evaluated_at")
        for field_name in ("current_binding_sha256", "event_id", "approval_id", "manager_login"):
            setattr(self, field_name, str(getattr(self, field_name)).strip())
        if (
            self.current_binding_sha256
            and _SHA256_PATTERN.fullmatch(self.current_binding_sha256) is None
        ):
            raise ValueError("manager preview approval decision has invalid binding SHA-256")
        return self


def runtime_identity_sha256(identity: RuntimeIdentity) -> str:
    return _canonical_sha256(identity.model_dump(mode="json", exclude_none=True))


def manager_preview_approval_binding_sha256(binding: ManagerPreviewApprovalBinding) -> str:
    payload = binding.model_dump(
        mode="json",
        exclude={"binding_sha256", "runtime_identity_sha256"},
        exclude_none=True,
    )
    payload["runtime_identity_sha256"] = runtime_identity_sha256(binding.runtime_identity)
    return _canonical_sha256(payload)


def build_manager_preview_approval_id(*, binding_sha256: str) -> str:
    normalized = _required_sha256(binding_sha256, "binding_sha256")
    return f"manager-preview-approval-{normalized[:32]}"


def build_manager_preview_approval_event_id(
    *,
    binding_sha256: str,
    action: ManagerPreviewApprovalAction,
    source_event_kind: str,
    source_event_id: str,
) -> str:
    normalized_binding = _required_sha256(binding_sha256, "binding_sha256")
    payload = {
        "binding_sha256": normalized_binding,
        "action": action,
        "source_event_kind": _required_token(source_event_kind, "source_event_kind"),
        "source_event_id": _required_token(source_event_id, "source_event_id"),
    }
    return f"manager-preview-approval-event-{_canonical_sha256(payload)[:32]}"


def immutable_image_digest(image_reference: str) -> str:
    normalized = image_reference.strip()
    _, separator, digest = normalized.rpartition("@")
    if not separator:
        raise ValueError(
            "manager preview approval runtime identity requires an immutable image reference"
        )
    return normalize_artifact_sha256_digest(
        digest,
        label="manager preview approval runtime image digest",
    )


def _required_sha256(value: str, label: str) -> str:
    normalized = value.strip().lower()
    if _SHA256_PATTERN.fullmatch(normalized) is None:
        raise ValueError(f"manager preview approval {label} requires a lowercase SHA-256")
    return normalized


def _required_token(value: str, label: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"manager preview approval requires {label}")
    return normalized


def _required_http_url(value: str, label: str) -> str:
    normalized = _required_token(value, label)
    parsed = urlsplit(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"manager preview approval {label} must be an HTTP(S) URL")
    return normalized


def _normalize_utc_timestamp(value: str, label: str) -> str:
    normalized = _required_token(value, label)
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(
            f"manager preview approval {label} must be an ISO-8601 timestamp"
        ) from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"manager preview approval {label} requires a timezone")
    return parsed.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _canonical_sha256(payload: object) -> str:
    canonical_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()
