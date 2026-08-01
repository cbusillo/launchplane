from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


TRUSTED_MAINTENANCE_POLICY_READ_ACTION = "trusted_maintenance_policy.read"
TRUSTED_MAINTENANCE_POLICY_WRITE_ACTION = "trusted_maintenance_policy.write"

TrustedMaintenancePolicyStatus = Literal["active", "superseded"]
TrustedMaintenanceActorType = Literal["Bot"]

_GIT_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class TrustedMaintenanceAllowedEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    event_name: str
    actions: tuple[str, ...]

    @model_validator(mode="after")
    def _validate_allowed_event(self) -> "TrustedMaintenanceAllowedEvent":
        if self.schema_version != 1:
            raise ValueError("Unsupported trusted-maintenance allowed event schema version.")
        self.event_name = _required_token(self.event_name, "event_name")
        self.actions = _required_unique_tokens(self.actions, "actions")
        return self


class TrustedMaintenanceActorRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    rule_id: str = ""
    actor_github_id: int = Field(ge=1)
    actor_type: TrustedMaintenanceActorType = "Bot"
    actor_login: str = ""
    sender_github_ids: tuple[int, ...]
    sender_type: TrustedMaintenanceActorType = "Bot"
    sender_logins: tuple[str, ...] = ()
    allowed_events: tuple[TrustedMaintenanceAllowedEvent, ...]

    @model_validator(mode="after")
    def _validate_actor_rule(self) -> "TrustedMaintenanceActorRule":
        if self.schema_version != 1:
            raise ValueError("Unsupported trusted-maintenance actor rule schema version.")
        if self.actor_type != "Bot" or self.sender_type != "Bot":
            raise ValueError(
                "trusted-maintenance v1 actor rules require Bot actor and sender types"
            )
        self.actor_login = self.actor_login.strip()
        self.sender_logins = tuple(login.strip() for login in self.sender_logins if login.strip())
        self.sender_github_ids = _positive_unique_ids(
            self.sender_github_ids,
            "sender_github_ids",
        )
        if not self.allowed_events:
            raise ValueError("trusted-maintenance actor rule requires allowed_events")
        event_keys = tuple(
            (allowed.event_name, action)
            for allowed in self.allowed_events
            for action in allowed.actions
        )
        if len(set(event_keys)) != len(event_keys):
            raise ValueError("trusted-maintenance actor rule event/action pairs must be unique")
        computed_rule_id = build_trusted_maintenance_actor_rule_id(self)
        if self.rule_id:
            self.rule_id = _required_token(self.rule_id, "rule_id")
            if self.rule_id != computed_rule_id:
                raise ValueError("trusted-maintenance actor rule_id does not match payload")
        else:
            self.rule_id = computed_rule_id
        return self


class TrustedMaintenancePolicyRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    record_id: str = ""
    status: TrustedMaintenancePolicyStatus = "active"
    repository_id: str
    repository_owner_id: str
    repository: str
    product: str
    context: str
    policy_revision: int = Field(ge=1)
    actor_rules: tuple[TrustedMaintenanceActorRule, ...]
    evidence_ttl_seconds: int | None = Field(default=None, ge=1, le=31_536_000)
    effective_at: str
    source: str
    reason: str
    supersedes_record_id: str | None = None
    policy_digest: str = ""

    @model_validator(mode="after")
    def _validate_record(self) -> "TrustedMaintenancePolicyRecord":
        if self.schema_version != 1:
            raise ValueError("Unsupported trusted-maintenance policy schema version.")
        self.repository_id = _required_decimal_id(self.repository_id, "repository_id")
        self.repository_owner_id = _required_decimal_id(
            self.repository_owner_id,
            "repository_owner_id",
        )
        self.repository = _normalize_repository(self.repository, "repository")
        self.product = _required_token(self.product, "product")
        self.context = _required_token(self.context, "context")
        if not self.actor_rules:
            raise ValueError("trusted-maintenance policy requires actor_rules")
        rule_ids = tuple(rule.rule_id for rule in self.actor_rules)
        if len(set(rule_ids)) != len(rule_ids):
            raise ValueError("trusted-maintenance policy actor rule IDs must be unique")
        self.effective_at = _normalize_utc_timestamp(self.effective_at, "effective_at")
        self.source = _required_token(self.source, "source")
        self.reason = _required_token(self.reason, "reason")
        if self.supersedes_record_id is not None:
            self.supersedes_record_id = _required_token(
                self.supersedes_record_id,
                "supersedes_record_id",
            )

        computed_record_id = build_trusted_maintenance_policy_record_id(
            repository_id=self.repository_id,
            product=self.product,
            context=self.context,
            policy_revision=self.policy_revision,
        )
        if self.record_id:
            self.record_id = _required_token(self.record_id, "record_id")
            if self.record_id != computed_record_id:
                raise ValueError("trusted-maintenance policy record_id does not match revision")
        else:
            self.record_id = computed_record_id

        computed_digest = trusted_maintenance_policy_digest(self)
        if self.policy_digest:
            self.policy_digest = _normalize_sha256(self.policy_digest, "policy_digest")
            if self.policy_digest != computed_digest:
                raise ValueError("trusted-maintenance policy digest does not match payload")
        else:
            self.policy_digest = computed_digest
        return self


class TrustedMaintenanceEvidenceBinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    repository_id: str
    repository_owner_id: str
    repository: str
    product: str
    context: str
    pull_request_number: int = Field(ge=1)
    head_sha: str
    classification_record_id: str
    classification_revision: int = Field(ge=1)
    classification_digest: str
    policy_record_id: str
    policy_revision: int = Field(ge=1)
    policy_digest: str
    matched_actor_rule_id: str
    pr_author_github_id: int = Field(ge=1)
    pr_author_type: TrustedMaintenanceActorType = "Bot"
    pr_author_login: str = ""
    sender_github_id: int = Field(ge=1)
    sender_type: TrustedMaintenanceActorType = "Bot"
    sender_login: str = ""
    head_repository_id: str
    head_repository_owner_id: str
    head_repository: str
    event_name: str
    event_action: str
    source: str
    delivery_id: str
    binding_sha256: str = ""

    @model_validator(mode="after")
    def _validate_binding(self) -> "TrustedMaintenanceEvidenceBinding":
        if self.schema_version != 1:
            raise ValueError("Unsupported trusted-maintenance evidence binding schema version.")
        self.repository_id = _required_decimal_id(self.repository_id, "repository_id")
        self.repository_owner_id = _required_decimal_id(
            self.repository_owner_id,
            "repository_owner_id",
        )
        self.repository = _normalize_repository(self.repository, "repository")
        self.product = _required_token(self.product, "product")
        self.context = _required_token(self.context, "context")
        self.head_sha = _normalize_git_sha(self.head_sha, "head_sha")
        self.classification_record_id = _required_token(
            self.classification_record_id,
            "classification_record_id",
        )
        self.classification_digest = _normalize_sha256(
            self.classification_digest,
            "classification_digest",
        )
        self.policy_record_id = _required_token(self.policy_record_id, "policy_record_id")
        self.policy_digest = _normalize_sha256(self.policy_digest, "policy_digest")
        self.matched_actor_rule_id = _required_token(
            self.matched_actor_rule_id,
            "matched_actor_rule_id",
        )
        if self.pr_author_type != "Bot" or self.sender_type != "Bot":
            raise ValueError("trusted-maintenance evidence requires Bot PR author and sender types")
        self.pr_author_login = self.pr_author_login.strip()
        self.sender_login = self.sender_login.strip()
        self.head_repository_id = _required_decimal_id(
            self.head_repository_id,
            "head_repository_id",
        )
        self.head_repository_owner_id = _required_decimal_id(
            self.head_repository_owner_id,
            "head_repository_owner_id",
        )
        self.head_repository = _normalize_repository(self.head_repository, "head_repository")
        if (
            self.head_repository_id != self.repository_id
            or self.head_repository_owner_id != self.repository_owner_id
            or self.head_repository != self.repository
        ):
            raise ValueError("trusted-maintenance evidence requires same-repository head")
        self.event_name = _required_token(self.event_name, "event_name")
        self.event_action = _required_token(self.event_action, "event_action")
        self.source = _required_token(self.source, "source")
        self.delivery_id = _required_token(self.delivery_id, "delivery_id")
        computed_binding_sha256 = trusted_maintenance_evidence_binding_sha256(self)
        if self.binding_sha256:
            self.binding_sha256 = _normalize_sha256(self.binding_sha256, "binding_sha256")
            if self.binding_sha256 != computed_binding_sha256:
                raise ValueError("trusted-maintenance evidence binding digest mismatch")
        else:
            self.binding_sha256 = computed_binding_sha256
        return self


class TrustedMaintenanceEvidenceRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    evidence_id: str = ""
    binding: TrustedMaintenanceEvidenceBinding
    occurred_at: str
    recorded_at: str = ""
    expires_at: str = ""
    evidence_digest: str = ""

    @model_validator(mode="after")
    def _validate_evidence(self) -> "TrustedMaintenanceEvidenceRecord":
        if self.schema_version != 1:
            raise ValueError("Unsupported trusted-maintenance evidence schema version.")
        self.occurred_at = _normalize_utc_timestamp(self.occurred_at, "occurred_at")
        self.recorded_at = (
            _normalize_utc_timestamp(self.recorded_at, "recorded_at")
            if self.recorded_at
            else self.occurred_at
        )
        if self.recorded_at != self.occurred_at:
            raise ValueError("trusted-maintenance evidence recorded_at must equal occurred_at")
        if self.expires_at:
            self.expires_at = _normalize_utc_timestamp(self.expires_at, "expires_at")
            if self.expires_at <= self.occurred_at:
                raise ValueError("trusted-maintenance evidence expires_at must follow occurred_at")
        computed_evidence_id = build_trusted_maintenance_evidence_id(
            source=self.binding.source,
            delivery_id=self.binding.delivery_id,
        )
        if self.evidence_id:
            self.evidence_id = _required_token(self.evidence_id, "evidence_id")
            if self.evidence_id != computed_evidence_id:
                raise ValueError("trusted-maintenance evidence_id does not match binding")
        else:
            self.evidence_id = computed_evidence_id

        computed_digest = trusted_maintenance_evidence_digest(self)
        if self.evidence_digest:
            self.evidence_digest = _normalize_sha256(self.evidence_digest, "evidence_digest")
            if self.evidence_digest != computed_digest:
                raise ValueError("trusted-maintenance evidence digest mismatch")
        else:
            self.evidence_digest = computed_digest
        return self


def build_trusted_maintenance_actor_rule_id(rule: TrustedMaintenanceActorRule) -> str:
    payload = rule.model_dump(
        mode="json",
        exclude={"rule_id", "actor_login", "sender_logins"},
        exclude_none=True,
    )
    return f"trusted-maintenance-actor-rule-{_canonical_sha256(payload)[:24]}"


def build_trusted_maintenance_policy_record_id(
    *, repository_id: str, product: str, context: str, policy_revision: int
) -> str:
    if policy_revision < 1:
        raise ValueError("trusted-maintenance policy revision must be positive")
    normalized_repository_id = _required_decimal_id(repository_id, "repository_id")
    scope_digest = _canonical_sha256(
        {
            "repository_id": normalized_repository_id,
            "product": _required_token(product, "product"),
            "context": _required_token(context, "context"),
        }
    )
    return f"trusted-maintenance-policy-{normalized_repository_id}-{scope_digest[:12]}-r{policy_revision}"


def trusted_maintenance_policy_digest(record: TrustedMaintenancePolicyRecord) -> str:
    payload = record.model_dump(
        mode="json",
        exclude={"policy_digest", "status"},
        exclude_none=True,
    )
    actor_rules = []
    for actor_rule in payload.get("actor_rules", []):
        if isinstance(actor_rule, dict):
            sanitized_rule = dict(actor_rule)
            sanitized_rule.pop("actor_login", None)
            sanitized_rule.pop("sender_logins", None)
            actor_rules.append(sanitized_rule)
        else:
            actor_rules.append(actor_rule)
    payload["actor_rules"] = actor_rules
    return _canonical_sha256(payload)


