from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal, Protocol, TypeVar, cast

from pydantic import BaseModel, ConfigDict, Field

from control_plane.contracts.product_owner import (
    ProductOwnerActionContext,
    ProductOwnerActorIdentity,
    ProductOwnerPolicyRecord,
    ProductOwnerRequirementRecord,
    ProductOwnerRoutingRecord,
    ProductOwnerAuthorityEvaluation,
)


class ProductOwnerPolicyConflictError(ValueError):
    """Raised on an Owner policy write race or conflicting immutable replay."""


class ProductOwnerPolicySequenceError(ValueError):
    """Raised when Owner policy revision history is stale or non-linear."""


class ProductOwnerRequirementConflictError(ValueError):
    """Raised on an Owner requirement write race or conflicting immutable replay."""


class ProductOwnerRequirementSequenceError(ValueError):
    """Raised when Owner requirement revision history is stale or non-linear."""


class ProductOwnerRoutingConflictError(ValueError):
    """Raised on an Owner routing write race or conflicting immutable replay."""


class ProductOwnerRoutingSequenceError(ValueError):
    """Raised when Owner routing revision history is stale or non-linear."""


ProductOwnerApplyMode = Literal["dry_run", "apply"]
ProductOwnerApplyStatus = Literal["would_apply", "would_replay", "applied", "replayed"]


class ProductOwnerPolicyReadStore(Protocol):
    def list_product_owner_policy_records(
        self,
        *,
        product: str = "",
        system: str = "",
        status: str = "",
        limit: int | None = None,
    ) -> tuple[ProductOwnerPolicyRecord, ...]: ...


class ProductOwnerPolicyStore(ProductOwnerPolicyReadStore, Protocol):
    def compare_and_write_product_owner_policy_record(
        self,
        record: ProductOwnerPolicyRecord,
        *,
        expected_current_record_id: str,
        expected_current_policy_digest: str,
    ) -> Literal["written", "replayed"]: ...


class ProductOwnerRequirementReadStore(Protocol):
    def list_product_owner_requirement_records(
        self,
        *,
        product: str = "",
        system: str = "",
        status: str = "",
        limit: int | None = None,
    ) -> tuple[ProductOwnerRequirementRecord, ...]: ...


class ProductOwnerRequirementStore(ProductOwnerRequirementReadStore, Protocol):
    def compare_and_write_product_owner_requirement_record(
        self,
        record: ProductOwnerRequirementRecord,
        *,
        expected_current_record_id: str,
        expected_current_requirement_digest: str,
    ) -> Literal["written", "replayed"]: ...


class ProductOwnerRoutingReadStore(Protocol):
    def list_product_owner_routing_records(
        self,
        *,
        product: str = "",
        system: str = "",
        status: str = "",
        limit: int | None = None,
    ) -> tuple[ProductOwnerRoutingRecord, ...]: ...


class ProductOwnerRoutingStore(ProductOwnerRoutingReadStore, Protocol):
    def compare_and_write_product_owner_routing_record(
        self,
        record: ProductOwnerRoutingRecord,
        *,
        expected_current_record_id: str,
        expected_current_routing_digest: str,
    ) -> Literal["written", "replayed"]: ...


class ProductOwnerPolicyApplyResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    status: ProductOwnerApplyStatus
    record: ProductOwnerPolicyRecord


class ProductOwnerRequirementApplyResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    status: ProductOwnerApplyStatus
    record: ProductOwnerRequirementRecord


class ProductOwnerRoutingApplyResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    status: ProductOwnerApplyStatus
    record: ProductOwnerRoutingRecord


class ProductOwnerReadModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    product: str
    system: str
    current_policy: ProductOwnerPolicyRecord | None = None
    current_requirement: ProductOwnerRequirementRecord | None = None
    current_routing: ProductOwnerRoutingRecord | None = None
    policy_history_count: int = Field(default=0, ge=0)
    requirement_history_count: int = Field(default=0, ge=0)
    routing_history_count: int = Field(default=0, ge=0)


