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
from control_plane.contracts.deploy_reference import docker_image_digest
from control_plane.contracts.product_owner import (
    PRODUCT_OWNER_POLICY_FINGERPRINT_VERSION,
    ProductOwnerPreviewIsolationClass,
    ProductOwnerReviewChangeClass,
)
from control_plane.contracts.runtime_identity import RuntimeIdentity

OWNER_ACCEPTANCE_READ_ACTION = "owner_acceptance.read"
OWNER_ACCEPTANCE_EVENT_WRITE_ACTION = "owner_acceptance_event.write"

OwnerAcceptanceAction = Literal[
    "accepted",
    "changes_requested",
    "revoked",
    "superseded",
    "invalidated",
]
OwnerAcceptanceDecisionStatus = Literal[
    "not_required",
    "pending",
    "accepted",
    "changes_requested",
    "revoked",
    "stale",
    "unavailable",
]
OwnerAcceptanceReasonCode = Literal[
    "engineering_only",
    "acceptance_missing",
    "acceptance_valid",
    "changes_requested",
    "acceptance_revoked",
    "acceptance_stale",
    "change_impact_unavailable",
    "change_impact_stale",
    "multi_product_unsupported",
    "owner_authority_unavailable",
    "owner_authority_denied",
    "preview_evidence_unavailable",
    "preview_evidence_stale",
    "owner_review_expired",
    "preview_isolation_insufficient",
    "contributing_identity_unknown",
    "self_review_denied",
    "review_context_missing",
]
OwnerAcceptanceEventWriteStatus = Literal["written", "replayed"]
OwnerAcceptanceSourceEventKind = Literal["browser_api", "system"]
OwnerAcceptanceViewerEligibilityReason = Literal[
    "current_product_owner",
    "not_current_product_owner",
    "viewer_identity_unsupported",
    "owner_authority_unavailable",
    "self_review_denied",
]
OwnerAcceptanceContributionResolution = Literal["resolved", "unknown"]
OwnerAcceptanceContributionReason = Literal[
    "server_resolved",
    "identity_evidence_unavailable",
    "identity_evidence_conflicting",
    "identity_evidence_incomplete",
]
OwnerAcceptanceHumanActionSemantics = Literal[
    "none",
    "product_review_accepted",
    "product_review_changes_requested",
    "product_review_revoked",
    "product_review_superseded",
    "product_review_invalidated",
]

_HUMAN_ACTION_SEMANTICS: dict[str, OwnerAcceptanceHumanActionSemantics] = {
    "accepted": "product_review_accepted",
    "changes_requested": "product_review_changes_requested",
    "revoked": "product_review_revoked",
    "superseded": "product_review_superseded",
    "invalidated": "product_review_invalidated",
}
OWNER_ACCEPTANCE_PROJECT_ACTION = "owner_acceptance.project"

OWNER_ACCEPTANCE_STATUS_PRECEDENCE: tuple[OwnerAcceptanceDecisionStatus, ...] = (
    "unavailable",
    "stale",
    "revoked",
    "changes_requested",
    "pending",
    "accepted",
    "not_required",
)

_GIT_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_REPOSITORY_PATTERN = re.compile(r"^[^/\s]+/[^/\s]+$")
_SOURCE_EVENT_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_HUMAN_ACTIONS = frozenset({"accepted", "changes_requested", "revoked"})
_SYSTEM_ACTIONS = frozenset({"superseded", "invalidated"})


def _required_token(value: str, field_name: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} must be non-empty")
    return normalized


def _normalize_decimal_id(value: str, field_name: str) -> str:
    normalized = _required_token(value, field_name)
    if not normalized.isdecimal() or int(normalized) < 1:
        raise ValueError(f"{field_name} must be a positive decimal identity")
    return str(int(normalized))


def _normalize_repository(value: str, field_name: str) -> str:
    normalized = _required_token(value, field_name).lower()
    if _REPOSITORY_PATTERN.fullmatch(normalized) is None:
        raise ValueError(f"{field_name} must be owner/name")
    return normalized


