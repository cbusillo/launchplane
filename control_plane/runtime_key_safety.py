from __future__ import annotations

from collections.abc import Iterable
from fnmatch import fnmatchcase
from typing import Protocol

from control_plane.contracts.runtime_key_safety_policy import (
    RuntimeEnvironmentClass,
    RuntimeKeySafetyEvaluation,
    RuntimeKeySafetyPolicyRecord,
    RuntimeKeySafetyFinding,
    RuntimeKeySafetyTarget,
    RuntimeSecretClass,
    RuntimeSecretSafetyRule,
    RuntimeSecretSafetyTargetScope,
)
from control_plane.contracts.secret_record import SecretBinding


ALLOWED_SECRET_CLASSES_BY_ENVIRONMENT: dict[RuntimeEnvironmentClass, set[RuntimeSecretClass]] = {
    "prod": {"prod_only", "shared_safe"},
    "testing": {"testing", "non_prod", "shared_safe"},
    "preview": {"preview", "non_prod", "shared_safe"},
    "dev": {"non_prod", "shared_safe"},
    "unknown": set(),
}


def runtime_key_safety_environment_class(instance_name: str) -> RuntimeEnvironmentClass:
    normalized_instance = instance_name.strip().lower()
    if normalized_instance in {"prod", "production"}:
        return "prod"
    if normalized_instance in {"testing", "test", "staging", "stage"}:
        return "testing"
    if normalized_instance in {"preview", "pr"} or normalized_instance.startswith("pr-"):
        return "preview"
    if normalized_instance in {"dev", "local", "development"}:
        return "dev"
    return "unknown"


class RuntimeKeySafetyPolicyReadStore(Protocol):
    def list_runtime_key_safety_policy_records(
        self,
        *,
        status: str = "",
        limit: int | None = None,
    ) -> tuple[RuntimeKeySafetyPolicyRecord, ...]: ...

    def list_secret_bindings(
        self,
        *,
        integration: str = "",
        context_name: str = "",
        instance_name: str = "",
        limit: int | None = None,
    ) -> tuple[SecretBinding, ...]: ...


def latest_active_runtime_key_safety_policy(
    record_store: RuntimeKeySafetyPolicyReadStore,
) -> RuntimeKeySafetyPolicyRecord:
    records = record_store.list_runtime_key_safety_policy_records(status="active", limit=1)
    if not records:
        raise ValueError("No active runtime key-safety policy record found.")
    return records[0]


def evaluate_runtime_key_safety_from_store(
    *,
    record_store: RuntimeKeySafetyPolicyReadStore,
    target: RuntimeKeySafetyTarget,
    required_binding_keys: Iterable[str],
    policy_record: RuntimeKeySafetyPolicyRecord | None = None,
) -> RuntimeKeySafetyEvaluation:
    policy = policy_record or latest_active_runtime_key_safety_policy(record_store)
    return evaluate_runtime_key_safety(
        target=target,
        required_binding_keys=required_binding_keys,
        secret_bindings=record_store.list_secret_bindings(
            integration="runtime_environment",
            limit=None,
        ),
        secret_rules=policy.rules,
    )


