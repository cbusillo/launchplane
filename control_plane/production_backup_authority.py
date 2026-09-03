from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
from typing import Literal, Protocol, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.production_backup_authority import (
    ProductionBackupAuthorityReadModel,
    ProductionBackupPolicyRecord,
    ProductionBackupPolicySummary,
    ProductionBackupTargetRecord,
    ProductionBackupTargetSummary,
)


class ProductionBackupAuthorityConflictError(ValueError):
    pass


class ProductionBackupAuthoritySequenceError(ValueError):
    pass


class ProductionBackupAuthorityStore(Protocol):
    def list_production_backup_target_records(
        self,
        *,
        target_id: str = "",
        status: str = "",
        limit: int | None = None,
    ) -> tuple[ProductionBackupTargetRecord, ...]: ...

    def list_production_backup_policy_records(
        self,
        *,
        product: str = "",
        context_name: str = "",
        instance_name: str = "",
        promotion_action: str = "",
        status: str = "",
        limit: int | None = None,
    ) -> tuple[ProductionBackupPolicyRecord, ...]: ...


ProductionBackupAuthorityWriteMode = Literal["dry_run", "apply"]


class ProductionBackupAuthorityWriteEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    mode: ProductionBackupAuthorityWriteMode
    targets: tuple[ProductionBackupTargetRecord, ...] = ()
    policy: ProductionBackupPolicyRecord
    expected_current_target_record_ids: dict[str, str] = Field(default_factory=dict)
    expected_current_policy_record_id: str = ""
    reviewed_authority_digest: str = ""

    @model_validator(mode="after")
    def _validate_envelope(self) -> ProductionBackupAuthorityWriteEnvelope:
        if self.schema_version != 1:
            raise ValueError("Unsupported production backup authority write schema version.")
        target_ids = tuple(target.target_id for target in self.targets)
        if len(set(target_ids)) != len(target_ids):
            raise ValueError("production backup authority target writes must use unique target IDs")
        self.expected_current_target_record_ids = {
            target_id.strip().lower(): record_id.strip()
            for target_id, record_id in self.expected_current_target_record_ids.items()
        }
        unknown_expectations = set(self.expected_current_target_record_ids) - set(target_ids)
        if unknown_expectations:
            raise ValueError(
                "production backup authority target expectations require matching target writes"
            )
        self.expected_current_policy_record_id = self.expected_current_policy_record_id.strip()
        self.reviewed_authority_digest = self.reviewed_authority_digest.strip().lower()
        if self.mode == "apply" and not self.reviewed_authority_digest:
            raise ValueError("production backup authority apply requires reviewed_authority_digest")
        return self


class ProductionBackupAuthorityWriteResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    mode: ProductionBackupAuthorityWriteMode
    status: Literal["would_apply", "applied", "replayed"]
    authority_digest: str
    policy: ProductionBackupPolicySummary
    targets: tuple[ProductionBackupTargetSummary, ...]


@dataclass(frozen=True, slots=True)
class ProductionBackupAuthorityWritePlan:
    target_plans: tuple[ProductionBackupTargetAppendPlan, ...]
    policy_plan: ProductionBackupPolicyAppendPlan
    future_target_records: tuple[ProductionBackupTargetRecord, ...]
    result: ProductionBackupAuthorityWriteResult


@dataclass(frozen=True, slots=True)
class ProductionBackupTargetAppendPlan:
    status: Literal["written", "replayed"]
    current_record: ProductionBackupTargetRecord | None
    superseded_current_record: ProductionBackupTargetRecord | None = None


@dataclass(frozen=True, slots=True)
class ProductionBackupPolicyAppendPlan:
    status: Literal["written", "replayed"]
    current_record: ProductionBackupPolicyRecord | None
    superseded_current_record: ProductionBackupPolicyRecord | None = None


