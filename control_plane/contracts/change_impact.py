from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


CHANGE_IMPACT_POLICY_READ_ACTION = "change_impact_policy.read"
CHANGE_IMPACT_POLICY_WRITE_ACTION = "change_impact_policy.write"
CHANGE_IMPACT_EVALUATION_READ_ACTION = "change_impact_evaluation.read"

ChangeImpactPolicyStatus = Literal["active", "superseded"]
ChangeImpactReviewTier = Literal["routine", "sensitive"]
ChangeImpactDecisionStatus = Literal["success", "unknown", "stale_head", "stale_policy"]
ChangeImpactEvidenceSource = Literal[
    "server_diff",
    "launchplane_dependency",
    "launchplane_reviewer",
]
ChangeImpactChangeKind = Literal["added", "modified", "removed", "renamed", "unknown"]
ChangeImpactStoredEvidenceKind = Literal["dependency", "reviewer"]
ChangeImpactAuthorshipResolution = Literal["resolved", "unresolved", "conflicting"]

_GIT_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_REPOSITORY_PATTERN = re.compile(r"^[^/\s]+/[^/\s]+$")
_GIT_REF_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-/]{0,254}$")


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
    return (
        parsed.astimezone(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace(
            "+00:00",
            "Z",
        )
    )


def _canonical_sha256(payload: object) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _normalize_path(value: str, field_name: str) -> str:
    normalized = _required_token(value, field_name).lstrip("/")
    if ".." in normalized.split("/"):
        raise ValueError(f"{field_name} cannot contain '..'")
    return normalized


def _normalize_prefixes(values: tuple[str, ...], field_name: str) -> tuple[str, ...]:
    prefixes = tuple(sorted({_normalize_path(value, field_name).rstrip("/") for value in values}))
    if not prefixes:
        raise ValueError(f"{field_name} must contain at least one path prefix")
    return prefixes


class ChangeImpactProductScope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(default=1, ge=1)
    product: str
    system: str
    owner_action: str = "pull_request.owner_acceptance"
    owner_environment: str = "pull_request"

    @model_validator(mode="after")
    def _validate_scope(self) -> "ChangeImpactProductScope":
        if self.schema_version != 1:
            raise ValueError("Unsupported change-impact product scope schema version.")
        object.__setattr__(self, "product", _required_token(self.product, "product"))
        object.__setattr__(self, "system", _required_token(self.system, "system"))
        object.__setattr__(self, "owner_action", _required_token(self.owner_action, "owner_action"))
        object.__setattr__(
            self,
            "owner_environment",
            _required_token(self.owner_environment, "owner_environment"),
        )
        return self


class ChangeImpactComponentRule(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(default=1, ge=1)
    rule_id: str = ""
    component: str
    path_prefixes: tuple[str, ...]
    affected_products: tuple[ChangeImpactProductScope, ...] = ()
    review_tier: ChangeImpactReviewTier = "routine"
    production_affecting: bool | None = None
    reason: str

    @model_validator(mode="after")
    def _validate_rule(self) -> "ChangeImpactComponentRule":
        if self.schema_version != 1:
            raise ValueError("Unsupported change-impact component rule schema version.")
        object.__setattr__(self, "component", _required_token(self.component, "component"))
        if self.production_affecting is False:
            object.__setattr__(self, "production_affecting", None)
        object.__setattr__(
            self,
            "path_prefixes",
            _normalize_prefixes(self.path_prefixes, "path_prefixes"),
        )
        object.__setattr__(
            self,
            "affected_products",
            tuple(sorted(set(self.affected_products), key=_product_scope_key)),
        )
        object.__setattr__(self, "reason", _required_token(self.reason, "reason"))
        computed_rule_id = build_change_impact_component_rule_id(self)
        if self.rule_id:
            normalized_rule_id = _required_token(self.rule_id, "rule_id")
            if normalized_rule_id != computed_rule_id:
                raise ValueError("change-impact component rule_id does not match payload")
            object.__setattr__(self, "rule_id", normalized_rule_id)
        else:
            object.__setattr__(self, "rule_id", computed_rule_id)
        return self


class ChangeImpactPolicyRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(default=1, ge=1)
    record_id: str = ""
    status: ChangeImpactPolicyStatus = "active"
    repository_id: str
    repository_owner_id: str
    repository: str
    policy_revision: int = Field(ge=1)
    component_rules: tuple[ChangeImpactComponentRule, ...]
    default_unknown_review_tier: Literal["sensitive"] = "sensitive"
    effective_at: str
    source: str
    reason: str
    supersedes_record_id: str | None = None
    policy_digest: str = ""

    @model_validator(mode="after")
    def _validate_record(self) -> "ChangeImpactPolicyRecord":
        if self.schema_version != 1:
            raise ValueError("Unsupported change-impact policy schema version.")
        object.__setattr__(
            self,
            "repository_id",
            _normalize_decimal_id(self.repository_id, "repository_id"),
        )
        object.__setattr__(
            self,
            "repository_owner_id",
            _normalize_decimal_id(self.repository_owner_id, "repository_owner_id"),
        )
        object.__setattr__(
            self,
            "repository",
            _normalize_repository(self.repository, "repository"),
        )
        if not self.component_rules:
            raise ValueError("change-impact policy requires component_rules")
        rule_ids = tuple(rule.rule_id for rule in self.component_rules)
        if len(rule_ids) != len(set(rule_ids)):
            raise ValueError("change-impact policy cannot repeat component rule IDs")
        components = tuple(rule.component for rule in self.component_rules)
        if len(components) != len(set(components)):
            raise ValueError("change-impact policy cannot repeat component identities")
        object.__setattr__(
            self,
            "component_rules",
            tuple(sorted(self.component_rules, key=lambda rule: rule.rule_id)),
        )
        object.__setattr__(
            self,
            "effective_at",
            _normalize_timestamp(self.effective_at, "effective_at"),
        )
        object.__setattr__(self, "source", _required_token(self.source, "source"))
        object.__setattr__(self, "reason", _required_token(self.reason, "reason"))
        if self.policy_revision == 1 and self.supersedes_record_id is not None:
            raise ValueError("initial change-impact policy cannot supersede another record")
        if self.policy_revision > 1 and not self.supersedes_record_id:
            raise ValueError("successor change-impact policy must name supersedes_record_id")
        if self.supersedes_record_id is not None:
            object.__setattr__(
                self,
                "supersedes_record_id",
                _required_token(self.supersedes_record_id, "supersedes_record_id"),
            )
        computed_record_id = build_change_impact_policy_record_id(
            repository_id=self.repository_id,
            policy_revision=self.policy_revision,
        )
        if self.record_id:
            normalized_record_id = _required_token(self.record_id, "record_id")
            if normalized_record_id != computed_record_id:
                raise ValueError("change-impact policy record_id does not match scope and revision")
            object.__setattr__(self, "record_id", normalized_record_id)
        else:
            object.__setattr__(self, "record_id", computed_record_id)
        computed_digest = change_impact_policy_digest(self)
        if self.policy_digest:
            normalized_digest = _normalize_sha256(self.policy_digest, "policy_digest")
            if normalized_digest != computed_digest:
                raise ValueError("change-impact policy digest does not match payload")
            object.__setattr__(self, "policy_digest", normalized_digest)
        else:
            object.__setattr__(self, "policy_digest", computed_digest)
        return self


class ChangeImpactTarget(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(default=1, ge=1)
    repository_id: str
    repository_owner_id: str
    repository: str
    pull_request_number: int = Field(ge=1)
    head_sha: str
    tree_sha: str

    @model_validator(mode="after")
    def _validate_target(self) -> "ChangeImpactTarget":
        if self.schema_version != 1:
            raise ValueError("Unsupported change-impact target schema version.")
        object.__setattr__(
            self,
            "repository_id",
            _normalize_decimal_id(self.repository_id, "repository_id"),
        )
        object.__setattr__(
            self,
            "repository_owner_id",
            _normalize_decimal_id(self.repository_owner_id, "repository_owner_id"),
        )
        object.__setattr__(
            self,
            "repository",
            _normalize_repository(self.repository, "repository"),
        )
        object.__setattr__(self, "head_sha", _normalize_git_sha(self.head_sha, "head_sha"))
        object.__setattr__(self, "tree_sha", _normalize_git_sha(self.tree_sha, "tree_sha"))
        return self


class ChangeImpactChangedFileEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(default=1, ge=1)
    path: str
    change_kind: ChangeImpactChangeKind = "unknown"
    source: Literal["server_diff"] = "server_diff"

    @model_validator(mode="after")
    def _validate_file(self) -> "ChangeImpactChangedFileEvidence":
        if self.schema_version != 1:
            raise ValueError("Unsupported change-impact file evidence schema version.")
        object.__setattr__(self, "path", _normalize_path(self.path, "path"))
        return self


class ChangeImpactStoredEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(default=1, ge=1)
    record_id: str
    component: str
    affected_products: tuple[ChangeImpactProductScope, ...] = ()
    kind: ChangeImpactStoredEvidenceKind
    confidence: Literal["known", "unknown", "ambiguous"] = "known"
    reason: str

    @model_validator(mode="after")
    def _validate_evidence(self) -> "ChangeImpactStoredEvidence":
        if self.schema_version != 1:
            raise ValueError("Unsupported change-impact stored evidence schema version.")
        object.__setattr__(self, "record_id", _required_token(self.record_id, "record_id"))
        object.__setattr__(self, "component", _required_token(self.component, "component"))
        object.__setattr__(self, "reason", _required_token(self.reason, "reason"))
        object.__setattr__(
            self,
            "affected_products",
            tuple(sorted(set(self.affected_products), key=_product_scope_key)),
        )
        return self


class ChangeImpactTargetReference(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(default=1, ge=1)
    repository: str
    pull_request_number: int = Field(ge=1)

    @model_validator(mode="after")
    def _validate_reference(self) -> "ChangeImpactTargetReference":
        if self.schema_version != 1:
            raise ValueError("Unsupported change-impact target reference schema version.")
        object.__setattr__(
            self,
            "repository",
            _normalize_repository(self.repository, "repository"),
        )
        return self


class ChangeImpactEvaluationMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(default=1, ge=1)
    request_id: str = Field(default="", max_length=200)
    reason: str = Field(default="", max_length=1000)

    @model_validator(mode="after")
    def _validate_metadata(self) -> "ChangeImpactEvaluationMetadata":
        if self.schema_version != 1:
            raise ValueError("Unsupported change-impact evaluation metadata schema version.")
        object.__setattr__(self, "request_id", self.request_id.strip())
        object.__setattr__(self, "reason", self.reason.strip())
        return self


class ChangeImpactEvaluationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(default=1, ge=1)
    target: ChangeImpactTargetReference
    metadata: ChangeImpactEvaluationMetadata = Field(default_factory=ChangeImpactEvaluationMetadata)

    @model_validator(mode="after")
    def _validate_request(self) -> "ChangeImpactEvaluationRequest":
        if self.schema_version != 1:
            raise ValueError("Unsupported change-impact evaluation request schema version.")
        return self


class ChangeImpactBaseEvidence(BaseModel):
    """Server-resolved base ref and SHA the reviewed change was compared against."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(default=1, ge=1)
    base_ref: str
    base_sha: str

    @model_validator(mode="after")
    def _validate_base(self) -> "ChangeImpactBaseEvidence":
        if self.schema_version != 1:
            raise ValueError("Unsupported change-impact base evidence schema version.")
        normalized_ref = _required_token(self.base_ref, "base_ref")
        if _GIT_REF_PATTERN.fullmatch(normalized_ref) is None:
            raise ValueError("base_ref must be a canonical Git ref name")
        object.__setattr__(self, "base_ref", normalized_ref)
        object.__setattr__(self, "base_sha", _normalize_git_sha(self.base_sha, "base_sha"))
        return self


class ChangeImpactAuthorshipEvidence(BaseModel):
    """Server-resolved numeric GitHub contributing identities over the reviewed range.

    ``resolution`` is ``resolved`` only when every reviewed commit and the pull
    request itself carry a consistent GitHub-linked numeric identity. Missing,
    incomplete, or contradictory identity evidence is never repaired here; it is
    reported so downstream Owner-review admissibility can fail closed.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(default=1, ge=1)
    resolution: ChangeImpactAuthorshipResolution
    contributor_github_ids: tuple[int, ...] = ()
    commit_count: int = Field(default=0, ge=0)
    reason: str = Field(default="", max_length=500)

    @model_validator(mode="after")
    def _validate_authorship(self) -> "ChangeImpactAuthorshipEvidence":
        if self.schema_version != 1:
            raise ValueError("Unsupported change-impact authorship evidence schema version.")
        for github_id in self.contributor_github_ids:
            if github_id < 1:
                raise ValueError("contributor_github_ids must be positive numeric GitHub IDs")
        object.__setattr__(
            self,
            "contributor_github_ids",
            tuple(sorted(set(self.contributor_github_ids))),
        )
        object.__setattr__(self, "reason", self.reason.strip())
        if self.resolution == "resolved" and not self.contributor_github_ids:
            raise ValueError("resolved change-impact authorship requires contributing identities")
        if self.resolution != "resolved" and not self.reason:
            raise ValueError("unresolved change-impact authorship requires a reason")
        return self


class ChangeImpactRepositoryEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(default=1, ge=1)
    target: ChangeImpactTarget
    merge_commit_sha: str = ""
    changed_files: tuple[ChangeImpactChangedFileEvidence, ...]
    base: ChangeImpactBaseEvidence | None = None
    authorship: ChangeImpactAuthorshipEvidence | None = None

    @model_validator(mode="after")
    def _validate_evidence(self) -> "ChangeImpactRepositoryEvidence":
        if self.schema_version != 1:
            raise ValueError("Unsupported change-impact repository evidence schema version.")
        if self.merge_commit_sha:
            object.__setattr__(
                self,
                "merge_commit_sha",
                _normalize_git_sha(self.merge_commit_sha, "merge_commit_sha"),
            )
        if not self.changed_files:
            raise ValueError("change-impact repository evidence requires changed files")
        paths = tuple(file.path for file in self.changed_files)
        if len(paths) != len(set(paths)):
            raise ValueError("change-impact repository evidence paths must be unique")
        object.__setattr__(
            self,
            "changed_files",
            tuple(sorted(self.changed_files, key=lambda changed_file: changed_file.path)),
        )
        return self


class ChangeImpactMatchedEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(default=1, ge=1)
    source: ChangeImpactEvidenceSource
    path: str = ""
    component: str = ""
    rule_id: str = ""
    review_tier: ChangeImpactReviewTier | None = None
    production_affecting: bool | None = None
    affected_products: tuple[ChangeImpactProductScope, ...] = ()
    reason: str


class ChangeImpactAffectedProduct(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(default=1, ge=1)
    product: str
    system: str
    owner_action: str
    owner_environment: str
    owner_acceptance_required: Literal[True] = True


class ChangeImpactCoverage(BaseModel):
    """Bounded path-coverage diagnostics, independent of classification authority."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    state: Literal["complete", "incomplete"]
    unmatched_path_count: int = Field(ge=0)
    unmatched_path_samples: tuple[Annotated[str, Field(max_length=256)], ...] = Field(
        default=(), max_length=20
    )
    truncated: bool = False


class ChangeImpactEvaluation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(default=1, ge=1)
    status: ChangeImpactDecisionStatus
    reason_code: str
    target: ChangeImpactTarget
    policy_record_id: str = ""
    policy_revision: int | None = Field(default=None, ge=1)
    policy_digest: str = ""
    engineering_review_tier: ChangeImpactReviewTier = "sensitive"
    required_engineering_review_count: Literal[1, 2] = 2
    owner_impact: Literal["required", "not_required", "unknown"] = "unknown"
    affected_products: tuple[ChangeImpactAffectedProduct, ...] = ()
    production_affecting_products: tuple[ChangeImpactProductScope, ...] = ()
    matched_evidence: tuple[ChangeImpactMatchedEvidence, ...] = ()
    unknown_evidence: tuple[str, ...] = ()
    coverage: ChangeImpactCoverage | None = None


def build_change_impact_component_rule_id(rule: ChangeImpactComponentRule) -> str:
    payload = rule.model_dump(
        mode="json",
        exclude={"rule_id"},
        exclude_none=True,
    )
    return f"change-impact-rule-{_canonical_sha256(payload)[:24]}"


def build_change_impact_policy_record_id(*, repository_id: str, policy_revision: int) -> str:
    if policy_revision < 1:
        raise ValueError("change-impact policy revision must be positive")
    normalized_repository_id = _normalize_decimal_id(repository_id, "repository_id")
    return f"change-impact-policy-{normalized_repository_id}-r{policy_revision}"


def change_impact_policy_digest(record: ChangeImpactPolicyRecord) -> str:
    return _canonical_sha256(
        record.model_dump(
            mode="json",
            exclude={"policy_digest", "status"},
            exclude_none=True,
        )
    )


def _product_scope_key(scope: ChangeImpactProductScope) -> tuple[str, str, str, str]:
    return scope.product, scope.system, scope.owner_action, scope.owner_environment