def evaluate_runtime_key_safety(
    *,
    target: RuntimeKeySafetyTarget,
    required_binding_keys: Iterable[str],
    secret_bindings: Iterable[SecretBinding],
    secret_rules: Iterable[RuntimeSecretSafetyRule],
) -> RuntimeKeySafetyEvaluation:
    checked_binding_keys = _normalize_required_binding_keys(required_binding_keys)
    rules_by_binding_key = _rules_by_binding_key(secret_rules)
    bindings_by_binding_key = _bindings_by_binding_key(secret_bindings)
    findings: list[RuntimeKeySafetyFinding] = []

    if target.environment_class == "unknown":
        findings.append(
            RuntimeKeySafetyFinding(
                code="unknown_environment_class",
                detail="Runtime key safety target has unknown environment class.",
            )
        )

    for binding_key in checked_binding_keys:
        bindings = bindings_by_binding_key.get(binding_key, ())
        if not bindings:
            findings.append(
                RuntimeKeySafetyFinding(
                    code="binding_missing",
                    binding_key=binding_key,
                    detail=f"Required managed secret binding {binding_key!r} is missing.",
                )
            )
            continue
        effective_bindings = _effective_bindings_for_target(bindings, target=target)
        if not effective_bindings:
            findings.append(
                RuntimeKeySafetyFinding(
                    code="binding_missing",
                    binding_key=binding_key,
                    detail=f"Required managed secret binding {binding_key!r} is missing.",
                )
            )
            continue
        if len(effective_bindings) > 1:
            findings.append(
                RuntimeKeySafetyFinding(
                    code="ambiguous_binding",
                    binding_key=binding_key,
                    detail=f"Required managed secret binding {binding_key!r} resolved to multiple records.",
                )
            )
            continue

        binding = effective_bindings[0]
        if binding.status != "configured":
            findings.append(
                RuntimeKeySafetyFinding(
                    code="binding_disabled",
                    binding_key=binding.binding_key,
                    binding_id=binding.binding_id,
                    secret_id=binding.secret_id,
                    detail=f"Managed secret binding {binding.binding_key!r} is not configured.",
                )
            )
            continue

        rule = rules_by_binding_key.get(binding.binding_key)
        if rule is None:
            findings.append(
                RuntimeKeySafetyFinding(
                    code="unclassified_binding",
                    binding_key=binding.binding_key,
                    binding_id=binding.binding_id,
                    secret_id=binding.secret_id,
                    detail=f"Managed secret binding {binding.binding_key!r} has no runtime key safety rule.",
                )
            )
            continue

        findings.extend(_evaluate_binding_rule(target=target, binding=binding, rule=rule))

    return RuntimeKeySafetyEvaluation(
        status="fail" if findings else "pass",
        target=target,
        checked_binding_keys=checked_binding_keys,
        findings=tuple(findings),
    )


def _normalize_required_binding_keys(required_binding_keys: Iterable[str]) -> tuple[str, ...]:
    normalized: list[str] = []
    for raw_key in required_binding_keys:
        binding_key = raw_key.strip()
        if not binding_key:
            raise ValueError("runtime key safety required binding keys must be non-empty")
        if binding_key not in normalized:
            normalized.append(binding_key)
    if not normalized:
        raise ValueError("runtime key safety requires at least one binding key")
    return tuple(normalized)


def _rules_by_binding_key(
    secret_rules: Iterable[RuntimeSecretSafetyRule],
) -> dict[str, RuntimeSecretSafetyRule]:
    rules_by_binding_key: dict[str, RuntimeSecretSafetyRule] = {}
    for rule in secret_rules:
        rules_by_binding_key[rule.binding_key] = rule
    return rules_by_binding_key


def _bindings_by_binding_key(
    secret_bindings: Iterable[SecretBinding],
) -> dict[str, tuple[SecretBinding, ...]]:
    grouped: dict[str, list[SecretBinding]] = {}
    for binding in secret_bindings:
        grouped.setdefault(binding.binding_key, []).append(binding)
    return {key: tuple(bindings) for key, bindings in grouped.items()}


def _effective_bindings_for_target(
    bindings: tuple[SecretBinding, ...], *, target: RuntimeKeySafetyTarget
) -> tuple[SecretBinding, ...]:
    ranked_bindings = tuple(
        (binding, _binding_route_rank(binding=binding, target=target)) for binding in bindings
    )
    highest_rank = max(rank for _, rank in ranked_bindings)
    if highest_rank == 0:
        return ()
    return tuple(
        binding
        for binding, rank in ranked_bindings
        if rank == highest_rank
    )


def _binding_route_rank(*, binding: SecretBinding, target: RuntimeKeySafetyTarget) -> int:
    if not binding.context and not binding.instance:
        return 1
    if binding.context == target.context and binding.instance == target.instance:
        return 3
    if binding.context == target.context and not binding.instance:
        return 2
    return 0