RecordT = TypeVar(
    "RecordT",
    ProductOwnerPolicyRecord,
    ProductOwnerRequirementRecord,
    ProductOwnerRoutingRecord,
)


def apply_product_owner_policy(
    *,
    store: ProductOwnerPolicyStore,
    record: ProductOwnerPolicyRecord,
    expected_current_record_id: str = "",
    expected_current_policy_digest: str = "",
    mode: ProductOwnerApplyMode = "apply",
) -> ProductOwnerPolicyApplyResult:
    replay = _validate_revision_append(
        records=store.list_product_owner_policy_records(
            product=record.product,
            system=record.system,
        ),
        record=record,
        revision_field="policy_revision",
        digest_field="policy_digest",
        expected_current_record_id=expected_current_record_id,
        expected_current_digest=expected_current_policy_digest,
        sequence_error=ProductOwnerPolicySequenceError,
        conflict_error=ProductOwnerPolicyConflictError,
        label="product Owner policy",
    )
    if mode == "dry_run":
        return ProductOwnerPolicyApplyResult(
            status="would_replay" if replay else "would_apply",
            record=record,
        )
    write_status = store.compare_and_write_product_owner_policy_record(
        record,
        expected_current_record_id=expected_current_record_id,
        expected_current_policy_digest=expected_current_policy_digest,
    )
    return ProductOwnerPolicyApplyResult(
        status="replayed" if write_status == "replayed" else "applied",
        record=record,
    )


def apply_product_owner_requirement(
    *,
    store: ProductOwnerRequirementStore,
    record: ProductOwnerRequirementRecord,
    expected_current_record_id: str = "",
    expected_current_requirement_digest: str = "",
    mode: ProductOwnerApplyMode = "apply",
) -> ProductOwnerRequirementApplyResult:
    replay = _validate_revision_append(
        records=store.list_product_owner_requirement_records(
            product=record.product,
            system=record.system,
        ),
        record=record,
        revision_field="requirement_revision",
        digest_field="requirement_digest",
        expected_current_record_id=expected_current_record_id,
        expected_current_digest=expected_current_requirement_digest,
        sequence_error=ProductOwnerRequirementSequenceError,
        conflict_error=ProductOwnerRequirementConflictError,
        label="product Owner requirement",
    )
    if mode == "dry_run":
        return ProductOwnerRequirementApplyResult(
            status="would_replay" if replay else "would_apply",
            record=record,
        )
    write_status = store.compare_and_write_product_owner_requirement_record(
        record,
        expected_current_record_id=expected_current_record_id,
        expected_current_requirement_digest=expected_current_requirement_digest,
    )
    return ProductOwnerRequirementApplyResult(
        status="replayed" if write_status == "replayed" else "applied",
        record=record,
    )


def apply_product_owner_routing(
    *,
    store: ProductOwnerRoutingStore,
    record: ProductOwnerRoutingRecord,
    expected_current_record_id: str = "",
    expected_current_routing_digest: str = "",
    mode: ProductOwnerApplyMode = "apply",
) -> ProductOwnerRoutingApplyResult:
    replay = _validate_revision_append(
        records=store.list_product_owner_routing_records(
            product=record.product,
            system=record.system,
        ),
        record=record,
        revision_field="routing_revision",
        digest_field="routing_digest",
        expected_current_record_id=expected_current_record_id,
        expected_current_digest=expected_current_routing_digest,
        sequence_error=ProductOwnerRoutingSequenceError,
        conflict_error=ProductOwnerRoutingConflictError,
        label="product Owner routing",
    )
    if mode == "dry_run":
        return ProductOwnerRoutingApplyResult(
            status="would_replay" if replay else "would_apply",
            record=record,
        )
    write_status = store.compare_and_write_product_owner_routing_record(
        record,
        expected_current_record_id=expected_current_record_id,
        expected_current_routing_digest=expected_current_routing_digest,
    )
    return ProductOwnerRoutingApplyResult(
        status="replayed" if write_status == "replayed" else "applied",
        record=record,
    )


