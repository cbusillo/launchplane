from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


TENANT_TECHNICAL_HUMAN_WAIVER_WRITE_ACTION = "tenant_technical_human_waiver.write"

RepositoryHumanRolePolicyStatus = Literal["active", "superseded"]
RepositoryHumanManagerAuthorityKind = Literal[
    "manager_primary",
    "manager_backup",
    "manager_delegated",
]
RepositoryHumanAuthorityKind = Literal[
    "repository_owner",
    "manager_primary",
    "manager_backup",
    "manager_delegated",
]
TenantTechnicalHumanWaiverAction = Literal["created", "revoked"]

_GIT_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class RepositoryHumanManagerDelegation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    delegation_id: str = ""
    delegated_manager_github_id: int = Field(ge=1)
    delegated_by_github_id: int = Field(ge=1)
    starts_at: str
    expires_at: str
    revoked_at: str = ""
    source_event_kind: str
    source_event_id: str
    reason: str

    @model_validator(mode="after")
    def _validate_delegation(self) -> "RepositoryHumanManagerDelegation":
        if self.schema_version != 1:
            raise ValueError("Unsupported repository human manager delegation schema version.")
        self.starts_at = _normalize_utc_timestamp(self.starts_at, "starts_at")
        self.expires_at = _normalize_utc_timestamp(self.expires_at, "expires_at")
        if self.expires_at <= self.starts_at:
            raise ValueError("repository human manager delegation expires_at must follow starts_at")
        if self.revoked_at:
            self.revoked_at = _normalize_utc_timestamp(self.revoked_at, "revoked_at")
        self.source_event_kind = _required_token(self.source_event_kind, "source_event_kind")
        self.source_event_id = _required_token(self.source_event_id, "source_event_id")
        self.reason = _required_token(self.reason, "reason")
        computed_delegation_id = build_repository_human_manager_delegation_id(self)
        if self.delegation_id:
            self.delegation_id = _required_token(self.delegation_id, "delegation_id")
            if self.delegation_id != computed_delegation_id:
                raise ValueError("repository human manager delegation_id does not match payload")
        else:
            self.delegation_id = computed_delegation_id
        return self


class RepositoryHumanRolePolicyRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    record_id: str = ""
    status: RepositoryHumanRolePolicyStatus = "active"
    repository_id: str
    repository_owner_id: str
    repository: str
    product: str
    context: str
    role_policy_revision: int = Field(ge=1)
    repository_owner_github_ids: tuple[int, ...]
    manager_primary_github_ids: tuple[int, ...]
    manager_backup_github_ids: tuple[int, ...] = ()
    manager_delegations: tuple[RepositoryHumanManagerDelegation, ...] = ()
    effective_at: str
    source: str
    reason: str
    supersedes_record_id: str | None = None
    role_policy_digest: str = ""

    @model_validator(mode="after")
    def _validate_record(self) -> "RepositoryHumanRolePolicyRecord":
        if self.schema_version != 1:
            raise ValueError("Unsupported repository human role policy schema version.")
        self.repository_id = _required_decimal_id(self.repository_id, "repository_id")
        self.repository_owner_id = _required_decimal_id(
            self.repository_owner_id, "repository_owner_id"
        )
        self.repository = _normalize_repository(self.repository, "repository")
        self.product = _required_token(self.product, "product")
        self.context = _required_token(self.context, "context")
        self.repository_owner_github_ids = _positive_unique_ids(
            self.repository_owner_github_ids, "repository_owner_github_ids"
        )
        self.manager_primary_github_ids = _positive_unique_ids(
            self.manager_primary_github_ids, "manager_primary_github_ids"
        )
        self.manager_backup_github_ids = _positive_unique_ids(
            self.manager_backup_github_ids, "manager_backup_github_ids", required=False
        )
        primary_and_backup = set(self.manager_primary_github_ids) & set(
            self.manager_backup_github_ids
        )
        if primary_and_backup:
            raise ValueError("repository human managers cannot be both primary and backup")
        standing_manager_ids = set(self.manager_primary_github_ids) | set(
            self.manager_backup_github_ids
        )
        for delegation in self.manager_delegations:
            if delegation.delegated_by_github_id not in standing_manager_ids:
                raise ValueError(
                    "repository human manager delegation must be granted by a primary or backup manager"
                )
            if delegation.delegated_manager_github_id in standing_manager_ids:
                raise ValueError(
                    "repository human manager delegation must name a non-standing manager"
                )
        delegation_ids = tuple(delegation.delegation_id for delegation in self.manager_delegations)
        if len(set(delegation_ids)) != len(delegation_ids):
            raise ValueError("repository human manager delegation IDs must be unique")
        self.effective_at = _normalize_utc_timestamp(self.effective_at, "effective_at")
        self.source = _required_token(self.source, "source")
        self.reason = _required_token(self.reason, "reason")
        if self.supersedes_record_id is not None:
            self.supersedes_record_id = _required_token(
                self.supersedes_record_id, "supersedes_record_id"
            )

        computed_record_id = build_repository_human_role_policy_record_id(
            repository_id=self.repository_id,
            product=self.product,
            context=self.context,
            role_policy_revision=self.role_policy_revision,
        )
        if self.record_id:
            self.record_id = _required_token(self.record_id, "record_id")
            if self.record_id != computed_record_id:
                raise ValueError("repository human role policy record_id does not match revision")
        else:
            self.record_id = computed_record_id

        computed_digest = repository_human_role_policy_digest(self)
        if self.role_policy_digest:
            self.role_policy_digest = _normalize_sha256(
                self.role_policy_digest, "role_policy_digest"
            )
            if self.role_policy_digest != computed_digest:
                raise ValueError("repository human role policy digest does not match payload")
        else:
            self.role_policy_digest = computed_digest
        return self


class RepositoryHumanRolePolicyProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    repository_id: str
    repository_owner_id: str
    repository: str
    product: str
    context: str
    role_policy_record_id: str
    role_policy_revision: int = Field(ge=1)
    role_policy_digest: str
    role_policy_source: str
    authority_kind: RepositoryHumanAuthorityKind
    delegation_id: str = ""
    evaluated_at: str

    @model_validator(mode="after")
    def _validate_provenance(self) -> "RepositoryHumanRolePolicyProvenance":
        if self.schema_version != 1:
            raise ValueError("Unsupported repository human role policy provenance schema version.")
        self.repository_id = _required_decimal_id(self.repository_id, "repository_id")
        self.repository_owner_id = _required_decimal_id(
            self.repository_owner_id, "repository_owner_id"
        )
        self.repository = _normalize_repository(self.repository, "repository")
        self.product = _required_token(self.product, "product")
        self.context = _required_token(self.context, "context")
        self.role_policy_record_id = _required_token(
            self.role_policy_record_id, "role_policy_record_id"
        )
        self.role_policy_digest = _normalize_sha256(self.role_policy_digest, "role_policy_digest")
        self.role_policy_source = _required_token(self.role_policy_source, "role_policy_source")
        self.delegation_id = self.delegation_id.strip()
        if self.authority_kind == "manager_delegated" and not self.delegation_id:
            raise ValueError("delegated manager provenance requires delegation_id")
        if self.authority_kind != "manager_delegated" and self.delegation_id:
            raise ValueError("only delegated manager provenance can include delegation_id")
        self.evaluated_at = _normalize_utc_timestamp(self.evaluated_at, "evaluated_at")
        return self


class TenantTechnicalHumanWaiverBinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    repository_id: str
    repository_owner_id: str
    repository: str
    product: str
    context: str
    pull_request_number: int = Field(ge=1)
    head_sha: str
    classification_revision: int = Field(ge=1)
    classification_digest: str
    role_policy_record_id: str
    role_policy_revision: int = Field(ge=1)
    role_policy_digest: str
    authz_policy_record_id: str
    authz_policy_revision: int = Field(ge=1)
    authz_policy_digest: str
    binding_sha256: str = ""

    @model_validator(mode="after")
    def _validate_binding(self) -> "TenantTechnicalHumanWaiverBinding":
        if self.schema_version != 1:
            raise ValueError("Unsupported tenant technical human waiver binding schema version.")
        self.repository_id = _required_decimal_id(self.repository_id, "repository_id")
        self.repository_owner_id = _required_decimal_id(
            self.repository_owner_id, "repository_owner_id"
        )
        self.repository = _normalize_repository(self.repository, "repository")
        self.product = _required_token(self.product, "product")
        self.context = _required_token(self.context, "context")
        self.head_sha = _normalize_git_sha(self.head_sha, "head_sha")
        self.classification_digest = _normalize_sha256(
            self.classification_digest, "classification_digest"
        )
        self.role_policy_record_id = _required_token(
            self.role_policy_record_id, "role_policy_record_id"
        )
        self.role_policy_digest = _normalize_sha256(self.role_policy_digest, "role_policy_digest")
        self.authz_policy_record_id = _required_token(
            self.authz_policy_record_id, "authz_policy_record_id"
        )
        self.authz_policy_digest = _normalize_sha256(
            self.authz_policy_digest, "authz_policy_digest"
        )
        computed_binding_sha256 = tenant_technical_human_waiver_binding_sha256(self)
        if self.binding_sha256:
            self.binding_sha256 = _normalize_sha256(self.binding_sha256, "binding_sha256")
            if self.binding_sha256 != computed_binding_sha256:
                raise ValueError("tenant technical human waiver binding digest mismatch")
        else:
            self.binding_sha256 = computed_binding_sha256
        return self