def _evaluate_binding_rule(
    *,
    target: RuntimeKeySafetyTarget,
    binding: SecretBinding,
    rule: RuntimeSecretSafetyRule,
) -> tuple[RuntimeKeySafetyFinding, ...]:
    findings: list[RuntimeKeySafetyFinding] = []
    allowed_secret_classes = ALLOWED_SECRET_CLASSES_BY_ENVIRONMENT[target.environment_class]
    if rule.secret_class not in allowed_secret_classes:
        findings.append(
            RuntimeKeySafetyFinding(
                code="secret_class_not_allowed",
                binding_key=binding.binding_key,
                binding_id=binding.binding_id,
                secret_id=binding.secret_id,
                secret_class=rule.secret_class,
                detail=(
                    f"Managed secret binding {binding.binding_key!r} is classified as "
                    f"{rule.secret_class!r}, which is not allowed for "
                    f"{target.environment_class!r} environments."
                ),
            )
        )
    target_allowed = _target_allowed(target=target, rule=rule)
    context_allowed = _context_allowed(target=target, rule=rule)
    if not target_allowed and not context_allowed:
        findings.append(
            RuntimeKeySafetyFinding(
                code="context_not_allowed",
                binding_key=binding.binding_key,
                binding_id=binding.binding_id,
                secret_id=binding.secret_id,
                secret_class=rule.secret_class,
                detail=(
                    f"Managed secret binding {binding.binding_key!r} is not allowed "
                    f"for context {target.context!r}."
                ),
            )
        )
    if not target_allowed and not _instance_allowed_for_diagnostics(
        target=target, rule=rule
    ):
        findings.append(
            RuntimeKeySafetyFinding(
                code="instance_not_allowed",
                binding_key=binding.binding_key,
                binding_id=binding.binding_id,
                secret_id=binding.secret_id,
                secret_class=rule.secret_class,
                detail=(
                    f"Managed secret binding {binding.binding_key!r} is not allowed "
                    f"for instance {target.instance!r}."
                ),
            )
        )
    return tuple(findings)


def _target_allowed(*, target: RuntimeKeySafetyTarget, rule: RuntimeSecretSafetyRule) -> bool:
    legacy_restricted = bool(
        rule.allowed_contexts or rule.allowed_instances or rule.allowed_instance_patterns
    )
    paired_restricted = bool(rule.allowed_targets)
    if not legacy_restricted and not paired_restricted:
        return True
    if legacy_restricted and _legacy_scope_allowed(target=target, rule=rule):
        return True
    return any(
        _target_scope_allowed(target=target, scope=scope)
        for scope in rule.allowed_targets
    )


def _context_allowed(*, target: RuntimeKeySafetyTarget, rule: RuntimeSecretSafetyRule) -> bool:
    legacy_restricted = bool(
        rule.allowed_contexts or rule.allowed_instances or rule.allowed_instance_patterns
    )
    if legacy_restricted and (
        not rule.allowed_contexts or target.context in rule.allowed_contexts
    ):
        return True
    return any(scope.context == target.context for scope in rule.allowed_targets)


def _instance_allowed_for_diagnostics(
    *, target: RuntimeKeySafetyTarget, rule: RuntimeSecretSafetyRule
) -> bool:
    matching_target_scopes = tuple(
        scope for scope in rule.allowed_targets if scope.context == target.context
    )
    if matching_target_scopes:
        return any(
            not scope.instances
            and not scope.instance_patterns
            or _instance_allowed(
                instance=target.instance,
                instances=scope.instances,
                instance_patterns=scope.instance_patterns,
            )
            for scope in matching_target_scopes
        )
    if rule.allowed_instances or rule.allowed_instance_patterns:
        return _legacy_instance_allowed(target=target, rule=rule)
    return True


def _legacy_scope_allowed(
    *, target: RuntimeKeySafetyTarget, rule: RuntimeSecretSafetyRule
) -> bool:
    return _legacy_context_allowed(target=target, rule=rule) and _legacy_instance_allowed(
        target=target, rule=rule
    )


def _legacy_context_allowed(
    *, target: RuntimeKeySafetyTarget, rule: RuntimeSecretSafetyRule
) -> bool:
    return not rule.allowed_contexts or target.context in rule.allowed_contexts


def _legacy_instance_allowed(
    *, target: RuntimeKeySafetyTarget, rule: RuntimeSecretSafetyRule
) -> bool:
    if not rule.allowed_instances and not rule.allowed_instance_patterns:
        return True
    return _instance_allowed(
        instance=target.instance,
        instances=rule.allowed_instances,
        instance_patterns=rule.allowed_instance_patterns,
    )


def _target_scope_allowed(
    *,
    target: RuntimeKeySafetyTarget,
    scope: RuntimeSecretSafetyTargetScope,
) -> bool:
    if scope.context != target.context:
        return False
    if not scope.instances and not scope.instance_patterns:
        return True
    return _instance_allowed(
        instance=target.instance,
        instances=scope.instances,
        instance_patterns=scope.instance_patterns,
    )


def _instance_allowed(
    *,
    instance: str,
    instances: tuple[str, ...],
    instance_patterns: tuple[str, ...],
) -> bool:
    if instance in instances:
        return True
    if any(character in instance for character in ("/", "\\")):
        return False
    return any(
        fnmatchcase(instance, pattern)
        for pattern in instance_patterns
    )