def require_production_backup_authority_store(
    record_store: object,
) -> ProductionBackupAuthorityStore:
    required_methods = (
        "list_production_backup_target_records",
        "list_production_backup_policy_records",
    )
    missing_methods = tuple(
        method_name
        for method_name in required_methods
        if not callable(getattr(record_store, method_name, None))
    )
    if missing_methods:
        raise TypeError(
            "Launchplane record store does not support production backup authority reads: "
            + ", ".join(missing_methods)
        )
    return cast(ProductionBackupAuthorityStore, record_store)


def plan_production_backup_target_append(
    *,
    records: tuple[ProductionBackupTargetRecord, ...],
    record: ProductionBackupTargetRecord,
) -> ProductionBackupTargetAppendPlan:
    stream = tuple(existing for existing in records if existing.target_id == record.target_id)
    if not stream:
        if record.target_revision != 1:
            raise ProductionBackupAuthoritySequenceError(
                "First production backup target record must have revision 1."
            )
        if record.status != "active":
            raise ProductionBackupAuthoritySequenceError(
                "First production backup target record must be active."
            )
        if record.supersedes_record_id:
            raise ProductionBackupAuthoritySequenceError(
                "First production backup target record cannot supersede another record."
            )
        return ProductionBackupTargetAppendPlan(status="written", current_record=None)

    current_record = _validate_target_history(stream)
    if record.provider_type != current_record.provider_type:
        raise ProductionBackupAuthorityConflictError(
            "Production backup target provider_type is immutable across revisions."
        )
    if record.destination_kind != current_record.destination_kind:
        raise ProductionBackupAuthorityConflictError(
            "Production backup target destination_kind is immutable across revisions."
        )
    same_revision = tuple(
        existing for existing in stream if existing.target_revision == record.target_revision
    )
    if same_revision:
        existing = same_revision[0]
        if (
            existing.target_digest == record.target_digest
            and existing.record_id == record.record_id
            and existing.status == record.status
        ):
            return ProductionBackupTargetAppendPlan(
                status="replayed",
                current_record=current_record,
            )
        raise ProductionBackupAuthorityConflictError(
            "Production backup target revision already exists with a different payload."
        )
    if record.status not in {"active", "retired"}:
        raise ProductionBackupAuthoritySequenceError(
            "New production backup target revisions must be active or retired."
        )
    if record.target_revision != current_record.target_revision + 1:
        raise ProductionBackupAuthoritySequenceError(
            "Production backup target revisions must append contiguously."
        )
    if record.supersedes_record_id != current_record.record_id:
        raise ProductionBackupAuthoritySequenceError(
            "Production backup target supersedes_record_id must match the current record."
        )
    return ProductionBackupTargetAppendPlan(
        status="written",
        current_record=current_record,
        superseded_current_record=current_record.model_copy(update={"status": "superseded"}),
    )


def plan_production_backup_policy_append(
    *,
    records: tuple[ProductionBackupPolicyRecord, ...],
    record: ProductionBackupPolicyRecord,
) -> ProductionBackupPolicyAppendPlan:
    stream = tuple(existing for existing in records if existing.policy_id == record.policy_id)
    if not stream:
        if record.policy_revision != 1:
            raise ProductionBackupAuthoritySequenceError(
                "First production backup policy record must have revision 1."
            )
        if record.status != "active":
            raise ProductionBackupAuthoritySequenceError(
                "First production backup policy record must be active."
            )
        if record.supersedes_record_id:
            raise ProductionBackupAuthoritySequenceError(
                "First production backup policy record cannot supersede another record."
            )
        return ProductionBackupPolicyAppendPlan(status="written", current_record=None)

    current_record = _validate_policy_history(stream)
    if not _same_policy_scope(current_record, record):
        raise ProductionBackupAuthorityConflictError(
            "Production backup policy exact scope is immutable across revisions."
        )
    same_revision = tuple(
        existing for existing in stream if existing.policy_revision == record.policy_revision
    )
    if same_revision:
        existing = same_revision[0]
        if (
            existing.policy_digest == record.policy_digest
            and existing.record_id == record.record_id
            and existing.status == record.status
        ):
            return ProductionBackupPolicyAppendPlan(
                status="replayed",
                current_record=current_record,
            )
        raise ProductionBackupAuthorityConflictError(
            "Production backup policy revision already exists with a different payload."
        )
    if record.status not in {"active", "retired"}:
        raise ProductionBackupAuthoritySequenceError(
            "New production backup policy revisions must be active or retired."
        )
    if record.policy_revision != current_record.policy_revision + 1:
        raise ProductionBackupAuthoritySequenceError(
            "Production backup policy revisions must append contiguously."
        )
    if record.supersedes_record_id != current_record.record_id:
        raise ProductionBackupAuthoritySequenceError(
            "Production backup policy supersedes_record_id must match the current record."
        )
    return ProductionBackupPolicyAppendPlan(
        status="written",
        current_record=current_record,
        superseded_current_record=current_record.model_copy(update={"status": "superseded"}),
    )