def get_product_owner_read_model(
    *,
    store: object,
    product: str,
    system: str,
) -> ProductOwnerReadModel:
    policy_store = require_product_owner_policy_read_store(store)
    requirement_store = require_product_owner_requirement_read_store(store)
    routing_store = require_product_owner_routing_read_store(store)
    policies = policy_store.list_product_owner_policy_records(product=product, system=system)
    requirements = requirement_store.list_product_owner_requirement_records(
        product=product,
        system=system,
    )
    routings = routing_store.list_product_owner_routing_records(product=product, system=system)
    evaluated_at = _evaluation_timestamp("")
    return ProductOwnerReadModel(
        product=product.strip(),
        system=system.strip(),
        current_policy=_safe_current_scoped_record(
            policies,
            product=product,
            system=system,
            revision_field="policy_revision",
            evaluated_at=evaluated_at,
            label="product Owner policy",
        ),
        current_requirement=_safe_current_scoped_record(
            requirements,
            product=product,
            system=system,
            revision_field="requirement_revision",
            evaluated_at=evaluated_at,
            label="product Owner requirement",
        ),
        current_routing=_safe_current_scoped_record(
            routings,
            product=product,
            system=system,
            revision_field="routing_revision",
            evaluated_at=evaluated_at,
            label="product Owner routing",
        ),
        policy_history_count=len(policies),
        requirement_history_count=len(requirements),
        routing_history_count=len(routings),
    )