class TenantTechnicalHumanWaiverAuthorization(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    author_github_id: int = Field(ge=1)
    author_login: str
    action: Literal["tenant_technical_human_waiver.write"] = "tenant_technical_human_waiver.write"
    managed_set_id: str
    managed_rule_id: str
    authz_policy_record_id: str
    authz_policy_revision: int = Field(ge=1)
    authz_policy_schema_version: Literal[2] = 2
    authz_policy_digest: str
    authz_policy_source: str
    role_policy_provenance: RepositoryHumanRolePolicyProvenance
    authorized_at: str

    @model_validator(mode="after")
    def _validate_authorization(self) -> "TenantTechnicalHumanWaiverAuthorization":
        if self.schema_version != 1:
            raise ValueError(
                "Unsupported tenant technical human waiver authorization schema version."
            )
        self.author_login = _required_token(self.author_login, "author_login")
        if self.role_policy_provenance.authority_kind != "repository_owner":
            raise ValueError(
                "tenant technical human waiver author must have repository_owner authority"
            )
        for field_name in (
            "managed_set_id",
            "managed_rule_id",
            "authz_policy_record_id",
            "authz_policy_source",
        ):
            setattr(self, field_name, _required_token(str(getattr(self, field_name)), field_name))
        self.authz_policy_digest = _normalize_sha256(
            self.authz_policy_digest, "authz_policy_digest"
        )
        self.authorized_at = _normalize_utc_timestamp(self.authorized_at, "authorized_at")
        return self


class TenantTechnicalHumanWaiverEventRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    event_id: str = ""
    waiver_id: str = ""
    binding: TenantTechnicalHumanWaiverBinding
    action: TenantTechnicalHumanWaiverAction
    occurred_at: str
    source_event_kind: str
    source_event_id: str
    reason: str
    authorization: TenantTechnicalHumanWaiverAuthorization
    expires_at: str = ""
    event_digest: str = ""

    @model_validator(mode="after")
    def _validate_event(self) -> "TenantTechnicalHumanWaiverEventRecord":
        if self.schema_version != 1:
            raise ValueError("Unsupported tenant technical human waiver event schema version.")
        self.occurred_at = _normalize_utc_timestamp(self.occurred_at, "occurred_at")
        self.source_event_kind = _required_token(self.source_event_kind, "source_event_kind")
        self.source_event_id = _required_token(self.source_event_id, "source_event_id")
        self.reason = _required_token(self.reason, "reason")
        if self.authorization.authorized_at != self.occurred_at:
            raise ValueError("tenant technical human waiver authorization timestamp mismatch")
        if self.authorization.action != TENANT_TECHNICAL_HUMAN_WAIVER_WRITE_ACTION:
            raise ValueError("tenant technical human waiver authorization action mismatch")
        role_provenance = self.authorization.role_policy_provenance
        if (
            self.binding.repository_id != role_provenance.repository_id
            or self.binding.repository_owner_id != role_provenance.repository_owner_id
            or self.binding.repository != role_provenance.repository
            or self.binding.product != role_provenance.product
            or self.binding.context != role_provenance.context
            or self.binding.role_policy_record_id != role_provenance.role_policy_record_id
            or self.binding.role_policy_revision != role_provenance.role_policy_revision
            or self.binding.role_policy_digest != role_provenance.role_policy_digest
            or self.binding.authz_policy_record_id != self.authorization.authz_policy_record_id
            or self.binding.authz_policy_revision != self.authorization.authz_policy_revision
            or self.binding.authz_policy_digest != self.authorization.authz_policy_digest
        ):
            raise ValueError("tenant technical human waiver authorization does not match binding")
        if self.expires_at:
            self.expires_at = _normalize_utc_timestamp(self.expires_at, "expires_at")
            if self.expires_at <= self.occurred_at:
                raise ValueError("tenant technical human waiver expires_at must follow occurred_at")
            if self.action != "created":
                raise ValueError("only tenant technical human waiver creation can expire")

        computed_waiver_id = build_tenant_technical_human_waiver_id(
            binding_sha256=self.binding.binding_sha256
        )
        if self.waiver_id:
            self.waiver_id = _required_token(self.waiver_id, "waiver_id")
            if self.waiver_id != computed_waiver_id:
                raise ValueError("tenant technical human waiver_id does not match binding")
        else:
            self.waiver_id = computed_waiver_id

        computed_event_id = build_tenant_technical_human_waiver_event_id(
            binding_sha256=self.binding.binding_sha256,
            action=self.action,
            source_event_kind=self.source_event_kind,
            source_event_id=self.source_event_id,
            author_github_id=self.authorization.author_github_id,
        )
        if self.event_id:
            self.event_id = _required_token(self.event_id, "event_id")
            if self.event_id != computed_event_id:
                raise ValueError("tenant technical human waiver event_id does not match payload")
        else:
            self.event_id = computed_event_id

        computed_event_digest = tenant_technical_human_waiver_event_digest(self)
        if self.event_digest:
            self.event_digest = _normalize_sha256(self.event_digest, "event_digest")
            if self.event_digest != computed_event_digest:
                raise ValueError("tenant technical human waiver event_digest mismatch")
        else:
            self.event_digest = computed_event_digest
        return self

    @property
    def author_github_id(self) -> int:
        return self.authorization.author_github_id


def build_repository_human_manager_delegation_id(
    delegation: RepositoryHumanManagerDelegation,
) -> str:
    payload = delegation.model_dump(
        mode="json",
        exclude={"delegation_id", "revoked_at"},
        exclude_none=True,
    )
    return f"repository-human-manager-delegation-{_canonical_sha256(payload)[:24]}"


def build_repository_human_role_policy_record_id(
    *, repository_id: str, product: str, context: str, role_policy_revision: int
) -> str:
    if role_policy_revision < 1:
        raise ValueError("repository human role policy revision must be positive")
    normalized_repository_id = _required_decimal_id(repository_id, "repository_id")
    scope_digest = _canonical_sha256(
        {
            "repository_id": normalized_repository_id,
            "product": _required_token(product, "product"),
            "context": _required_token(context, "context"),
        }
    )
    return f"repository-human-role-policy-{normalized_repository_id}-{scope_digest[:12]}-r{role_policy_revision}"


def repository_human_role_policy_digest(record: RepositoryHumanRolePolicyRecord) -> str:
    return _model_sha256(record, exclude={"role_policy_digest", "status"})


def tenant_technical_human_waiver_binding_sha256(
    binding: TenantTechnicalHumanWaiverBinding,
) -> str:
    return _model_sha256(binding, exclude={"binding_sha256"})


def tenant_technical_human_waiver_event_digest(
    event: TenantTechnicalHumanWaiverEventRecord,
) -> str:
    return _model_sha256(event, exclude={"event_digest"})


def build_tenant_technical_human_waiver_id(*, binding_sha256: str) -> str:
    normalized = _normalize_sha256(binding_sha256, "binding_sha256")
    return f"tenant-technical-human-waiver-{normalized[:32]}"


def build_tenant_technical_human_waiver_event_id(
    *,
    binding_sha256: str,
    action: TenantTechnicalHumanWaiverAction,
    source_event_kind: str,
    source_event_id: str,
    author_github_id: int,
) -> str:
    if author_github_id < 1:
        raise ValueError("tenant technical human waiver author_github_id must be positive")
    payload = {
        "binding_sha256": _normalize_sha256(binding_sha256, "binding_sha256"),
        "action": action,
        "source_event_kind": _required_token(source_event_kind, "source_event_kind"),
        "source_event_id": _required_token(source_event_id, "source_event_id"),
        "author_github_id": author_github_id,
    }
    return f"tenant-technical-human-waiver-event-{_canonical_sha256(payload)[:32]}"


def _positive_unique_ids(
    values: tuple[int, ...], label: str, *, required: bool = True
) -> tuple[int, ...]:
    if not values and required:
        raise ValueError(f"repository human role policy requires {label}")
    if any(value < 1 for value in values):
        raise ValueError(f"repository human role policy {label} must contain positive IDs")
    return tuple(dict.fromkeys(values))


def _required_decimal_id(value: str, label: str) -> str:
    normalized = _required_token(value, label)
    if not normalized.isdecimal() or int(normalized) <= 0:
        raise ValueError(f"repository human admission {label} requires a positive numeric ID")
    return normalized


def _normalize_repository(value: str, label: str) -> str:
    normalized = _required_token(value, label).lower()
    owner, separator, name = normalized.partition("/")
    if not separator or not owner or not name or "/" in name:
        raise ValueError(f"repository human admission {label} must be OWNER/REPO")
    return normalized


def _normalize_git_sha(value: str, label: str) -> str:
    normalized = _required_token(value, label).lower()
    if _GIT_SHA_PATTERN.fullmatch(normalized) is None:
        raise ValueError(f"repository human admission {label} must be an exact Git SHA")
    return normalized


def _normalize_sha256(value: str, label: str) -> str:
    normalized = _required_token(value, label).lower()
    if _SHA256_PATTERN.fullmatch(normalized) is None:
        raise ValueError(f"repository human admission {label} requires a lowercase SHA-256")
    return normalized


def _required_token(value: str, label: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"repository human admission requires {label}")
    return normalized


def _normalize_utc_timestamp(value: str, label: str) -> str:
    normalized = _required_token(value, label)
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(
            f"repository human admission {label} must be an ISO-8601 timestamp"
        ) from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"repository human admission {label} requires a timezone")
    return parsed.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _model_sha256(model: BaseModel, *, exclude: set[str]) -> str:
    return _canonical_sha256(model.model_dump(mode="json", exclude=exclude, exclude_none=True))


def _canonical_sha256(payload: object) -> str:
    canonical_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()
