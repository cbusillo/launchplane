from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from control_plane.contracts.runtime_key_safety_policy import (
    RuntimeKeySafetyPolicyRecord,
    RuntimeSecretSafetyRule,
)


TimestampProvider = Callable[[], str]
RecordSlugProvider = Callable[[str], str]


class RuntimeKeySafetyPolicyStore(Protocol):
    def list_runtime_key_safety_policy_records(
        self,
        *,
        status: str = "",
        limit: int | None = None,
    ) -> tuple[RuntimeKeySafetyPolicyRecord, ...]: ...

    def write_runtime_key_safety_policy_record(
        self, record: RuntimeKeySafetyPolicyRecord
    ) -> None: ...


def summarize_runtime_key_safety_policy_record(
    record: RuntimeKeySafetyPolicyRecord,
) -> dict[str, object]:
    return {
        "record_id": record.record_id,
        "status": record.status,
        "source": record.source,
        "updated_at": record.updated_at,
        "policy_sha256": record.policy_sha256,
        "rule_count": len(record.rules),
        "binding_keys": [rule.binding_key for rule in record.rules],
    }


def write_runtime_key_safety_policy(
    *,
    record_store: RuntimeKeySafetyPolicyStore,
    rules: tuple[RuntimeSecretSafetyRule, ...],
    source_label: str,
    now_timestamp: TimestampProvider,
    record_slug: RecordSlugProvider,
) -> tuple[RuntimeKeySafetyPolicyRecord, bool]:
    existing_records = record_store.list_runtime_key_safety_policy_records(status="active", limit=1)
    existing_record = existing_records[0] if existing_records else None
    merged_rules = merge_runtime_key_safety_rules(
        existing_record.rules if existing_record is not None else (), rules
    )
    updated_at = now_timestamp()
    pending_record = RuntimeKeySafetyPolicyRecord(
        record_id="runtime-key-safety-policy-pending",
        status="active",
        source=source_label,
        updated_at=updated_at,
        rules=merged_rules,
    )
    if (
        existing_record is not None
        and existing_record.policy_sha256 == pending_record.policy_sha256
    ):
        return existing_record, False
    record = pending_record.model_copy(
        update={
            "record_id": "runtime-key-safety-policy-"
            f"{record_slug(updated_at)}-{pending_record.policy_sha256[:12]}",
        }
    )
    record_store.write_runtime_key_safety_policy_record(record)
    return record, True


def merge_runtime_key_safety_rules(
    existing_rules: tuple[RuntimeSecretSafetyRule, ...],
    requested_rules: tuple[RuntimeSecretSafetyRule, ...],
) -> tuple[RuntimeSecretSafetyRule, ...]:
    rules_by_binding_key = {rule.binding_key: rule for rule in existing_rules}
    for requested_rule in requested_rules:
        existing_rule = rules_by_binding_key.get(requested_rule.binding_key)
        if existing_rule is None or existing_rule.secret_class != requested_rule.secret_class:
            rules_by_binding_key[requested_rule.binding_key] = requested_rule
            continue
        rules_by_binding_key[requested_rule.binding_key] = requested_rule.model_copy(
            update={
                "allowed_contexts": merge_runtime_key_safety_scope_values(
                    existing_rule.allowed_contexts, requested_rule.allowed_contexts
                ),
                "allowed_instances": merge_runtime_key_safety_scope_values(
                    existing_rule.allowed_instances, requested_rule.allowed_instances
                ),
                "description": requested_rule.description or existing_rule.description,
            }
        )
    return tuple(rules_by_binding_key[key] for key in sorted(rules_by_binding_key))


def merge_runtime_key_safety_scope_values(
    existing_values: tuple[str, ...], requested_values: tuple[str, ...]
) -> tuple[str, ...]:
    if not existing_values or not requested_values:
        return ()
    return tuple(sorted({*existing_values, *requested_values}))