def evaluate_product_owner_authority(
    *,
    context: ProductOwnerActionContext,
    actor: ProductOwnerActorIdentity,
    policies: tuple[ProductOwnerPolicyRecord, ...],
    requirements: tuple[ProductOwnerRequirementRecord, ...],
    routings: tuple[ProductOwnerRoutingRecord, ...],
    claimed_policy_revision: int | None = None,
    claimed_policy_digest: str = "",
    claimed_requirement_revision: int | None = None,
    claimed_requirement_digest: str = "",
    evaluated_at: str = "",
) -> ProductOwnerAuthorityEvaluation:
    normalized_evaluated_at = _evaluation_timestamp(evaluated_at)
    try:
        current_requirement = _current_scoped_record(
            requirements,
            product=context.product,
            system=context.system,
            revision_field="requirement_revision",
            evaluated_at=normalized_evaluated_at,
            label="product Owner requirement",
        )
    except ValueError:
        return _evaluation(
            decision="unavailable",
            reason_code="requirement_history_invalid",
            context=context,
            actor=actor,
        )
    if current_requirement is None:
        return _evaluation(
            decision="not_required",
            reason_code="owner_requirement_not_configured",
            context=context,
            actor=actor,
        )

    matching_requirements = tuple(
        requirement
        for requirement in current_requirement.requirements
        if requirement.action == context.action
        and context.repository_id in requirement.repository_ids
        and context.environment in requirement.environments
    )
    if not matching_requirements:
        return _evaluation(
            decision="not_required",
            reason_code="owner_action_not_required",
            context=context,
            actor=actor,
            requirement=current_requirement,
        )

    try:
        current_policy = _current_scoped_record(
            policies,
            product=context.product,
            system=context.system,
            revision_field="policy_revision",
            evaluated_at=normalized_evaluated_at,
            label="product Owner policy",
        )
    except ValueError:
        current_policy = None
    try:
        current_routing = _current_scoped_record(
            routings,
            product=context.product,
            system=context.system,
            revision_field="routing_revision",
            evaluated_at=normalized_evaluated_at,
            label="product Owner routing",
        )
    except ValueError:
        current_routing = None
    if current_policy is None:
        return _evaluation(
            decision="unavailable",
            reason_code="owner_policy_unavailable",
            context=context,
            actor=actor,
            requirement=current_requirement,
            routing=current_routing,
        )

    scoped_owners = tuple(
        owner
        for owner in current_policy.owners
        if context.repository_id in owner.repository_ids
        and context.environment in owner.environments
    )
    if not scoped_owners:
        return _evaluation(
            decision="unavailable",
            reason_code="policy_scope_not_covered",
            context=context,
            actor=actor,
            requirement=current_requirement,
            policy=current_policy,
            routing=current_routing,
        )
    notify_identity_ids = tuple(owner.identity.identity_id for owner in scoped_owners)
    preferred_identity_ids = (
        set(current_routing.preferred_owner_identity_ids) if current_routing is not None else set()
    )
    actor_is_preferred = actor.identity_id in preferred_identity_ids

    if actor.identity_id not in notify_identity_ids:
        return _evaluation(
            decision="denied",
            reason_code="actor_not_current_owner",
            context=context,
            actor=actor,
            requirement=current_requirement,
            policy=current_policy,
            routing=current_routing,
            actor_is_preferred=actor_is_preferred,
            notify_owner_identity_ids=notify_identity_ids,
        )

    if (
        claimed_policy_revision is None
        or not claimed_policy_digest.strip()
        or claimed_requirement_revision is None
        or not claimed_requirement_digest.strip()
    ):
        return _evaluation(
            decision="denied",
            reason_code="missing_authority_provenance",
            context=context,
            actor=actor,
            requirement=current_requirement,
            policy=current_policy,
            routing=current_routing,
            actor_is_preferred=actor_is_preferred,
            notify_owner_identity_ids=notify_identity_ids,
        )
    if (
        claimed_policy_revision != current_policy.policy_revision
        or claimed_policy_digest.strip().lower() != current_policy.policy_digest
    ):
        return _evaluation(
            decision="denied",
            reason_code="stale_policy",
            context=context,
            actor=actor,
            requirement=current_requirement,
            policy=current_policy,
            routing=current_routing,
            actor_is_preferred=actor_is_preferred,
            notify_owner_identity_ids=notify_identity_ids,
        )
    if (
        claimed_requirement_revision != current_requirement.requirement_revision
        or claimed_requirement_digest.strip().lower() != current_requirement.requirement_digest
    ):
        return _evaluation(
            decision="denied",
            reason_code="stale_requirement",
            context=context,
            actor=actor,
            requirement=current_requirement,
            policy=current_policy,
            routing=current_routing,
            actor_is_preferred=actor_is_preferred,
            notify_owner_identity_ids=notify_identity_ids,
        )

    return _evaluation(
        decision="authorized",
        reason_code="current_owner_quorum_satisfied",
        context=context,
        actor=actor,
        requirement=current_requirement,
        policy=current_policy,
        routing=current_routing,
        actor_is_preferred=actor_is_preferred,
        satisfying_owner_identity_ids=(actor.identity_id,),
        notify_owner_identity_ids=notify_identity_ids,
    )


def require_product_owner_policy_read_store(store: object) -> ProductOwnerPolicyReadStore:
    if not callable(getattr(store, "list_product_owner_policy_records", None)):
        raise TypeError("Product Owner policy storage is unavailable.")
    return cast(ProductOwnerPolicyReadStore, store)


def require_product_owner_requirement_read_store(store: object) -> ProductOwnerRequirementReadStore:
    if not callable(getattr(store, "list_product_owner_requirement_records", None)):
        raise TypeError("Product Owner requirement storage is unavailable.")
    return cast(ProductOwnerRequirementReadStore, store)


def require_product_owner_routing_read_store(store: object) -> ProductOwnerRoutingReadStore:
    if not callable(getattr(store, "list_product_owner_routing_records", None)):
        raise TypeError("Product Owner routing storage is unavailable.")
    return cast(ProductOwnerRoutingReadStore, store)


