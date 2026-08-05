from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import re
from typing import Any, Literal, Mapping, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


PRODUCT_OWNER_POLICY_READ_ACTION = "product_owner_policy.read"
PRODUCT_OWNER_POLICY_WRITE_ACTION = "product_owner_policy.write"
PRODUCT_OWNER_REQUIREMENT_READ_ACTION = "product_owner_requirement.read"
PRODUCT_OWNER_REQUIREMENT_WRITE_ACTION = "product_owner_requirement.write"
PRODUCT_OWNER_ROUTING_READ_ACTION = "product_owner_routing.read"
PRODUCT_OWNER_ROUTING_WRITE_ACTION = "product_owner_routing.write"
PRODUCT_OWNER_SHADOW_READ_ACTION = "product_owner_shadow.read"

ProductOwnerRecordStatus = Literal["active", "superseded"]
ProductOwnerShadowDecision = Literal[
    "authorized",
    "denied",
    "not_required",
    "unavailable",
]

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_ACTION_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")


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


def _normalize_utc_timestamp(value: str, field_name: str) -> str:
    normalized = _required_token(value, field_name)
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{field_name} must be an ISO-8601 timestamp") from error
    if parsed.tzinfo is None:
        raise ValueError(f"{field_name} must include a timezone")
    return parsed.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _canonical_sha256(payload: object) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _normalize_digest(value: str, field_name: str) -> str:
    normalized = value.strip().lower()
    if not _SHA256_PATTERN.fullmatch(normalized):
        raise ValueError(f"{field_name} must be a lowercase SHA-256 digest")
    return normalized


def _normalize_tokens(values: tuple[str, ...], field_name: str) -> tuple[str, ...]:
    normalized = tuple(sorted({_required_token(value, field_name) for value in values}))
    if not normalized:
        raise ValueError(f"{field_name} must contain at least one value")
    return normalized


def _normalize_repository_ids(values: tuple[str, ...]) -> tuple[str, ...]:
    normalized = tuple(
        sorted(
            {_normalize_decimal_id(value, "repository_ids") for value in values},
            key=int,
        )
    )
    if not normalized:
        raise ValueError("repository_ids must contain at least one immutable repository identity")
    return normalized


class ProductOwnerIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(default=1, ge=1)
    owner_class: Literal["owner"] = "owner"
    identity_kind: Literal["human"] = "human"
    provider: Literal["github"]
    provider_subject_id: str
    identity_id: str = ""

    @model_validator(mode="after")
    def _validate_identity(self) -> "ProductOwnerIdentity":
        if self.schema_version != 1:
            raise ValueError("Unsupported product Owner identity schema version.")
        object.__setattr__(
            self,
            "provider_subject_id",
            _normalize_decimal_id(
                self.provider_subject_id,
                "provider_subject_id",
            ),
        )
        computed_identity_id = build_product_owner_identity_id(
            provider=self.provider,
            provider_subject_id=self.provider_subject_id,
        )
        if self.identity_id:
            normalized_identity_id = _required_token(self.identity_id, "identity_id")
            if normalized_identity_id != computed_identity_id:
                raise ValueError(
                    "product Owner identity_id does not match immutable provider identity"
                )
            object.__setattr__(self, "identity_id", normalized_identity_id)
        else:
            object.__setattr__(self, "identity_id", computed_identity_id)
        return self

    def model_copy(
        self,
        *,
        update: Mapping[str, Any] | None = None,
        deep: bool = False,
    ) -> Self:
        if not update:
            return super().model_copy(deep=deep)
        payload = self.model_dump(mode="python", round_trip=True)
        payload.update(update)
        if {"provider", "provider_subject_id"} & update.keys():
            payload["identity_id"] = ""
        return type(self).model_validate(payload)


class ProductOwnerActorIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(default=1, ge=1)
    identity_kind: Literal["human"] = "human"
    provider: Literal["github"]
    provider_subject_id: str
    identity_id: str = ""

    @model_validator(mode="after")
    def _validate_actor(self) -> "ProductOwnerActorIdentity":
        if self.schema_version != 1:
            raise ValueError("Unsupported product Owner actor identity schema version.")
        object.__setattr__(
            self,
            "provider_subject_id",
            _normalize_decimal_id(
                self.provider_subject_id,
                "provider_subject_id",
            ),
        )
        computed_identity_id = build_product_owner_identity_id(
            provider=self.provider,
            provider_subject_id=self.provider_subject_id,
        )
        if self.identity_id:
            normalized_identity_id = _required_token(self.identity_id, "identity_id")
            if normalized_identity_id != computed_identity_id:
                raise ValueError("product Owner actor identity_id does not match provider identity")
            object.__setattr__(self, "identity_id", normalized_identity_id)
        else:
            object.__setattr__(self, "identity_id", computed_identity_id)
        return self

    def model_copy(
        self,
        *,
        update: Mapping[str, Any] | None = None,
        deep: bool = False,
    ) -> Self:
        if not update:
            return super().model_copy(deep=deep)
        payload = self.model_dump(mode="python", round_trip=True)
        payload.update(update)
        if {"provider", "provider_subject_id"} & update.keys():
            payload["identity_id"] = ""
        return type(self).model_validate(payload)