def plan_production_backup_authority_write(
    *,
    record_store: ProductionBackupAuthorityStore,
    envelope: ProductionBackupAuthorityWriteEnvelope,
) -> ProductionBackupAuthorityWritePlan:
    return plan_production_backup_authority_write_from_records(
        target_records=record_store.list_production_backup_target_records(),
        policy_records=record_store.list_production_backup_policy_records(),
        envelope=envelope,
    )


def plan_production_backup_authority_write_from_records(
    *,
    target_records: tuple[ProductionBackupTargetRecord, ...],
    policy_records: tuple[ProductionBackupPolicyRecord, ...],
    envelope: ProductionBackupAuthorityWriteEnvelope,
) -> ProductionBackupAuthorityWritePlan:
    existing_targets = list(target_records)
    future_targets = list(existing_targets)
    target_plans: list[ProductionBackupTargetAppendPlan] = []
    for target in envelope.targets:
        stream = tuple(record for record in future_targets if record.target_id == target.target_id)
        plan = plan_production_backup_target_append(records=stream, record=target)
        expected_current_id = envelope.expected_current_target_record_ids.get(target.target_id, "")
        current_id = plan.current_record.record_id if plan.current_record is not None else ""
        if expected_current_id != current_id:
            raise ProductionBackupAuthorityConflictError(
                f"Expected current production backup target record does not match '{target.target_id}'."
            )
        target_plans.append(plan)
        if plan.status == "replayed":
            continue
        if plan.superseded_current_record is not None:
            future_targets = [
                plan.superseded_current_record
                if existing.record_id == plan.superseded_current_record.record_id
                else existing
                for existing in future_targets
            ]
        future_targets.append(target)

    matching_policy_records = tuple(
        record
        for record in policy_records
        if record.product == envelope.policy.product
        and record.context == envelope.policy.context
        and record.instance == envelope.policy.instance
        and record.promotion_action == envelope.policy.promotion_action
    )
    policy_plan = plan_production_backup_policy_append(
        records=matching_policy_records,
        record=envelope.policy,
    )
    current_policy_id = (
        policy_plan.current_record.record_id if policy_plan.current_record is not None else ""
    )
    if envelope.expected_current_policy_record_id != current_policy_id:
        raise ProductionBackupAuthorityConflictError(
            "Expected current production backup policy record does not match."
        )
    bound_target_records: tuple[ProductionBackupTargetRecord, ...]
    target_summaries: tuple[ProductionBackupTargetSummary, ...]
    if envelope.policy.status == "active":
        source_target, destination_target = validate_production_backup_policy_binding(
            policy=envelope.policy,
            target_records=tuple(future_targets),
        )
        bound_target_records = (source_target, destination_target)
        target_summaries = (
            _target_summary(source_target),
            _target_summary(destination_target),
        )
    else:
        bound_target_records = ()
        target_summaries = ()
    authority_digest = production_backup_authority_write_digest(
        envelope,
        bound_target_records=bound_target_records,
    )
    if envelope.mode == "apply" and envelope.reviewed_authority_digest != authority_digest:
        raise ProductionBackupAuthorityConflictError(
            "Reviewed production backup authority digest does not match the current request."
        )
    replayed = policy_plan.status == "replayed" and all(
        plan.status == "replayed" for plan in target_plans
    )
    return ProductionBackupAuthorityWritePlan(
        target_plans=tuple(target_plans),
        policy_plan=policy_plan,
        future_target_records=tuple(future_targets),
        result=ProductionBackupAuthorityWriteResult(
            mode=envelope.mode,
            status=("replayed" if replayed else "would_apply"),
            authority_digest=authority_digest,
            policy=_policy_summary(envelope.policy),
            targets=target_summaries,
        ),
    )