def require_product_owner_policy_store(store: object) -> ProductOwnerPolicyStore:
    read_store = require_product_owner_policy_read_store(store)
    if not callable(getattr(store, "compare_and_write_product_owner_policy_record", None)):
        raise TypeError("Product Owner policy mutation storage is unavailable.")
    return cast(ProductOwnerPolicyStore, read_store)


def require_product_owner_requirement_store(store: object) -> ProductOwnerRequirementStore:
    read_store = require_product_owner_requirement_read_store(store)
    if not callable(getattr(store, "compare_and_write_product_owner_requirement_record", None)):
        raise TypeError("Product Owner requirement mutation storage is unavailable.")
    return cast(ProductOwnerRequirementStore, read_store)


def require_product_owner_routing_store(store: object) -> ProductOwnerRoutingStore:
    read_store = require_product_owner_routing_read_store(store)
    if not callable(getattr(store, "compare_and_write_product_owner_routing_record", None)):
        raise TypeError("Product Owner routing mutation storage is unavailable.")
    return cast(ProductOwnerRoutingStore, read_store)


def _validate_revision_append(
    *,
    records: tuple[RecordT, ...],
    record: RecordT,
    revision_field: str,
    digest_field: str,
    expected_current_record_id: str,
    expected_current_digest: str,
    sequence_error: type[ValueError],
    conflict_error: type[ValueError],
    label: str,
) -> bool:
    _assert_record_integrity(
        record,
        label=label,
        error_type=conflict_error,
    )
    for existing in records:
        _assert_record_integrity(
            existing,
            label=label,
            error_type=sequence_error,
        )
    if record.status != "active":
        raise sequence_error(f"Incoming {label} revision must have active status.")
    if record.effective_at > _evaluation_timestamp(""):
        raise sequence_error(f"Incoming active {label} revision cannot take effect in the future.")
    same_id = tuple(existing for existing in records if existing.record_id == record.record_id)
    if same_id:
        if len(same_id) != 1 or getattr(same_id[0], digest_field) != getattr(
            record,
            digest_field,
        ):
            raise conflict_error(f"{label} record id conflicts with existing history.")
        return True

    current = _current_record(records)
    revision = int(getattr(record, revision_field))
    if current is None:
        if revision != 1:
            raise sequence_error(f"Initial {label} revision must be 1, got {revision}.")
        if expected_current_record_id.strip() or expected_current_digest.strip():
            raise conflict_error(f"Initial {label} apply requires an absent expected current tip.")
        return False

    current_revision = int(getattr(current, revision_field))
    current_digest = str(getattr(current, digest_field))
    if (
        expected_current_record_id.strip() != current.record_id
        or expected_current_digest.strip().lower() != current_digest
    ):
        raise conflict_error(f"Expected current {label} tip is stale.")
    if revision != current_revision + 1:
        raise sequence_error(
            f"Next {label} revision must be {current_revision + 1}, got {revision}."
        )
    if record.supersedes_record_id != current.record_id:
        raise sequence_error(f"Next {label} record must supersede the current record.")
    if record.effective_at < current.effective_at:
        raise sequence_error(f"Next {label} effective_at cannot precede the current revision.")
    return False


def _current_record(records: tuple[RecordT, ...]) -> RecordT | None:
    active = tuple(record for record in records if record.status == "active")
    if len(active) > 1:
        raise ValueError("Product Owner record history has multiple active revisions.")
    return active[0] if active else None