class ProductOwnerGrant(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(default=1, ge=1)
    grant_id: str = ""
    identity: ProductOwnerIdentity
    repository_ids: tuple[str, ...]
    environments: tuple[str, ...]

    @model_validator(mode="after")
    def _validate_grant(self) -> "ProductOwnerGrant":
        if self.schema_version != 1:
            raise ValueError("Unsupported product Owner grant schema version.")
        object.__setattr__(self, "repository_ids", _normalize_repository_ids(self.repository_ids))
        object.__setattr__(
            self,
            "environments",
            _normalize_tokens(self.environments, "environments"),
        )
        computed_grant_id = build_product_owner_grant_id(self)
        if self.grant_id:
            normalized_grant_id = _required_token(self.grant_id, "grant_id")
            if normalized_grant_id != computed_grant_id:
                raise ValueError("product Owner grant_id does not match grant scope")
            object.__setattr__(self, "grant_id", normalized_grant_id)
        else:
            object.__setattr__(self, "grant_id", computed_grant_id)
        return self

    def model_copy(
        self,
        *,
        update: Mapping[str, Any] | None = None,
        deep: bool = False,
    ) -> Self:
        if not update:
            return super().model_copy(deep=deep)
        payload = self.model_dump(mode="python", round_trip=True)
        payload.update(update)
        if {"identity", "repository_ids", "environments"} & update.keys():
            payload["grant_id"] = ""
        return type(self).model_validate(payload)


class ProductOwnerPolicyRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    record_id: str = ""
    status: ProductOwnerRecordStatus = "active"
    product: str
    system: str
    policy_revision: int = Field(ge=1)
    owners: tuple[ProductOwnerGrant, ...]
    quorum: Literal[1] = 1
    effective_at: str
    source: str
    reason: str
    supersedes_record_id: str | None = None
    policy_digest: str = ""

    @model_validator(mode="after")
    def _validate_policy(self) -> "ProductOwnerPolicyRecord":
        if self.schema_version != 1:
            raise ValueError("Unsupported product Owner policy schema version.")
        self.product = _required_token(self.product, "product")
        self.system = _required_token(self.system, "system")
        if not self.owners:
            raise ValueError("product Owner policy requires at least one Owner")
        identity_ids = tuple(owner.identity.identity_id for owner in self.owners)
        if len(identity_ids) != len(set(identity_ids)):
            raise ValueError("product Owner policy cannot repeat an immutable provider identity")
        self.owners = tuple(sorted(self.owners, key=lambda owner: owner.identity.identity_id))
        self.effective_at = _normalize_utc_timestamp(self.effective_at, "effective_at")
        self.source = _required_token(self.source, "source")
        self.reason = _required_token(self.reason, "reason")
        if self.policy_revision == 1 and self.supersedes_record_id is not None:
            raise ValueError("initial product Owner policy cannot supersede another record")
        if self.policy_revision > 1 and not self.supersedes_record_id:
            raise ValueError("successor product Owner policy must name supersedes_record_id")
        if self.supersedes_record_id is not None:
            self.supersedes_record_id = _required_token(
                self.supersedes_record_id,
                "supersedes_record_id",
            )
        computed_record_id = build_product_owner_policy_record_id(
            product=self.product,
            system=self.system,
            policy_revision=self.policy_revision,
        )
        if self.record_id:
            self.record_id = _required_token(self.record_id, "record_id")
            if self.record_id != computed_record_id:
                raise ValueError("product Owner policy record_id does not match scope and revision")
        else:
            self.record_id = computed_record_id
        computed_digest = product_owner_policy_digest(self)
        if self.policy_digest:
            self.policy_digest = _normalize_digest(self.policy_digest, "policy_digest")
            if self.policy_digest != computed_digest:
                raise ValueError("product Owner policy digest does not match payload")
        else:
            self.policy_digest = computed_digest
        return self


class ProductOwnerRequirement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    action: str
    repository_ids: tuple[str, ...]
    environments: tuple[str, ...]
    quorum: Literal[1] = 1

    @model_validator(mode="after")
    def _validate_requirement(self) -> "ProductOwnerRequirement":
        if self.schema_version != 1:
            raise ValueError("Unsupported product Owner requirement schema version.")
        self.action = _required_token(self.action, "action")
        if _ACTION_PATTERN.fullmatch(self.action) is None:
            raise ValueError("product Owner requirement action must be a stable lowercase token")
        if self.action.startswith(("authz_policy", "product_owner_")):
            raise ValueError(
                "product Owner requirements cannot gate authorization-policy or Owner administration"
            )
        self.repository_ids = _normalize_repository_ids(self.repository_ids)
        self.environments = _normalize_tokens(self.environments, "environments")
        return self


class ProductOwnerRequirementRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    record_id: str = ""
    status: ProductOwnerRecordStatus = "active"
    product: str
    system: str
    requirement_revision: int = Field(ge=1)
    requirements: tuple[ProductOwnerRequirement, ...] = ()
    enforcement_mode: Literal["shadow"] = "shadow"
    effective_at: str
    source: str
    reason: str
    supersedes_record_id: str | None = None
    requirement_digest: str = ""

    @model_validator(mode="after")
    def _validate_requirement_record(self) -> "ProductOwnerRequirementRecord":
        if self.schema_version != 1:
            raise ValueError("Unsupported product Owner requirement record schema version.")
        self.product = _required_token(self.product, "product")
        self.system = _required_token(self.system, "system")
        keys = tuple(
            (requirement.action, requirement.repository_ids, requirement.environments)
            for requirement in self.requirements
        )
        if len(keys) != len(set(keys)):
            raise ValueError(
                "product Owner requirement record cannot contain duplicate requirements"
            )
        self.requirements = tuple(
            sorted(self.requirements, key=lambda item: keys_for_requirement(item))
        )
        self.effective_at = _normalize_utc_timestamp(self.effective_at, "effective_at")
        self.source = _required_token(self.source, "source")
        self.reason = _required_token(self.reason, "reason")
        _validate_supersedes(
            revision=self.requirement_revision,
            supersedes_record_id=self.supersedes_record_id,
            label="product Owner requirement",
        )
        if self.supersedes_record_id is not None:
            self.supersedes_record_id = _required_token(
                self.supersedes_record_id,
                "supersedes_record_id",
            )
        computed_record_id = build_product_owner_requirement_record_id(
            product=self.product,
            system=self.system,
            requirement_revision=self.requirement_revision,
        )
        self.record_id = _validate_or_assign_record_id(
            current=self.record_id,
            computed=computed_record_id,
            label="product Owner requirement",
        )
        computed_digest = product_owner_requirement_digest(self)
        self.requirement_digest = _validate_or_assign_digest(
            current=self.requirement_digest,
            computed=computed_digest,
            field_name="requirement_digest",
            label="product Owner requirement",
        )
        return self


class ProductOwnerRoutingRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    record_id: str = ""
    status: ProductOwnerRecordStatus = "active"
    product: str
    system: str
    routing_revision: int = Field(ge=1)
    authoritative: Literal[False] = False
    preferred_owner_identity_ids: tuple[str, ...] = ()
    effective_at: str
    source: str
    reason: str
    supersedes_record_id: str | None = None
    routing_digest: str = ""

    @model_validator(mode="after")
    def _validate_routing_record(self) -> "ProductOwnerRoutingRecord":
        if self.schema_version != 1:
            raise ValueError("Unsupported product Owner routing record schema version.")
        self.product = _required_token(self.product, "product")
        self.system = _required_token(self.system, "system")
        self.preferred_owner_identity_ids = tuple(
            sorted(
                {
                    _required_token(identity_id, "preferred_owner_identity_ids")
                    for identity_id in self.preferred_owner_identity_ids
                }
            )
        )
        self.effective_at = _normalize_utc_timestamp(self.effective_at, "effective_at")
        self.source = _required_token(self.source, "source")
        self.reason = _required_token(self.reason, "reason")
        _validate_supersedes(
            revision=self.routing_revision,
            supersedes_record_id=self.supersedes_record_id,
            label="product Owner routing",
        )
        if self.supersedes_record_id is not None:
            self.supersedes_record_id = _required_token(
                self.supersedes_record_id,
                "supersedes_record_id",
            )
        computed_record_id = build_product_owner_routing_record_id(
            product=self.product,
            system=self.system,
            routing_revision=self.routing_revision,
        )
        self.record_id = _validate_or_assign_record_id(
            current=self.record_id,
            computed=computed_record_id,
            label="product Owner routing",
        )
        computed_digest = product_owner_routing_digest(self)
        self.routing_digest = _validate_or_assign_digest(
            current=self.routing_digest,
            computed=computed_digest,
            field_name="routing_digest",
            label="product Owner routing",
        )
        return self


class ProductOwnerActionContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    product: str
    system: str
    repository_id: str
    environment: str
    action: str

    @model_validator(mode="after")
    def _validate_context(self) -> "ProductOwnerActionContext":
        if self.schema_version != 1:
            raise ValueError("Unsupported product Owner action context schema version.")
        self.product = _required_token(self.product, "product")
        self.system = _required_token(self.system, "system")
        self.repository_id = _normalize_decimal_id(self.repository_id, "repository_id")
        self.environment = _required_token(self.environment, "environment")
        self.action = _required_token(self.action, "action")
        return self


class ProductOwnerShadowEvaluation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    mode: Literal["shadow"] = "shadow"
    authoritative: Literal[False] = False
    enforcement_effect: Literal["none"] = "none"
    decision: ProductOwnerShadowDecision
    reason_code: str
    context: ProductOwnerActionContext
    actor_identity_id: str
    requirement_record_id: str = ""
    requirement_revision: int | None = Field(default=None, ge=1)
    requirement_digest: str = ""
    policy_record_id: str = ""
    policy_revision: int | None = Field(default=None, ge=1)
    policy_digest: str = ""
    routing_record_id: str = ""
    routing_revision: int | None = Field(default=None, ge=1)
    routing_digest: str = ""
    actor_is_preferred: bool = False
    satisfying_owner_identity_ids: tuple[str, ...] = ()
    notify_owner_identity_ids: tuple[str, ...] = ()
    quorum: Literal[1] = 1


def build_product_owner_identity_id(*, provider: str, provider_subject_id: str) -> str:
    normalized_provider = _required_token(provider, "provider").lower()
    if normalized_provider != "github":
        raise ValueError("product Owner identity provider must be github")
    normalized_subject = _normalize_decimal_id(provider_subject_id, "provider_subject_id")
    digest = _canonical_sha256(
        {"provider": normalized_provider, "provider_subject_id": normalized_subject}
    )
    return f"owner-identity-{normalized_provider}-{digest[:24]}"


def build_product_owner_grant_id(grant: ProductOwnerGrant) -> str:
    digest = _canonical_sha256(
        {
            "identity_id": grant.identity.identity_id,
            "repository_ids": grant.repository_ids,
            "environments": grant.environments,
        }
    )
    return f"product-owner-grant-{digest[:24]}"


def _scope_record_id(*, kind: str, product: str, system: str, revision: int) -> str:
    if revision < 1:
        raise ValueError(f"{kind} revision must be positive")
    normalized_product = _required_token(product, "product")
    normalized_system = _required_token(system, "system")
    scope_digest = _canonical_sha256({"product": normalized_product, "system": normalized_system})
    return f"product-owner-{kind}-{scope_digest[:16]}-r{revision}"


def build_product_owner_policy_record_id(*, product: str, system: str, policy_revision: int) -> str:
    return _scope_record_id(
        kind="policy",
        product=product,
        system=system,
        revision=policy_revision,
    )


def build_product_owner_requirement_record_id(
    *, product: str, system: str, requirement_revision: int
) -> str:
    return _scope_record_id(
        kind="requirement",
        product=product,
        system=system,
        revision=requirement_revision,
    )


def build_product_owner_routing_record_id(
    *, product: str, system: str, routing_revision: int
) -> str:
    return _scope_record_id(
        kind="routing",
        product=product,
        system=system,
        revision=routing_revision,
    )


def product_owner_policy_digest(record: ProductOwnerPolicyRecord) -> str:
    return _canonical_sha256(
        record.model_dump(
            mode="json",
            exclude={"policy_digest", "status"},
            exclude_none=True,
        )
    )


def product_owner_requirement_digest(record: ProductOwnerRequirementRecord) -> str:
    return _canonical_sha256(
        record.model_dump(
            mode="json",
            exclude={"requirement_digest", "status"},
            exclude_none=True,
        )
    )


def product_owner_routing_digest(record: ProductOwnerRoutingRecord) -> str:
    return _canonical_sha256(
        record.model_dump(
            mode="json",
            exclude={"routing_digest", "status"},
            exclude_none=True,
        )
    )


def keys_for_requirement(requirement: ProductOwnerRequirement) -> tuple[object, ...]:
    return requirement.action, requirement.repository_ids, requirement.environments


def _validate_supersedes(*, revision: int, supersedes_record_id: str | None, label: str) -> None:
    if revision == 1 and supersedes_record_id is not None:
        raise ValueError(f"initial {label} record cannot supersede another record")
    if revision > 1 and not supersedes_record_id:
        raise ValueError(f"successor {label} record must name supersedes_record_id")


def _validate_or_assign_record_id(*, current: str, computed: str, label: str) -> str:
    if not current:
        return computed
    normalized = _required_token(current, "record_id")
    if normalized != computed:
        raise ValueError(f"{label} record_id does not match scope and revision")
    return normalized


def _validate_or_assign_digest(*, current: str, computed: str, field_name: str, label: str) -> str:
    if not current:
        return computed
    normalized = _normalize_digest(current, field_name)
    if normalized != computed:
        raise ValueError(f"{label} digest does not match payload")
    return normalized