def production_backup_authority_write_digest(
    envelope: ProductionBackupAuthorityWriteEnvelope,
    *,
    bound_target_records: tuple[ProductionBackupTargetRecord, ...],
) -> str:
    payload = {
        "schema_version": envelope.schema_version,
        "targets": [
            {
                "target_id": target.target_id,
                "record_id": target.record_id,
                "target_digest": target.target_digest,
            }
            for target in envelope.targets
        ],
        "policy": {
            "policy_id": envelope.policy.policy_id,
            "record_id": envelope.policy.record_id,
            "policy_digest": envelope.policy.policy_digest,
        },
        "bound_targets": [
            {
                "target_id": target.target_id,
                "record_id": target.record_id,
                "target_digest": target.target_digest,
            }
            for target in bound_target_records
        ],
        "expected_current_target_record_ids": dict(
            sorted(envelope.expected_current_target_record_ids.items())
        ),
        "expected_current_policy_record_id": envelope.expected_current_policy_record_id,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def resolve_production_backup_authority(
    *,
    record_store: ProductionBackupAuthorityStore,
    product: str,
    context: str,
    instance: str,
    promotion_action: str,
    generated_at: str,
) -> ProductionBackupAuthorityReadModel:
    normalized_product = product.strip().lower()
    normalized_context = context.strip().lower()
    normalized_instance = instance.strip().lower()
    normalized_action = promotion_action.strip()
    generated = _parse_timestamp(generated_at)
    policy_records = record_store.list_production_backup_policy_records(
        product=normalized_product,
        context_name=normalized_context,
        instance_name=normalized_instance,
        promotion_action=normalized_action,
    )
    if not policy_records:
        return _read_model(
            product=normalized_product,
            context=normalized_context,
            instance=normalized_instance,
            promotion_action=normalized_action,
            state="missing",
            summary="Launchplane has no production backup policy for the exact promotion action.",
            reason_codes=("production_backup_policy_missing",),
            generated_at=generated_at,
        )
    try:
        current_policy = _validate_policy_history(policy_records)
    except (ProductionBackupAuthorityConflictError, ProductionBackupAuthoritySequenceError):
        return _read_model(
            product=normalized_product,
            context=normalized_context,
            instance=normalized_instance,
            promotion_action=normalized_action,
            state="invalid",
            summary="Production backup policy history is invalid or ambiguous.",
            reason_codes=("production_backup_policy_history_invalid",),
            generated_at=generated_at,
        )
    policy_summary = _policy_summary(current_policy)
    if current_policy.status == "retired":
        return _read_model(
            product=normalized_product,
            context=normalized_context,
            instance=normalized_instance,
            promotion_action=normalized_action,
            state="retired",
            summary="The exact production backup policy is retired.",
            reason_codes=("production_backup_policy_retired",),
            policy=policy_summary,
            generated_at=generated_at,
        )
    if current_policy.status != "active":
        return _read_model(
            product=normalized_product,
            context=normalized_context,
            instance=normalized_instance,
            promotion_action=normalized_action,
            state="invalid",
            summary="The current production backup policy is not active.",
            reason_codes=("production_backup_policy_current_status_invalid",),
            policy=policy_summary,
            generated_at=generated_at,
        )
    if _parse_timestamp(current_policy.review_after) <= generated:
        return _read_model(
            product=normalized_product,
            context=normalized_context,
            instance=normalized_instance,
            promotion_action=normalized_action,
            state="stale",
            summary="The exact production backup policy requires operator review.",
            reason_codes=("production_backup_policy_stale",),
            policy=policy_summary,
            generated_at=generated_at,
        )

    target_records: list[ProductionBackupTargetRecord] = []
    for target_id in current_policy.target_ids:
        records = record_store.list_production_backup_target_records(target_id=target_id)
        if not records:
            return _read_model(
                product=normalized_product,
                context=normalized_context,
                instance=normalized_instance,
                promotion_action=normalized_action,
                state="missing",
                summary="A production backup policy target is missing.",
                reason_codes=(f"production_backup_target_missing:{target_id}",),
                policy=policy_summary,
                targets=tuple(_target_summary(record) for record in target_records),
                generated_at=generated_at,
            )
        try:
            current_target = _validate_target_history(records)
        except (ProductionBackupAuthorityConflictError, ProductionBackupAuthoritySequenceError):
            return _read_model(
                product=normalized_product,
                context=normalized_context,
                instance=normalized_instance,
                promotion_action=normalized_action,
                state="invalid",
                summary="A production backup target history is invalid or ambiguous.",
                reason_codes=(f"production_backup_target_history_invalid:{target_id}",),
                policy=policy_summary,
                targets=tuple(_target_summary(record) for record in target_records),
                generated_at=generated_at,
            )
        target_records.append(current_target)
        target_summary = tuple(_target_summary(record) for record in target_records)
        if current_target.status == "retired":
            return _read_model(
                product=normalized_product,
                context=normalized_context,
                instance=normalized_instance,
                promotion_action=normalized_action,
                state="retired",
                summary="A production backup policy target is retired.",
                reason_codes=(f"production_backup_target_retired:{target_id}",),
                policy=policy_summary,
                targets=target_summary,
                generated_at=generated_at,
            )
        if current_target.status != "active":
            return _read_model(
                product=normalized_product,
                context=normalized_context,
                instance=normalized_instance,
                promotion_action=normalized_action,
                state="invalid",
                summary="A current production backup target is not active.",
                reason_codes=(f"production_backup_target_current_status_invalid:{target_id}",),
                policy=policy_summary,
                targets=target_summary,
                generated_at=generated_at,
            )
        if _parse_timestamp(current_target.review_after) <= generated:
            return _read_model(
                product=normalized_product,
                context=normalized_context,
                instance=normalized_instance,
                promotion_action=normalized_action,
                state="stale",
                summary="A production backup policy target requires operator review.",
                reason_codes=(f"production_backup_target_stale:{target_id}",),
                policy=policy_summary,
                targets=target_summary,
                generated_at=generated_at,
            )

    source_target, destination_target = target_records
    if source_target.destination_kind != "proxmox_guest":
        return _invalid_target_binding(
            current_policy=current_policy,
            targets=target_records,
            generated_at=generated_at,
            reason_code="production_backup_source_target_kind_invalid",
        )
    if destination_target.destination_kind != "proxmox_storage":
        return _invalid_target_binding(
            current_policy=current_policy,
            targets=target_records,
            generated_at=generated_at,
            reason_code="production_backup_destination_target_kind_invalid",
        )
    if source_target.provider_type != destination_target.provider_type:
        return _invalid_target_binding(
            current_policy=current_policy,
            targets=target_records,
            generated_at=generated_at,
            reason_code="production_backup_target_provider_mismatch",
        )
    source_destination = source_target.destination
    backup_destination = destination_target.destination
    if (
        source_destination.host != backup_destination.host
        or source_destination.username != backup_destination.username
    ):
        return _invalid_target_binding(
            current_policy=current_policy,
            targets=target_records,
            generated_at=generated_at,
            reason_code="production_backup_target_endpoint_mismatch",
        )
    return _read_model(
        product=normalized_product,
        context=normalized_context,
        instance=normalized_instance,
        promotion_action=normalized_action,
        state="ready",
        summary="The exact production promotion action has current snapshot and independent-backup authority.",
        policy=policy_summary,
        targets=tuple(_target_summary(record) for record in target_records),
        generated_at=generated_at,
    )


def validate_production_backup_policy_binding(
    *,
    policy: ProductionBackupPolicyRecord,
    target_records: tuple[ProductionBackupTargetRecord, ...],
) -> tuple[ProductionBackupTargetRecord, ProductionBackupTargetRecord]:
    resolved: list[ProductionBackupTargetRecord] = []
    for target_id in policy.target_ids:
        stream = tuple(record for record in target_records if record.target_id == target_id)
        if not stream:
            raise ProductionBackupAuthorityConflictError(
                f"Production backup policy target '{target_id}' does not exist."
            )
        current = _validate_target_history(stream)
        if current.status != "active":
            raise ProductionBackupAuthorityConflictError(
                f"Production backup policy target '{target_id}' is not active."
            )
        resolved.append(current)
    source_target, destination_target = resolved
    if source_target.destination_kind != "proxmox_guest":
        raise ProductionBackupAuthorityConflictError(
            "Production backup policy source target must be a Proxmox guest."
        )
    if destination_target.destination_kind != "proxmox_storage":
        raise ProductionBackupAuthorityConflictError(
            "Production backup policy destination target must be Proxmox storage."
        )
    if source_target.provider_type != destination_target.provider_type:
        raise ProductionBackupAuthorityConflictError(
            "Production backup policy targets must use the same provider type."
        )
    source_destination = source_target.destination
    backup_destination = destination_target.destination
    if (
        source_destination.host != backup_destination.host
        or source_destination.username != backup_destination.username
    ):
        raise ProductionBackupAuthorityConflictError(
            "Production backup policy targets must use the same provider endpoint."
        )
    return source_target, destination_target


def _validate_target_history(
    records: tuple[ProductionBackupTargetRecord, ...],
) -> ProductionBackupTargetRecord:
    if not records:
        raise ProductionBackupAuthoritySequenceError("Production backup target history is empty.")
    ordered = sorted(records, key=lambda record: record.target_revision)
    target_ids = {record.target_id for record in ordered}
    if len(target_ids) != 1:
        raise ProductionBackupAuthorityConflictError(
            "Production backup target history spans multiple target IDs."
        )
    if len({record.target_revision for record in ordered}) != len(ordered):
        raise ProductionBackupAuthorityConflictError(
            "Production backup target history contains duplicate revisions."
        )
    provider_types = {record.provider_type for record in ordered}
    destination_kinds = {record.destination_kind for record in ordered}
    if len(provider_types) != 1 or len(destination_kinds) != 1:
        raise ProductionBackupAuthorityConflictError(
            "Production backup target provider and destination kind must remain immutable."
        )
    previous: ProductionBackupTargetRecord | None = None
    for index, record in enumerate(ordered, start=1):
        if record.target_revision != index:
            raise ProductionBackupAuthoritySequenceError(
                "Production backup target history must be contiguous from revision 1."
            )
        if previous is None:
            if record.supersedes_record_id:
                raise ProductionBackupAuthoritySequenceError(
                    "First production backup target record cannot supersede another record."
                )
        elif record.supersedes_record_id != previous.record_id:
            raise ProductionBackupAuthoritySequenceError(
                "Production backup target history has an invalid supersession chain."
            )
        if record is not ordered[-1] and record.status != "superseded":
            raise ProductionBackupAuthorityConflictError(
                "Historical production backup target records must be superseded."
            )
        previous = record
    current = ordered[-1]
    if current.status not in {"active", "retired"}:
        raise ProductionBackupAuthorityConflictError(
            "Current production backup target record must be active or retired."
        )
    return current


def _validate_policy_history(
    records: tuple[ProductionBackupPolicyRecord, ...],
) -> ProductionBackupPolicyRecord:
    if not records:
        raise ProductionBackupAuthoritySequenceError("Production backup policy history is empty.")
    ordered = sorted(records, key=lambda record: record.policy_revision)
    policy_ids = {record.policy_id for record in ordered}
    if len(policy_ids) != 1:
        raise ProductionBackupAuthorityConflictError(
            "Production backup policy history spans multiple policy IDs."
        )
    if len({record.policy_revision for record in ordered}) != len(ordered):
        raise ProductionBackupAuthorityConflictError(
            "Production backup policy history contains duplicate revisions."
        )
    first = ordered[0]
    if any(not _same_policy_scope(first, record) for record in ordered[1:]):
        raise ProductionBackupAuthorityConflictError(
            "Production backup policy history changes exact scope."
        )
    previous: ProductionBackupPolicyRecord | None = None
    for index, record in enumerate(ordered, start=1):
        if record.policy_revision != index:
            raise ProductionBackupAuthoritySequenceError(
                "Production backup policy history must be contiguous from revision 1."
            )
        if previous is None:
            if record.supersedes_record_id:
                raise ProductionBackupAuthoritySequenceError(
                    "First production backup policy record cannot supersede another record."
                )
        elif record.supersedes_record_id != previous.record_id:
            raise ProductionBackupAuthoritySequenceError(
                "Production backup policy history has an invalid supersession chain."
            )
        if record is not ordered[-1] and record.status != "superseded":
            raise ProductionBackupAuthorityConflictError(
                "Historical production backup policy records must be superseded."
            )
        previous = record
    current = ordered[-1]
    if current.status not in {"active", "retired"}:
        raise ProductionBackupAuthorityConflictError(
            "Current production backup policy record must be active or retired."
        )
    return current


def _same_policy_scope(
    left: ProductionBackupPolicyRecord, right: ProductionBackupPolicyRecord
) -> bool:
    return (
        left.policy_id == right.policy_id
        and left.product == right.product
        and left.context == right.context
        and left.instance == right.instance
        and left.promotion_action == right.promotion_action
    )


def _target_summary(record: ProductionBackupTargetRecord) -> ProductionBackupTargetSummary:
    return ProductionBackupTargetSummary(
        target_id=record.target_id,
        record_id=record.record_id,
        target_revision=record.target_revision,
        status=record.status,
        provider_type=record.provider_type,
        destination_kind=record.destination_kind,
        effective_at=record.effective_at,
        review_after=record.review_after,
    )


def _policy_summary(record: ProductionBackupPolicyRecord) -> ProductionBackupPolicySummary:
    return ProductionBackupPolicySummary(
        policy_id=record.policy_id,
        record_id=record.record_id,
        policy_revision=record.policy_revision,
        status=record.status,
        promotion_action=record.promotion_action,
        source_target_id=record.fast_snapshot.source_target_id,
        destination_target_id=record.independent_backup.destination_target_id,
        effective_at=record.effective_at,
        review_after=record.review_after,
    )


def _invalid_target_binding(
    *,
    current_policy: ProductionBackupPolicyRecord,
    targets: list[ProductionBackupTargetRecord],
    generated_at: str,
    reason_code: str,
) -> ProductionBackupAuthorityReadModel:
    return _read_model(
        product=current_policy.product,
        context=current_policy.context,
        instance=current_policy.instance,
        promotion_action=current_policy.promotion_action,
        state="invalid",
        summary="The production backup policy target binding is invalid.",
        reason_codes=(reason_code,),
        policy=_policy_summary(current_policy),
        targets=tuple(_target_summary(record) for record in targets),
        generated_at=generated_at,
    )


def _read_model(
    *,
    product: str,
    context: str,
    instance: str,
    promotion_action: str,
    state: Literal["ready", "missing", "invalid", "stale", "retired"],
    summary: str,
    generated_at: str,
    reason_codes: tuple[str, ...] = (),
    policy: ProductionBackupPolicySummary | None = None,
    targets: tuple[ProductionBackupTargetSummary, ...] = (),
) -> ProductionBackupAuthorityReadModel:
    return ProductionBackupAuthorityReadModel(
        product=product,
        context=context,
        instance=instance,
        promotion_action=promotion_action,
        state=state,
        ready=state == "ready",
        summary=summary,
        reason_codes=reason_codes,
        policy=policy,
        targets=targets,
        generated_at=generated_at,
    )


def _parse_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("production backup generated_at must be an ISO-8601 timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("production backup generated_at must be timezone-aware")
    return parsed
