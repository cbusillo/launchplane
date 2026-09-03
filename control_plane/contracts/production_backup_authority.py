from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


ProductionBackupAuthorityRecordStatus = Literal["active", "superseded", "retired"]
ProductionBackupAuthorityState = Literal["ready", "missing", "invalid", "stale", "retired"]
ProductionBackupProviderType = Literal["proxmox"]
ProductionBackupDestinationKind = Literal["proxmox_guest", "proxmox_storage"]

PRODUCTION_BACKUP_AUTHORITY_READ_ACTION = "production_backup_authority.read"
PRODUCTION_BACKUP_AUTHORITY_WRITE_ACTION = "production_backup_authority.write"

_IDENTIFIER_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_PROVIDER_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class ProxmoxGuestBackupDestinationReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider_type: Literal["proxmox"] = "proxmox"
    destination_kind: Literal["proxmox_guest"] = "proxmox_guest"
    host: str
    username: str
    guest_kind: Literal["lxc", "qemu"]
    guest_id: str

    @model_validator(mode="after")
    def _validate_reference(self) -> ProxmoxGuestBackupDestinationReference:
        self.host = _required_value(self.host, "destination host")
        self.username = _required_value(self.username, "destination username")
        self.guest_id = _positive_decimal(self.guest_id, "destination guest_id")
        return self


class ProxmoxStorageBackupDestinationReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider_type: Literal["proxmox"] = "proxmox"
    destination_kind: Literal["proxmox_storage"] = "proxmox_storage"
    host: str
    username: str
    storage_id: str

    @model_validator(mode="after")
    def _validate_reference(self) -> ProxmoxStorageBackupDestinationReference:
        self.host = _required_value(self.host, "destination host")
        self.username = _required_value(self.username, "destination username")
        self.storage_id = _required_provider_identifier(self.storage_id, "destination storage_id")
        return self


ProductionBackupDestinationReference = Annotated[
    ProxmoxGuestBackupDestinationReference | ProxmoxStorageBackupDestinationReference,
    Field(discriminator="destination_kind"),
]


class ProductionBackupTargetRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    record_id: str = ""
    target_id: str
    target_revision: int = Field(ge=1)
    status: ProductionBackupAuthorityRecordStatus = "active"
    destination: ProductionBackupDestinationReference
    effective_at: str
    review_after: str
    source: str
    reason: str
    supersedes_record_id: str | None = None
    target_digest: str = ""

    @model_validator(mode="after")
    def _validate_record(self) -> ProductionBackupTargetRecord:
        if self.schema_version != 1:
            raise ValueError("Unsupported production backup target schema version.")
        self.target_id = _required_identifier(self.target_id, "target_id")
        self.effective_at = _normalize_utc_timestamp(self.effective_at, "effective_at")
        self.review_after = _normalize_utc_timestamp(self.review_after, "review_after")
        if self.review_after <= self.effective_at:
            raise ValueError("production backup target review_after must follow effective_at")
        self.source = _required_value(self.source, "source")
        self.reason = _required_value(self.reason, "reason")
        if self.supersedes_record_id is not None:
            self.supersedes_record_id = _required_value(
                self.supersedes_record_id, "supersedes_record_id"
            )
        expected_record_id = build_production_backup_target_record_id(
            target_id=self.target_id,
            target_revision=self.target_revision,
        )
        if self.record_id:
            self.record_id = _required_value(self.record_id, "record_id")
            if self.record_id != expected_record_id:
                raise ValueError("production backup target record_id does not match revision")
        else:
            self.record_id = expected_record_id
        expected_digest = production_backup_target_digest(self)
        if self.target_digest:
            self.target_digest = _normalize_sha256(self.target_digest, "target_digest")
            if self.target_digest != expected_digest:
                raise ValueError("production backup target digest does not match payload")
        else:
            self.target_digest = expected_digest
        return self

    @property
    def provider_type(self) -> ProductionBackupProviderType:
        return self.destination.provider_type

    @property
    def destination_kind(self) -> ProductionBackupDestinationKind:
        return self.destination.destination_kind


class ProductionFastSnapshotPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: Literal["fast_snapshot"] = "fast_snapshot"
    source_target_id: str
    snapshot_prefix: str
    retention_count: int = Field(ge=0, le=100)
    max_evidence_age_seconds: int = Field(ge=1, le=86_400)

    @model_validator(mode="after")
    def _validate_policy(self) -> ProductionFastSnapshotPolicy:
        self.source_target_id = _required_identifier(
            self.source_target_id, "fast snapshot source_target_id"
        )
        self.snapshot_prefix = _required_provider_identifier(
            self.snapshot_prefix, "fast snapshot snapshot_prefix"
        )
        return self


class ProductionIndependentBackupPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: Literal["independent_backup"] = "independent_backup"
    source_target_id: str
    destination_target_id: str
    max_evidence_age_seconds: int = Field(ge=1, le=604_800)

    @model_validator(mode="after")
    def _validate_policy(self) -> ProductionIndependentBackupPolicy:
        self.source_target_id = _required_identifier(
            self.source_target_id, "independent backup source_target_id"
        )
        self.destination_target_id = _required_identifier(
            self.destination_target_id, "independent backup destination_target_id"
        )
        if self.source_target_id == self.destination_target_id:
            raise ValueError(
                "independent backup source_target_id and destination_target_id must differ"
            )
        return self


class ProductionBackupPolicyRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    record_id: str = ""
    policy_id: str = ""
    product: str
    context: str
    instance: str
    promotion_action: str
    policy_revision: int = Field(ge=1)
    status: ProductionBackupAuthorityRecordStatus = "active"
    fast_snapshot: ProductionFastSnapshotPolicy
    independent_backup: ProductionIndependentBackupPolicy
    effective_at: str
    review_after: str
    source: str
    reason: str
    supersedes_record_id: str | None = None
    policy_digest: str = ""

    @model_validator(mode="after")
    def _validate_record(self) -> ProductionBackupPolicyRecord:
        if self.schema_version != 1:
            raise ValueError("Unsupported production backup policy schema version.")
        self.product = _required_identifier(self.product, "product")
        self.context = _required_identifier(self.context, "context")
        self.instance = _required_identifier(self.instance, "instance")
        self.promotion_action = _required_value(self.promotion_action, "promotion_action")
        if self.fast_snapshot.source_target_id != self.independent_backup.source_target_id:
            raise ValueError("production backup policy operations must use the same source target")
        self.effective_at = _normalize_utc_timestamp(self.effective_at, "effective_at")
        self.review_after = _normalize_utc_timestamp(self.review_after, "review_after")
        if self.review_after <= self.effective_at:
            raise ValueError("production backup policy review_after must follow effective_at")
        self.source = _required_value(self.source, "source")
        self.reason = _required_value(self.reason, "reason")
        if self.supersedes_record_id is not None:
            self.supersedes_record_id = _required_value(
                self.supersedes_record_id, "supersedes_record_id"
            )
        expected_policy_id = build_production_backup_policy_id(
            product=self.product,
            context=self.context,
            instance=self.instance,
            promotion_action=self.promotion_action,
        )
        if self.policy_id:
            self.policy_id = _required_value(self.policy_id, "policy_id")
            if self.policy_id != expected_policy_id:
                raise ValueError("production backup policy_id does not match exact scope")
        else:
            self.policy_id = expected_policy_id
        expected_record_id = build_production_backup_policy_record_id(
            policy_id=self.policy_id,
            policy_revision=self.policy_revision,
        )
        if self.record_id:
            self.record_id = _required_value(self.record_id, "record_id")
            if self.record_id != expected_record_id:
                raise ValueError("production backup policy record_id does not match revision")
        else:
            self.record_id = expected_record_id
        expected_digest = production_backup_policy_digest(self)
        if self.policy_digest:
            self.policy_digest = _normalize_sha256(self.policy_digest, "policy_digest")
            if self.policy_digest != expected_digest:
                raise ValueError("production backup policy digest does not match payload")
        else:
            self.policy_digest = expected_digest
        return self

    @property
    def target_ids(self) -> tuple[str, str]:
        return (
            self.fast_snapshot.source_target_id,
            self.independent_backup.destination_target_id,
        )


class ProductionBackupTargetSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_id: str
    record_id: str
    target_revision: int = Field(ge=1)
    status: ProductionBackupAuthorityRecordStatus
    provider_type: ProductionBackupProviderType
    destination_kind: ProductionBackupDestinationKind
    effective_at: str
    review_after: str


class ProductionBackupPolicySummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    policy_id: str
    record_id: str
    policy_revision: int = Field(ge=1)
    status: ProductionBackupAuthorityRecordStatus
    promotion_action: str
    source_target_id: str
    destination_target_id: str
    effective_at: str
    review_after: str


class ProductionBackupAuthorityReadModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    product: str
    context: str
    instance: str
    promotion_action: str
    state: ProductionBackupAuthorityState
    ready: bool
    summary: str
    reason_codes: tuple[str, ...] = ()
    policy: ProductionBackupPolicySummary | None = None
    targets: tuple[ProductionBackupTargetSummary, ...] = ()
    generated_at: str


class ProductionBackupAuthorityStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    promotion_action: str
    state: ProductionBackupAuthorityState
    ready: bool
    summary: str
    reason_codes: tuple[str, ...] = ()
    generated_at: str


def production_backup_authority_status(
    authority: ProductionBackupAuthorityReadModel,
) -> ProductionBackupAuthorityStatus:
    return ProductionBackupAuthorityStatus(
        promotion_action=authority.promotion_action,
        state=authority.state,
        ready=authority.ready,
        summary=authority.summary,
        reason_codes=authority.reason_codes,
        generated_at=authority.generated_at,
    )


def build_production_backup_target_record_id(*, target_id: str, target_revision: int) -> str:
    return f"production-backup-target-{_required_identifier(target_id, 'target_id')}-r{target_revision}"


def build_production_backup_policy_id(
    *, product: str, context: str, instance: str, promotion_action: str
) -> str:
    scope = {
        "product": _required_identifier(product, "product"),
        "context": _required_identifier(context, "context"),
        "instance": _required_identifier(instance, "instance"),
        "promotion_action": _required_value(promotion_action, "promotion_action"),
    }
    digest = hashlib.sha256(
        json.dumps(scope, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return f"production-backup-policy-{digest[:24]}"


def build_production_backup_policy_record_id(*, policy_id: str, policy_revision: int) -> str:
    return f"{_required_value(policy_id, 'policy_id')}-r{policy_revision}"


def production_backup_target_digest(record: ProductionBackupTargetRecord) -> str:
    payload = {
        "schema_version": record.schema_version,
        "record_id": record.record_id,
        "target_id": record.target_id,
        "target_revision": record.target_revision,
        "destination": record.destination.model_dump(mode="json"),
        "effective_at": record.effective_at,
        "review_after": record.review_after,
        "source": record.source,
        "reason": record.reason,
        "supersedes_record_id": record.supersedes_record_id,
    }
    return _digest(payload)


def production_backup_policy_digest(record: ProductionBackupPolicyRecord) -> str:
    payload = {
        "schema_version": record.schema_version,
        "record_id": record.record_id,
        "policy_id": record.policy_id,
        "product": record.product,
        "context": record.context,
        "instance": record.instance,
        "promotion_action": record.promotion_action,
        "policy_revision": record.policy_revision,
        "fast_snapshot": record.fast_snapshot.model_dump(mode="json"),
        "independent_backup": record.independent_backup.model_dump(mode="json"),
        "effective_at": record.effective_at,
        "review_after": record.review_after,
        "source": record.source,
        "reason": record.reason,
        "supersedes_record_id": record.supersedes_record_id,
    }
    return _digest(payload)


def _digest(payload: dict[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _required_identifier(value: str, label: str) -> str:
    normalized = _required_value(value, label).lower()
    if _IDENTIFIER_PATTERN.fullmatch(normalized) is None:
        raise ValueError(
            f"production backup {label} must use lowercase letters, numbers, dots, underscores, or dashes"
        )
    return normalized


def _required_value(value: str, label: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"production backup {label} is required")
    return normalized


def _required_provider_identifier(value: str, label: str) -> str:
    normalized = _required_value(value, label)
    if _PROVIDER_IDENTIFIER_PATTERN.fullmatch(normalized) is None:
        raise ValueError(
            f"production backup {label} must use letters, numbers, dots, underscores, or dashes"
        )
    return normalized


def _positive_decimal(value: str, label: str) -> str:
    normalized = _required_value(value, label)
    if not normalized.isdecimal() or int(normalized) <= 0:
        raise ValueError(f"production backup {label} requires a positive numeric value")
    return normalized


def _normalize_utc_timestamp(value: str, label: str) -> str:
    normalized = _required_value(value, label)
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"production backup {label} must be an ISO-8601 timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"production backup {label} requires a timezone-aware UTC timestamp")
    if parsed.utcoffset() != timedelta():
        raise ValueError(f"production backup {label} must be UTC")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _normalize_sha256(value: str, label: str) -> str:
    normalized = _required_value(value, label).lower()
    if _SHA256_PATTERN.fullmatch(normalized) is None:
        raise ValueError(f"production backup {label} must be a lowercase SHA-256")
    return normalized