def _normalize_git_sha(value: str, field_name: str) -> str:
    normalized = _required_token(value, field_name).lower()
    if _GIT_SHA_PATTERN.fullmatch(normalized) is None:
        raise ValueError(f"{field_name} must be a Git SHA")
    return normalized


def _normalize_sha256(value: str, field_name: str) -> str:
    normalized = _required_token(value, field_name).lower()
    if _SHA256_PATTERN.fullmatch(normalized) is None:
        raise ValueError(f"{field_name} must be a lowercase SHA-256 digest")
    return normalized


def _normalize_timestamp(value: str, field_name: str) -> str:
    normalized = _required_token(value, field_name)
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{field_name} must be an ISO-8601 timestamp") from error
    if parsed.tzinfo is None:
        raise ValueError(f"{field_name} must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _normalize_http_url(value: str, field_name: str) -> str:
    normalized = _required_token(value, field_name)
    parsed = urlsplit(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"{field_name} must be an absolute HTTP(S) URL")
    return normalized


def _canonical_sha256(payload: object) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class OwnerAcceptanceRuntimeIdentityBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(default=1, ge=1)
    product: str
    context: str
    instance: str
    environment_kind: str
    deployment_record_id: str
    artifact_id: str
    source_git_ref: str
    image_reference: str
    release_tuple_id: str = ""
    preview_id: str
    preview_generation_id: str
    runtime_identity_sha256: str = ""

    @model_validator(mode="after")
    def _validate_runtime_identity(self) -> "OwnerAcceptanceRuntimeIdentityBinding":
        if self.schema_version != 1:
            raise ValueError("Unsupported Owner acceptance runtime identity schema version.")
        for field_name in (
            "product",
            "context",
            "instance",
            "environment_kind",
            "deployment_record_id",
            "artifact_id",
            "preview_id",
            "preview_generation_id",
        ):
            object.__setattr__(
                self,
                field_name,
                _required_token(str(getattr(self, field_name)), field_name),
            )
        object.__setattr__(
            self,
            "source_git_ref",
            _normalize_git_sha(self.source_git_ref, "source_git_ref"),
        )
        object.__setattr__(
            self,
            "image_reference",
            _required_token(self.image_reference, "image_reference"),
        )
        object.__setattr__(self, "release_tuple_id", self.release_tuple_id.strip())
        computed_digest = owner_acceptance_runtime_identity_sha256(self)
        if self.runtime_identity_sha256:
            normalized_digest = _normalize_sha256(
                self.runtime_identity_sha256,
                "runtime_identity_sha256",
            )
            if normalized_digest != computed_digest:
                raise ValueError("Owner acceptance runtime identity digest does not match payload")
            object.__setattr__(self, "runtime_identity_sha256", normalized_digest)
        else:
            object.__setattr__(self, "runtime_identity_sha256", computed_digest)
        return self


class OwnerAcceptancePreviewBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(default=1, ge=1)
    context: str
    preview_id: str
    serving_generation_id: str
    artifact_id: str
    artifact_image_digest: str
    manifest_fingerprint: str
    preview_url: str
    runtime_identity: OwnerAcceptanceRuntimeIdentityBinding

    @model_validator(mode="after")
    def _validate_preview(self) -> "OwnerAcceptancePreviewBinding":
        if self.schema_version != 1:
            raise ValueError("Unsupported Owner acceptance preview binding schema version.")
        for field_name in (
            "context",
            "preview_id",
            "serving_generation_id",
            "artifact_id",
            "manifest_fingerprint",
        ):
            object.__setattr__(
                self,
                field_name,
                _required_token(str(getattr(self, field_name)), field_name),
            )
        object.__setattr__(
            self,
            "artifact_image_digest",
            _required_token(self.artifact_image_digest, "artifact_image_digest").lower(),
        )
        object.__setattr__(
            self,
            "preview_url",
            _normalize_http_url(self.preview_url, "preview_url"),
        )
        runtime = self.runtime_identity
        mismatches = []
        if runtime.context.casefold() != self.context.casefold():
            mismatches.append("context")
        if runtime.artifact_id.casefold() != self.artifact_id.casefold():
            mismatches.append("artifact_id")
        if runtime.preview_id != self.preview_id:
            mismatches.append("preview_id")
        if runtime.preview_generation_id != self.serving_generation_id:
            mismatches.append("preview_generation_id")
        if runtime.environment_kind.casefold() != "preview":
            mismatches.append("environment_kind")
        image_digest = docker_image_digest(runtime.image_reference)
        if not image_digest or normalize_artifact_sha256_digest(
            image_digest,
            label="Owner acceptance preview runtime image digest",
        ) != normalize_artifact_sha256_digest(
            self.artifact_image_digest,
            label="Owner acceptance preview artifact image digest",
        ):
            mismatches.append("artifact_image_digest")
        if mismatches:
            raise ValueError(
                "Owner acceptance preview runtime identity mismatched fields: "
                + ", ".join(mismatches)
            )
        return self


class OwnerAcceptanceContributionBinding(BaseModel):
    """Server-resolved numeric GitHub contributing identities for a reviewed range.

    Launchplane resolves these from trusted pull-request and GitHub-linked commit
    evidence. Unresolved or conflicting evidence is recorded as ``unknown`` with no
    identities so self-review and admissibility fail closed.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(default=1, ge=1)
    resolution: OwnerAcceptanceContributionResolution
    reason_code: OwnerAcceptanceContributionReason
    contributor_github_ids: tuple[int, ...] = ()
    commit_count: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _validate_contribution(self) -> "OwnerAcceptanceContributionBinding":
        if self.schema_version != 1:
            raise ValueError("Unsupported Owner acceptance contribution schema version.")
        for github_id in self.contributor_github_ids:
            if github_id < 1:
                raise ValueError("contributor_github_ids must be positive numeric GitHub IDs")
        object.__setattr__(
            self,
            "contributor_github_ids",
            tuple(sorted(set(self.contributor_github_ids))),
        )
        if self.resolution == "resolved":
            if self.reason_code != "server_resolved":
                raise ValueError("resolved contributing identities require server_resolved reason")
            if not self.contributor_github_ids:
                raise ValueError("resolved contributing identities cannot be empty")
        else:
            if self.reason_code == "server_resolved":
                raise ValueError("unknown contributing identities require a failure reason")
            if self.contributor_github_ids:
                raise ValueError("unknown contributing identities cannot name identities")
        return self

    def includes(self, github_id: int) -> bool:
        return github_id in self.contributor_github_ids


class OwnerAcceptancePolicyFingerprintBinding(BaseModel):
    """Product/action-scoped, explicitly versioned Owner policy fingerprints."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(default=1, ge=1)
    fingerprint_version: int = Field(
        default=PRODUCT_OWNER_POLICY_FINGERPRINT_VERSION,
        ge=1,
    )
    owner_membership_fingerprint: str
    self_review_fingerprint: str
    review_age_fingerprint: str
    requirement_fingerprint: str
    preview_trust_fingerprint: str

    @model_validator(mode="after")
    def _validate_fingerprints(self) -> "OwnerAcceptancePolicyFingerprintBinding":
        if self.schema_version != 1:
            raise ValueError("Unsupported Owner acceptance policy fingerprint schema version.")
        if self.fingerprint_version != PRODUCT_OWNER_POLICY_FINGERPRINT_VERSION:
            raise ValueError("Unsupported Owner acceptance policy fingerprint version.")
        for field_name in (
            "owner_membership_fingerprint",
            "self_review_fingerprint",
            "review_age_fingerprint",
            "requirement_fingerprint",
            "preview_trust_fingerprint",
        ):
            object.__setattr__(
                self,
                field_name,
                _normalize_sha256(str(getattr(self, field_name)), field_name),
            )
        return self


class OwnerAcceptancePreviewIsolationBinding(BaseModel):
    """Recorded preview data/credential isolation class for the reviewed evidence.

    L1 product judgment is never a security control. This binding exists so weak or
    unknown preview isolation makes the recorded review inadmissible rather than
    silently trusted.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(default=1, ge=1)
    isolation_class: ProductOwnerPreviewIsolationClass
    data_transport_mode: str = ""
    source: Literal["product_preview_profile", "no_preview_binding"]

    @model_validator(mode="after")
    def _validate_isolation(self) -> "OwnerAcceptancePreviewIsolationBinding":
        if self.schema_version != 1:
            raise ValueError("Unsupported Owner acceptance preview isolation schema version.")
        object.__setattr__(self, "data_transport_mode", self.data_transport_mode.strip())
        if self.source == "no_preview_binding":
            if self.isolation_class != "not_applicable":
                raise ValueError(
                    "Owner acceptance isolation without a preview binding must be not_applicable"
                )
            if self.data_transport_mode:
                raise ValueError(
                    "Owner acceptance isolation without a preview binding has no transport mode"
                )
        else:
            if self.isolation_class == "not_applicable":
                raise ValueError(
                    "Owner acceptance preview isolation requires a real isolation class"
                )
            if not self.data_transport_mode:
                raise ValueError(
                    "Owner acceptance preview isolation requires the bound data transport mode"
                )
        return self


class OwnerAcceptanceReviewContext(BaseModel):
    """Exact reviewed context bound to one Owner product-review event.

    The context is entirely server-resolved. It never grants merge or release
    authority; it exists so historical evidence can be judged currently admissible
    using bound base identity, impact class, authorship, finite age, scoped policy
    fingerprints, and preview isolation.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(default=1, ge=1)
    base_ref: str
    base_sha: str
    change_class: ProductOwnerReviewChangeClass
    engineering_review_tier: Literal["routine", "sensitive"]
    review_max_age_seconds: int = Field(ge=1)
    contributions: OwnerAcceptanceContributionBinding
    policy_fingerprints: OwnerAcceptancePolicyFingerprintBinding
    preview_isolation: OwnerAcceptancePreviewIsolationBinding

    @model_validator(mode="after")
    def _validate_review_context(self) -> "OwnerAcceptanceReviewContext":
        if self.schema_version != 1:
            raise ValueError("Unsupported Owner acceptance review context schema version.")
        object.__setattr__(self, "base_ref", _required_token(self.base_ref, "base_ref"))
        object.__setattr__(self, "base_sha", _normalize_git_sha(self.base_sha, "base_sha"))
        return self


class OwnerAcceptanceBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(default=1, ge=1)
    repository_id: str
    repository_owner_id: str
    repository: str
    pull_request_number: int = Field(ge=1)
    head_sha: str
    tree_sha: str
    change_impact_policy_record_id: str
    change_impact_policy_revision: int = Field(ge=1)
    change_impact_policy_digest: str
    product: str
    system: str
    action: str
    environment: str
    owner_policy_record_id: str
    owner_policy_revision: int = Field(ge=1)
    owner_policy_digest: str
    owner_requirement_record_id: str
    owner_requirement_revision: int = Field(ge=1)
    owner_requirement_digest: str
    preview: OwnerAcceptancePreviewBinding | None = None
    review_context: OwnerAcceptanceReviewContext | None = None
    binding_sha256: str = ""

    @model_validator(mode="after")
    def _validate_binding(self) -> "OwnerAcceptanceBinding":
        if self.schema_version != 1:
            raise ValueError("Unsupported Owner acceptance binding schema version.")
        for field_name in ("repository_id", "repository_owner_id"):
            object.__setattr__(
                self,
                field_name,
                _normalize_decimal_id(str(getattr(self, field_name)), field_name),
            )
        object.__setattr__(self, "repository", _normalize_repository(self.repository, "repository"))
        object.__setattr__(self, "head_sha", _normalize_git_sha(self.head_sha, "head_sha"))
        object.__setattr__(self, "tree_sha", _normalize_git_sha(self.tree_sha, "tree_sha"))
        for field_name in (
            "change_impact_policy_record_id",
            "product",
            "system",
            "action",
            "environment",
            "owner_policy_record_id",
            "owner_requirement_record_id",
        ):
            object.__setattr__(
                self,
                field_name,
                _required_token(str(getattr(self, field_name)), field_name),
            )
        for field_name in (
            "change_impact_policy_digest",
            "owner_policy_digest",
            "owner_requirement_digest",
        ):
            object.__setattr__(
                self,
                field_name,
                _normalize_sha256(str(getattr(self, field_name)), field_name),
            )
        if self.preview is not None:
            mismatches = []
            if self.preview.runtime_identity.product.casefold() != self.product.casefold():
                mismatches.append("product")
            if self.preview.runtime_identity.source_git_ref != self.head_sha:
                mismatches.append("head_sha")
            if mismatches:
                raise ValueError(
                    "Owner acceptance preview binding mismatched fields: " + ", ".join(mismatches)
                )
        if self.review_context is not None:
            expected_source = (
                "no_preview_binding" if self.preview is None else "product_preview_profile"
            )
            if self.review_context.preview_isolation.source != expected_source:
                raise ValueError(
                    "Owner acceptance preview isolation source does not match the preview binding"
                )
            if self.review_context.base_sha == self.head_sha:
                raise ValueError("Owner acceptance reviewed base cannot equal the reviewed head")
        computed_binding = owner_acceptance_binding_sha256(self)
        if self.binding_sha256:
            normalized_binding = _normalize_sha256(self.binding_sha256, "binding_sha256")
            if normalized_binding != computed_binding:
                raise ValueError("Owner acceptance binding_sha256 does not match payload")
            object.__setattr__(self, "binding_sha256", normalized_binding)
        else:
            object.__setattr__(self, "binding_sha256", computed_binding)
        return self


class OwnerAcceptanceAuthorization(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(default=1, ge=1)
    owner_identity_id: str
    owner_github_id: int = Field(ge=1)
    owner_login: str
    owner_policy_record_id: str
    owner_policy_revision: int = Field(ge=1)
    owner_policy_digest: str
    owner_requirement_record_id: str
    owner_requirement_revision: int = Field(ge=1)
    owner_requirement_digest: str
    authorized_at: str
    self_review: bool = False
    self_review_exception_revision: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _validate_authorization(self) -> "OwnerAcceptanceAuthorization":
        if self.schema_version != 1:
            raise ValueError("Unsupported Owner acceptance authorization schema version.")
        if not self.self_review and self.self_review_exception_revision:
            raise ValueError(
                "Owner acceptance self-review exception revision requires a self review"
            )
        for field_name in (
            "owner_identity_id",
            "owner_login",
            "owner_policy_record_id",
            "owner_requirement_record_id",
        ):
            object.__setattr__(
                self,
                field_name,
                _required_token(str(getattr(self, field_name)), field_name),
            )
        for field_name in ("owner_policy_digest", "owner_requirement_digest"):
            object.__setattr__(
                self,
                field_name,
                _normalize_sha256(str(getattr(self, field_name)), field_name),
            )
        object.__setattr__(
            self,
            "authorized_at",
            _normalize_timestamp(self.authorized_at, "authorized_at"),
        )
        return self


class OwnerAcceptanceEventRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(default=1, ge=1)
    event_id: str = ""
    acceptance_id: str = ""
    binding: OwnerAcceptanceBinding
    action: OwnerAcceptanceAction
    occurred_at: str
    source_event_kind: OwnerAcceptanceSourceEventKind
    source_event_id: str = Field(min_length=1, max_length=128)
    reason: str = Field(default="", max_length=4000)
    authorization: OwnerAcceptanceAuthorization | None = None

    @model_validator(mode="after")
    def _validate_event(self) -> "OwnerAcceptanceEventRecord":
        if self.schema_version != 1:
            raise ValueError("Unsupported Owner acceptance event schema version.")
        object.__setattr__(
            self,
            "occurred_at",
            _normalize_timestamp(self.occurred_at, "occurred_at"),
        )
        object.__setattr__(
            self,
            "source_event_kind",
            _required_token(self.source_event_kind, "source_event_kind"),
        )
        object.__setattr__(
            self,
            "source_event_id",
            _required_token(self.source_event_id, "source_event_id"),
        )
        if _SOURCE_EVENT_ID_PATTERN.fullmatch(self.source_event_id) is None:
            raise ValueError("Owner acceptance source_event_id is not canonical")
        object.__setattr__(self, "reason", self.reason.strip())
        if self.action != "accepted" and not self.reason:
            raise ValueError(f"Owner acceptance action {self.action!r} requires a reason")
        if self.action in _HUMAN_ACTIONS:
            if self.source_event_kind != "browser_api":
                raise ValueError("Human Owner acceptance events require browser_api source")
            if self.authorization is None:
                raise ValueError(f"Owner acceptance action {self.action!r} requires authorization")
            if self.authorization.authorized_at != self.occurred_at:
                raise ValueError("Owner acceptance authorization and event timestamps must match")
            if self.authorization.owner_policy_record_id != self.binding.owner_policy_record_id:
                raise ValueError("Owner acceptance authorization policy does not match binding")
            if self.authorization.owner_policy_revision != self.binding.owner_policy_revision:
                raise ValueError(
                    "Owner acceptance authorization policy revision does not match binding"
                )
            if self.authorization.owner_policy_digest != self.binding.owner_policy_digest:
                raise ValueError(
                    "Owner acceptance authorization policy digest does not match binding"
                )
            if (
                self.authorization.owner_requirement_record_id
                != self.binding.owner_requirement_record_id
            ):
                raise ValueError(
                    "Owner acceptance authorization requirement does not match binding"
                )
            if (
                self.authorization.owner_requirement_revision
                != self.binding.owner_requirement_revision
            ):
                raise ValueError(
                    "Owner acceptance authorization requirement revision does not match binding"
                )
            if self.authorization.owner_requirement_digest != self.binding.owner_requirement_digest:
                raise ValueError(
                    "Owner acceptance authorization requirement digest does not match binding"
                )
            if (
                self.action == "accepted"
                and self.authorization.self_review
                and self.authorization.self_review_exception_revision < 1
            ):
                raise ValueError(
                    "Owner acceptance self-review requires a revisioned routine policy exception"
                )
        elif self.action in _SYSTEM_ACTIONS:
            if self.source_event_kind != "system":
                raise ValueError("System Owner acceptance events require system source")
            if self.authorization is not None:
                raise ValueError(f"Owner acceptance action {self.action!r} must be system-authored")
        acceptance_id = build_owner_acceptance_id(binding_sha256=self.binding.binding_sha256)
        if self.acceptance_id:
            normalized_acceptance_id = _required_token(self.acceptance_id, "acceptance_id")
            if normalized_acceptance_id != acceptance_id:
                raise ValueError("Owner acceptance acceptance_id does not match binding")
            object.__setattr__(self, "acceptance_id", normalized_acceptance_id)
        else:
            object.__setattr__(self, "acceptance_id", acceptance_id)
        event_id = build_owner_acceptance_event_id(
            binding_sha256=self.binding.binding_sha256,
            action=self.action,
            source_event_kind=self.source_event_kind,
            source_event_id=self.source_event_id,
        )
        if self.event_id:
            normalized_event_id = _required_token(self.event_id, "event_id")
            if normalized_event_id != event_id:
                raise ValueError("Owner acceptance event_id does not match event payload")
            object.__setattr__(self, "event_id", normalized_event_id)
        else:
            object.__setattr__(self, "event_id", event_id)
        return self


def owner_acceptance_human_action_semantics(
    action: OwnerAcceptanceAction | None,
) -> OwnerAcceptanceHumanActionSemantics:
    """Project a stored human action into machine-readable non-authority semantics.

    The stored enum never changes. This projection exists so an API or UI client
    cannot read L1 ``accepted`` as merge readiness, landed state, or production
    authorization.
    """
    if action is None:
        return "none"
    return _HUMAN_ACTION_SEMANTICS[action]


def _validate_non_authority(
    *,
    admissible: bool,
    status: OwnerAcceptanceDecisionStatus,
    authorizes: tuple[str, ...],
    current_event: "OwnerAcceptanceEventRecord | None",
    human_action_semantics: OwnerAcceptanceHumanActionSemantics,
) -> None:
    if authorizes:
        raise ValueError("Owner product review never authorizes merge, release, or production")
    if admissible and status != "accepted":
        raise ValueError("Only a currently accepted Owner product review can be admissible")
    expected_semantics = owner_acceptance_human_action_semantics(
        current_event.action if current_event is not None else None
    )
    if human_action_semantics != expected_semantics:
        raise ValueError("Owner acceptance human_action_semantics must project the current event")


class OwnerAcceptanceProductDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(default=1, ge=1)
    product: str
    system: str
    action: str
    environment: str
    status: OwnerAcceptanceDecisionStatus
    reason_code: OwnerAcceptanceReasonCode
    binding: OwnerAcceptanceBinding | None = None
    current_event: OwnerAcceptanceEventRecord | None = None
    admissible: bool = False
    human_action_semantics: OwnerAcceptanceHumanActionSemantics = "none"
    authorizes: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _validate_product_decision(self) -> "OwnerAcceptanceProductDecision":
        if self.schema_version != 1:
            raise ValueError("Unsupported Owner acceptance product decision schema version.")
        _validate_non_authority(
            admissible=self.admissible,
            status=self.status,
            authorizes=self.authorizes,
            current_event=self.current_event,
            human_action_semantics=self.human_action_semantics,
        )
        for field_name in ("product", "system", "action", "environment"):
            object.__setattr__(
                self,
                field_name,
                _required_token(str(getattr(self, field_name)), field_name),
            )
        if self.binding is not None:
            for field_name in ("product", "system", "action", "environment"):
                if getattr(self.binding, field_name) != getattr(self, field_name):
                    raise ValueError(
                        f"Owner acceptance product decision {field_name} does not match binding"
                    )
        if self.current_event is not None:
            if self.binding is None:
                raise ValueError("Owner acceptance product decision event requires a binding")
            for field_name in ("product", "system", "action", "environment"):
                if getattr(self.current_event.binding, field_name) != getattr(self, field_name):
                    raise ValueError(
                        f"Owner acceptance product decision event {field_name} does not match subject"
                    )
        return self


class OwnerAcceptanceViewerBindingEligibility(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(default=1, ge=1)
    binding_sha256: str
    product: str
    system: str
    action: str
    environment: str
    can_submit_event: bool
    can_accept: bool
    can_request_changes: bool
    can_revoke: bool
    reason_code: OwnerAcceptanceViewerEligibilityReason

    @model_validator(mode="after")
    def _validate_viewer_binding_eligibility(
        self,
    ) -> "OwnerAcceptanceViewerBindingEligibility":
        if self.schema_version != 1:
            raise ValueError("Unsupported Owner acceptance viewer eligibility schema version.")
        object.__setattr__(
            self,
            "binding_sha256",
            _normalize_sha256(self.binding_sha256, "binding_sha256"),
        )
        for field_name in ("product", "system", "action", "environment"):
            object.__setattr__(
                self,
                field_name,
                _required_token(str(getattr(self, field_name)), field_name),
            )
        if self.can_submit_event != any(
            (self.can_accept, self.can_request_changes, self.can_revoke)
        ):
            raise ValueError(
                "Owner acceptance viewer eligibility must match its action capabilities."
            )
        if self.reason_code == "current_product_owner":
            if not all((self.can_accept, self.can_request_changes, self.can_revoke)):
                raise ValueError("Current product Owners must receive every Owner review action.")
        elif self.reason_code == "self_review_denied":
            if self.can_accept or not self.can_request_changes or not self.can_revoke:
                raise ValueError(
                    "Self-review denial must block acceptance while preserving withdrawal actions."
                )
        elif self.can_submit_event:
            raise ValueError("Ineligible viewers cannot submit Owner review events.")
        return self


class OwnerAcceptanceDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(default=1, ge=1)
    mode: Literal["shadow"] = "shadow"
    authoritative: Literal[False] = False
    enforcement_effect: Literal["none"] = "none"
    status: OwnerAcceptanceDecisionStatus
    reason_code: OwnerAcceptanceReasonCode
    binding: OwnerAcceptanceBinding | None = None
    current_event: OwnerAcceptanceEventRecord | None = None
    admissible: bool = False
    human_action_semantics: OwnerAcceptanceHumanActionSemantics = "none"
    authorizes: tuple[str, ...] = ()
    products: tuple[OwnerAcceptanceProductDecision, ...] = ()
    evaluated_at: str

    @model_validator(mode="after")
    def _validate_decision(self) -> "OwnerAcceptanceDecision":
        if self.schema_version != 1:
            raise ValueError("Unsupported Owner acceptance decision schema version.")
        _validate_non_authority(
            admissible=self.admissible,
            status=self.status,
            authorizes=self.authorizes,
            current_event=self.current_event,
            human_action_semantics=self.human_action_semantics,
        )
        object.__setattr__(
            self,
            "evaluated_at",
            _normalize_timestamp(self.evaluated_at, "evaluated_at"),
        )
        return self


def owner_acceptance_binding_sha256(binding: OwnerAcceptanceBinding) -> str:
    return _canonical_sha256(
        binding.model_dump(
            mode="json",
            exclude={"binding_sha256"},
            exclude_none=True,
        )
    )


def owner_acceptance_runtime_identity_sha256(
    identity: OwnerAcceptanceRuntimeIdentityBinding | RuntimeIdentity,
) -> str:
    payload = {
        "schema_version": 1,
        "product": identity.product,
        "context": identity.context,
        "instance": identity.instance,
        "environment_kind": identity.environment_kind,
        "deployment_record_id": identity.deployment_record_id,
        "artifact_id": identity.artifact_id,
        "source_git_ref": identity.source_git_ref,
        "image_reference": identity.image_reference,
        "release_tuple_id": identity.release_tuple_id,
        "preview_id": identity.preview_id,
        "preview_generation_id": identity.preview_generation_id,
    }
    return _canonical_sha256(payload)


def owner_acceptance_runtime_identity_binding(
    identity: RuntimeIdentity,
) -> OwnerAcceptanceRuntimeIdentityBinding:
    return OwnerAcceptanceRuntimeIdentityBinding(
        product=identity.product,
        context=identity.context,
        instance=identity.instance,
        environment_kind=identity.environment_kind,
        deployment_record_id=identity.deployment_record_id,
        artifact_id=identity.artifact_id,
        source_git_ref=identity.source_git_ref,
        image_reference=identity.image_reference,
        release_tuple_id=identity.release_tuple_id,
        preview_id=identity.preview_id,
        preview_generation_id=identity.preview_generation_id,
    )


def build_owner_acceptance_id(*, binding_sha256: str) -> str:
    return f"owner-acceptance-{_normalize_sha256(binding_sha256, 'binding_sha256')[:32]}"


def build_owner_acceptance_event_id(
    *,
    binding_sha256: str,
    action: OwnerAcceptanceAction,
    source_event_kind: OwnerAcceptanceSourceEventKind,
    source_event_id: str,
) -> str:
    payload = {
        "binding_sha256": _normalize_sha256(binding_sha256, "binding_sha256"),
        "action": _required_token(action, "action"),
        "source_event_kind": _required_token(source_event_kind, "source_event_kind"),
        "source_event_id": _required_token(source_event_id, "source_event_id"),
    }
    return f"owner-acceptance-event-{_canonical_sha256(payload)[:32]}"


def owner_acceptance_event_replay_digest(record: OwnerAcceptanceEventRecord) -> str:
    payload = record.model_dump(mode="json", exclude_none=True)
    payload.pop("occurred_at", None)
    authorization = payload.get("authorization")
    if isinstance(authorization, dict):
        authorization.pop("authorized_at", None)
    return _canonical_sha256(payload)