def _current_scoped_record(
    records: tuple[RecordT, ...],
    *,
    product: str,
    system: str,
    revision_field: str,
    evaluated_at: str,
    label: str,
) -> RecordT | None:
    scoped = tuple(
        record for record in records if record.product == product and record.system == system
    )
    if not scoped:
        return None
    for record in scoped:
        _assert_record_integrity(
            record,
            label=label,
            error_type=ValueError,
        )
    by_revision = {int(getattr(record, revision_field)): record for record in scoped}
    if len(by_revision) != len(scoped):
        raise ValueError(f"{label} history contains duplicate revisions.")
    current = _current_record(scoped)
    if current is None:
        raise ValueError(f"{label} history has no active revision.")
    current_revision = int(getattr(current, revision_field))
    expected_revisions = tuple(range(1, current_revision + 1))
    if tuple(sorted(by_revision)) != expected_revisions:
        raise ValueError(f"{label} history is not contiguous through the active revision.")
    for revision in expected_revisions:
        record = by_revision[revision]
        if revision == 1:
            if record.supersedes_record_id is not None:
                raise ValueError(f"Initial {label} revision cannot supersede another record.")
        elif record.supersedes_record_id != by_revision[revision - 1].record_id:
            raise ValueError(f"{label} history has a broken predecessor chain.")
        if revision > 1 and record.effective_at < by_revision[revision - 1].effective_at:
            raise ValueError(f"{label} history has a non-monotonic effective_at sequence.")
        expected_status = "active" if revision == current_revision else "superseded"
        if record.status != expected_status:
            raise ValueError(f"{label} history has an invalid revision status.")
    if current.effective_at > evaluated_at:
        raise ValueError(f"Current {label} revision is not yet effective.")
    return current


def _assert_record_integrity(
    record: RecordT,
    *,
    label: str,
    error_type: type[ValueError],
) -> None:
    try:
        type(record).model_validate(record.model_dump(mode="python"))
    except ValueError as error:
        raise error_type(f"{label} payload does not match its derived identifiers.") from error


def _evaluation_timestamp(value: str) -> str:
    if not value.strip():
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Product Owner evaluated_at must include a timezone.")
    return parsed.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _safe_current_scoped_record(
    records: tuple[RecordT, ...],
    *,
    product: str,
    system: str,
    revision_field: str,
    evaluated_at: str,
    label: str,
) -> RecordT | None:
    try:
        return _current_scoped_record(
            records,
            product=product,
            system=system,
            revision_field=revision_field,
            evaluated_at=evaluated_at,
            label=label,
        )
    except ValueError:
        return None


def _evaluation(
    *,
    decision: Literal["authorized", "denied", "not_required", "unavailable"],
    reason_code: str,
    context: ProductOwnerActionContext,
    actor: ProductOwnerActorIdentity,
    requirement: ProductOwnerRequirementRecord | None = None,
    policy: ProductOwnerPolicyRecord | None = None,
    routing: ProductOwnerRoutingRecord | None = None,
    actor_is_preferred: bool = False,
    satisfying_owner_identity_ids: tuple[str, ...] = (),
    notify_owner_identity_ids: tuple[str, ...] = (),
) -> ProductOwnerAuthorityEvaluation:
    return ProductOwnerAuthorityEvaluation(
        decision=decision,
        reason_code=reason_code,
        context=context,
        actor_identity_id=actor.identity_id,
        requirement_record_id=requirement.record_id if requirement is not None else "",
        requirement_revision=(
            requirement.requirement_revision if requirement is not None else None
        ),
        requirement_digest=(requirement.requirement_digest if requirement is not None else ""),
        policy_record_id=policy.record_id if policy is not None else "",
        policy_revision=policy.policy_revision if policy is not None else None,
        policy_digest=policy.policy_digest if policy is not None else "",
        routing_record_id=routing.record_id if routing is not None else "",
        routing_revision=routing.routing_revision if routing is not None else None,
        routing_digest=routing.routing_digest if routing is not None else "",
        actor_is_preferred=actor_is_preferred,
        satisfying_owner_identity_ids=satisfying_owner_identity_ids,
        notify_owner_identity_ids=notify_owner_identity_ids,
    )