def trusted_maintenance_evidence_binding_sha256(
    binding: TrustedMaintenanceEvidenceBinding,
) -> str:
    return _model_sha256(
        binding,
        exclude={"binding_sha256", "pr_author_login", "sender_login"},
    )


def build_trusted_maintenance_evidence_id(*, source: str, delivery_id: str) -> str:
    identity_digest = _canonical_sha256(
        {
            "source": _required_token(source, "source"),
            "delivery_id": _required_token(delivery_id, "delivery_id"),
        }
    )
    return f"trusted-maintenance-evidence-{identity_digest[:32]}"


def trusted_maintenance_evidence_digest(record: TrustedMaintenanceEvidenceRecord) -> str:
    return _model_sha256(record, exclude={"evidence_digest"})


def trusted_maintenance_expires_at(*, occurred_at: str, ttl_seconds: int | None) -> str:
    if ttl_seconds is None:
        return ""
    if ttl_seconds < 1:
        raise ValueError("trusted-maintenance evidence TTL must be positive")
    parsed = _parse_utc_timestamp(occurred_at, "occurred_at")
    return (
        (parsed + timedelta(seconds=ttl_seconds))
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _positive_unique_ids(values: tuple[int, ...], label: str) -> tuple[int, ...]:
    if not values:
        raise ValueError(f"trusted-maintenance requires {label}")
    if any(value < 1 for value in values):
        raise ValueError(f"trusted-maintenance {label} must contain positive IDs")
    return tuple(dict.fromkeys(values))


def _required_unique_tokens(values: tuple[str, ...], label: str) -> tuple[str, ...]:
    normalized = tuple(_required_token(value, label) for value in values)
    if len(set(normalized)) != len(normalized):
        raise ValueError(f"trusted-maintenance {label} must be unique")
    return normalized


def _required_decimal_id(value: str, label: str) -> str:
    normalized = _required_token(value, label)
    if not normalized.isdecimal() or int(normalized) <= 0:
        raise ValueError(f"trusted-maintenance {label} requires a positive numeric ID")
    return normalized


def _normalize_repository(value: str, label: str) -> str:
    normalized = _required_token(value, label).lower()
    owner, separator, name = normalized.partition("/")
    if not separator or not owner or not name or "/" in name:
        raise ValueError(f"trusted-maintenance {label} must be OWNER/REPO")
    return normalized


def _normalize_git_sha(value: str, label: str) -> str:
    normalized = _required_token(value, label).lower()
    if _GIT_SHA_PATTERN.fullmatch(normalized) is None:
        raise ValueError(f"trusted-maintenance {label} must be an exact Git SHA")
    return normalized


def _normalize_sha256(value: str, label: str) -> str:
    normalized = _required_token(value, label).lower()
    if _SHA256_PATTERN.fullmatch(normalized) is None:
        raise ValueError(f"trusted-maintenance {label} requires a lowercase SHA-256")
    return normalized


def _required_token(value: str, label: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"trusted-maintenance requires {label}")
    return normalized


def _normalize_utc_timestamp(value: str, label: str) -> str:
    return _parse_utc_timestamp(value, label).isoformat().replace("+00:00", "Z")


def _parse_utc_timestamp(value: str, label: str) -> datetime:
    normalized = _required_token(value, label)
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"trusted-maintenance {label} must be an ISO-8601 timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"trusted-maintenance {label} requires a timezone")
    return parsed.astimezone(timezone.utc).replace(microsecond=0)


def _model_sha256(model: BaseModel, *, exclude: set[str]) -> str:
    return _canonical_sha256(model.model_dump(mode="json", exclude=exclude, exclude_none=True))


def _canonical_sha256(payload: object) -> str:
    canonical_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()
