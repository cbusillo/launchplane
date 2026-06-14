from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from control_plane.contracts.runtime_key_safety_policy import (
    RuntimeKeySafetyPolicyRecord,
    RuntimeSecretSafetyRule,
    RuntimeSecretSafetyTargetScope,
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
    for raw_requested_rule in requested_rules:
        requested_rule = normalize_runtime_key_safety_requested_rule(raw_requested_rule)
        existing_rule = rules_by_binding_key.get(requested_rule.binding_key)
        if existing_rule is None or existing_rule.secret_class != requested_rule.secret_class:
            rules_by_binding_key[requested_rule.binding_key] = requested_rule
            continue
        rules_by_binding_key[requested_rule.binding_key] = requested_rule.model_copy(
            update={
                "allowed_contexts": merge_runtime_key_safety_legacy_context_scope(
                    existing_rule, requested_rule
                ),
                **(
                    merge_runtime_key_safety_instance_scope(existing_rule, requested_rule)
                    if _has_legacy_scope(requested_rule)
                    else {
                        "allowed_instances": existing_rule.allowed_instances,
                        "allowed_instance_patterns": existing_rule.allowed_instance_patterns,
                    }
                ),
                "allowed_targets": merge_runtime_key_safety_target_scopes(
                    existing_rule.allowed_targets, requested_rule.allowed_targets
                ),
                "description": requested_rule.description or existing_rule.description,
            }
        )
    return tuple(rules_by_binding_key[key] for key in sorted(rules_by_binding_key))


def normalize_runtime_key_safety_requested_rule(
    rule: RuntimeSecretSafetyRule,
) -> RuntimeSecretSafetyRule:
    if not rule.allowed_instance_patterns:
        return rule
    if rule.allowed_targets:
        raise ValueError(
            "runtime key-safety policy requests cannot mix legacy "
            "allowed_instance_patterns with paired allowed_targets"
        )
    if not rule.allowed_contexts:
        raise ValueError(
            "runtime key-safety policy requests with allowed_instance_patterns "
            "must use paired allowed_targets or exact allowed_contexts"
        )
    # New writes must not persist preview patterns on legacy axes; paired target
    # scopes keep context and dynamic instance grants from forming a cross-product.
    return rule.model_copy(
        update={
            "allowed_contexts": (),
            "allowed_instances": (),
            "allowed_instance_patterns": (),
            "allowed_targets": tuple(
                RuntimeSecretSafetyTargetScope(
                    context=context,
                    instances=rule.allowed_instances,
                    instance_patterns=rule.allowed_instance_patterns,
                )
                for context in rule.allowed_contexts
            ),
        }
    )


def merge_runtime_key_safety_scope_values(
    existing_values: tuple[str, ...], requested_values: tuple[str, ...]
) -> tuple[str, ...]:
    if not existing_values or not requested_values:
        return ()
    return tuple(sorted({*existing_values, *requested_values}))


def merge_runtime_key_safety_legacy_context_scope(
    existing_rule: RuntimeSecretSafetyRule,
    requested_rule: RuntimeSecretSafetyRule,
) -> tuple[str, ...]:
    if not _has_legacy_scope(requested_rule):
        return existing_rule.allowed_contexts
    if not _has_legacy_scope(existing_rule):
        return requested_rule.allowed_contexts
    return merge_runtime_key_safety_scope_values(
        existing_rule.allowed_contexts, requested_rule.allowed_contexts
    )


def merge_runtime_key_safety_target_scopes(
    existing_scopes: tuple[RuntimeSecretSafetyTargetScope, ...],
    requested_scopes: tuple[RuntimeSecretSafetyTargetScope, ...],
) -> tuple[RuntimeSecretSafetyTargetScope, ...]:
    scopes_by_context = {scope.context: scope for scope in existing_scopes}
    for requested_scope in requested_scopes:
        existing_scope = scopes_by_context.get(requested_scope.context)
        if existing_scope is None:
            scopes_by_context[requested_scope.context] = requested_scope
            continue
        existing_restricted = bool(
            existing_scope.instances or existing_scope.instance_patterns
        )
        requested_restricted = bool(
            requested_scope.instances or requested_scope.instance_patterns
        )
        if not existing_restricted or not requested_restricted:
            scopes_by_context[requested_scope.context] = requested_scope.model_copy(
                update={"instances": (), "instance_patterns": ()}
            )
            continue
        scopes_by_context[requested_scope.context] = requested_scope.model_copy(
            update={
                "instances": tuple(
                    sorted({*existing_scope.instances, *requested_scope.instances})
                ),
                "instance_patterns": tuple(
                    sorted(
                        {
                            *existing_scope.instance_patterns,
                            *requested_scope.instance_patterns,
                        }
                    )
                ),
            }
        )
    return tuple(scopes_by_context[key] for key in sorted(scopes_by_context))


def _has_legacy_scope(rule: RuntimeSecretSafetyRule) -> bool:
    return bool(
        rule.allowed_contexts or rule.allowed_instances or rule.allowed_instance_patterns
    )


def merge_runtime_key_safety_instance_scope(
    existing_rule: RuntimeSecretSafetyRule,
    requested_rule: RuntimeSecretSafetyRule,
) -> dict[str, tuple[str, ...]]:
    if not _has_legacy_scope(existing_rule):
        return {
            "allowed_instances": requested_rule.allowed_instances,
            "allowed_instance_patterns": requested_rule.allowed_instance_patterns,
        }
    existing_restricted = bool(
        existing_rule.allowed_instances or existing_rule.allowed_instance_patterns
    )
    requested_restricted = bool(
        requested_rule.allowed_instances or requested_rule.allowed_instance_patterns
    )
    if not existing_restricted or not requested_restricted:
        return {"allowed_instances": (), "allowed_instance_patterns": ()}
    return {
        "allowed_instances": tuple(
            sorted({*existing_rule.allowed_instances, *requested_rule.allowed_instances})
        ),
        "allowed_instance_patterns": tuple(
            sorted(
                {
                    *existing_rule.allowed_instance_patterns,
                    *requested_rule.allowed_instance_patterns,
                }
            )
        ),
    }
