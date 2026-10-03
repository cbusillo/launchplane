from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from fnmatch import fnmatchcase
from typing import Protocol
from urllib.parse import urlsplit

from control_plane.contracts.runtime_key_safety_policy import (
    RuntimeEnvironmentClass,
    RuntimeKeySafetyEvaluation,
    RuntimeKeySafetyPolicyRecord,
    RuntimeKeySafetyFinding,
    RuntimeKeySafetyTarget,
    RuntimeSecretClass,
    RuntimeSecretSafetyRule,
    RuntimeSecretSafetyTargetScope,
    UnreasonedSharedIntegrationKeyMode,
)
from control_plane.contracts.secret_record import SecretBinding


ALLOWED_SECRET_CLASSES_BY_ENVIRONMENT: dict[RuntimeEnvironmentClass, set[RuntimeSecretClass]] = {
    "prod": {"prod_only", "shared_safe"},
    "testing": {"testing", "non_prod", "shared_safe"},
    "preview": {"preview", "non_prod", "shared_safe"},
    "dev": {"non_prod", "shared_safe"},
    "unknown": set(),
}
SECRET_SHAPED_RUNTIME_KEY_PARTS = frozenset({"PASSWORD", "TOKEN", "SECRET", "KEY"})
# Key-name markers for production integration credentials: stores, payments,
# outgoing mail, printing and common business-system connectors. A binding whose
# key carries one of these never takes its classification from a non-production
# lane. The active policy record can add product-specific markers.
DEFAULT_INTEGRATION_KEY_MARKERS = (
    "SHOPIFY",
    "STRIPE",
    "PAYPAL",
    "SQUARE",
    "BRAINTREE",
    "AUTHORIZE_NET",
    "PAYMENT",
    "SMTP",
    "MAIL",
    "SENDGRID",
    "MAILGUN",
    "POSTMARK",
    "RESEND",
    "PRINTNODE",
    "REPAIRSHOPR",
    "FISHBOWL",
)


def is_secret_shaped_runtime_key(key_name: str) -> bool:
    return any(
        key_part in SECRET_SHAPED_RUNTIME_KEY_PARTS for key_part in key_name.upper().split("_")
    )


# A keyword connection string's password, as in libpq ``host=db password=...``
# or ODBC ``Server=db;Pwd=...``.
_KEYWORD_PASSWORD_PATTERN = re.compile(r"(?:^|[\s;])(?:password|pwd)\s*=\s*[^\s;]", re.IGNORECASE)


def runtime_value_carries_credentials(value: str) -> bool:
    """Whether a value embeds a password, as in ``smtp://user:password@host``."""
    if _KEYWORD_PASSWORD_PATTERN.search(value):
        return True
    try:
        return bool(urlsplit(value.strip()).password)
    except ValueError:
        return False


def is_credential_runtime_value(key_name: str, value: str) -> bool:
    return bool(value.strip()) and (
        is_secret_shaped_runtime_key(key_name.strip()) or runtime_value_carries_credentials(value)
    )


def is_integration_runtime_key(key_name: str, *, extra_markers: Iterable[str] = ()) -> bool:
    key_parts = _key_parts(key_name)
    for marker in (*DEFAULT_INTEGRATION_KEY_MARKERS, *extra_markers):
        marker_parts = _key_parts(marker)
        if not marker_parts:
            continue
        width = len(marker_parts)
        if any(
            key_parts[index : index + width] == marker_parts
            for index in range(len(key_parts) - width + 1)
        ):
            return True
    return False


def _key_parts(key_name: str) -> tuple[str, ...]:
    return tuple(part for part in key_name.upper().replace(".", "_").split("_") if part)


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


def preview_template_runtime_bindings(
    *,
    record_store: RuntimeKeySafetyPolicyReadStore,
    template_context: str,
    template_instance: str,
) -> tuple[SecretBinding, ...]:
    """The managed runtime bindings that deliver values to a preview's template lane.

    A lane receives its own bindings and, per key, falls back to the site's
    context-scoped binding, so both are read and the delivered one is kept.
    """
    template_target = RuntimeKeySafetyTarget(
        context=template_context,
        instance=template_instance,
        environment_class=runtime_key_safety_environment_class(template_instance),
    )
    candidates = tuple(
        binding
        for binding in record_store.list_secret_bindings(
            integration="runtime_environment", context_name=template_context, limit=None
        )
        if runtime_secret_binding_matches_target(binding=binding, target=template_target)
    )
    return tuple(
        binding
        for key_bindings in _bindings_by_binding_key(candidates).values()
        for binding in _effective_bindings_for_target(key_bindings, target=template_target)
    )


def evaluate_preview_copied_runtime_key_safety(
    *,
    template_bindings: tuple[SecretBinding, ...],
    policy_record: RuntimeKeySafetyPolicyRecord,
    preview_context: str,
    preview_slug: str,
    copied_keys: Iterable[str],
) -> RuntimeKeySafetyEvaluation:
    """Check template-lane values a preview copies as if they were stored for the preview.

    The template's bindings are retargeted to the preview, so each copied key needs a
    policy rule that allows previews; the template lane's own classification and
    declared classes never carry over. A copied value with no managed binding fails.
    """
    return evaluate_runtime_key_safety(
        target=RuntimeKeySafetyTarget(
            context=preview_context,
            instance=preview_slug,
            environment_class="preview",
        ),
        required_binding_keys=copied_keys,
        secret_bindings=tuple(
            binding.model_copy(update={"context": preview_context, "instance": preview_slug})
            for binding in template_bindings
        ),
        secret_rules=policy_record.rules,
        integration_key_markers=policy_record.integration_key_markers,
        unreasoned_shared_integration_keys="report",
    )


def is_preview_copied_credential(
    key_name: str, value: str, *, template_bindings: tuple[SecretBinding, ...]
) -> bool:
    """A copied value is a credential when a managed secret delivers it or it looks like one."""
    if not value.strip():
        return False
    return is_credential_runtime_value(key_name, value) or any(
        binding.binding_key == key_name for binding in template_bindings
    )


def preview_copied_integration_credential_keys(
    values: Mapping[str, str],
    *,
    template_bindings: tuple[SecretBinding, ...],
    extra_integration_key_markers: Iterable[str] = (),
) -> tuple[str, ...]:
    markers = tuple(extra_integration_key_markers)
    return tuple(
        sorted(
            key
            for key, value in values.items()
            if is_integration_runtime_key(key, extra_markers=markers)
            and is_preview_copied_credential(key, value, template_bindings=template_bindings)
        )
    )


def runtime_secret_binding_matches_target(
    *, binding: SecretBinding, target: RuntimeKeySafetyTarget
) -> bool:
    return _binding_route_rank(binding=binding, target=target) > 0


def evaluate_runtime_key_safety_from_store(
    *,
    record_store: RuntimeKeySafetyPolicyReadStore,
    target: RuntimeKeySafetyTarget,
    required_binding_keys: Iterable[str],
    policy_record: RuntimeKeySafetyPolicyRecord | None = None,
    unreasoned_shared_integration_keys: UnreasonedSharedIntegrationKeyMode = "refuse",
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
        integration_key_markers=policy.integration_key_markers,
        unreasoned_shared_integration_keys=unreasoned_shared_integration_keys,
    )


def evaluate_runtime_key_safety(
    *,
    target: RuntimeKeySafetyTarget,
    required_binding_keys: Iterable[str],
    secret_bindings: Iterable[SecretBinding],
    secret_rules: Iterable[RuntimeSecretSafetyRule],
    integration_key_markers: Iterable[str] = (),
    unreasoned_shared_integration_keys: UnreasonedSharedIntegrationKeyMode = "refuse",
) -> RuntimeKeySafetyEvaluation:
    extra_integration_key_markers = tuple(integration_key_markers)
    checked_binding_keys = _normalize_required_binding_keys(required_binding_keys)
    rules_by_binding_key = _rules_by_binding_key(secret_rules)
    bindings_by_binding_key = _bindings_by_binding_key(secret_bindings)
    findings: list[RuntimeKeySafetyFinding] = []
    reported: list[RuntimeKeySafetyFinding] = []

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

        binding_findings = _evaluate_binding(
            target=target,
            binding=binding,
            rule=rules_by_binding_key.get(binding.binding_key),
            extra_integration_key_markers=extra_integration_key_markers,
        )
        findings.extend(binding_findings)
        # A declared shared_safe production key needs its reason whether the lane
        # or a policy rule classifies it.
        missing_reason = _missing_sharing_reason(
            target=target,
            binding=binding,
            extra_integration_key_markers=extra_integration_key_markers,
        )
        if missing_reason is not None and not binding_findings:
            if unreasoned_shared_integration_keys == "refuse":
                findings.append(missing_reason)
            else:
                reported.append(missing_reason)

    return RuntimeKeySafetyEvaluation(
        status="fail" if findings else "pass",
        target=target,
        checked_binding_keys=checked_binding_keys,
        findings=tuple(findings),
        reported=tuple(reported),
    )


def _evaluate_binding(
    *,
    target: RuntimeKeySafetyTarget,
    binding: SecretBinding,
    rule: RuntimeSecretSafetyRule | None,
    extra_integration_key_markers: tuple[str, ...],
) -> tuple[RuntimeKeySafetyFinding, ...]:
    if rule is not None:
        return _evaluate_binding_rule(target=target, binding=binding, rule=rule)
    if _binding_stored_for_exact_stable_lane(
        binding=binding,
        target=target,
        extra_integration_key_markers=extra_integration_key_markers,
    ):
        return _evaluate_declared_secret_class(target=target, binding=binding)
    return (
        RuntimeKeySafetyFinding(
            code="unclassified_binding",
            binding_key=binding.binding_key,
            binding_id=binding.binding_id,
            secret_id=binding.secret_id,
            detail=f"Managed secret binding {binding.binding_key!r} has no runtime key safety rule.",
        ),
    )


_LANE_CLASSIFIED_ENVIRONMENT_CLASSES = frozenset({"prod", "testing", "dev"})


# A secret stored for one exact stable lane resolves only for that lane, so the
# lane is its classification and no policy rule is needed. Previews are
# excluded: they copy template-lane values, and their check retargets the
# template's bindings to the preview, which would otherwise look lane-exact.
# A production integration credential stored on a non-production lane is also
# excluded unless its writer declared a class: the lane cannot tell a production
# key from a test key, so it needs a rule or a declared class.
def _binding_stored_for_exact_stable_lane(
    *,
    binding: SecretBinding,
    target: RuntimeKeySafetyTarget,
    extra_integration_key_markers: tuple[str, ...],
) -> bool:
    if (
        target.environment_class != "prod"
        and binding.declared_secret_class is None
        and is_integration_runtime_key(
            binding.binding_key, extra_markers=extra_integration_key_markers
        )
    ):
        return False
    return (
        target.environment_class in _LANE_CLASSIFIED_ENVIRONMENT_CLASSES
        and bool(binding.context)
        and bool(binding.instance)
        and binding.context == target.context
        and binding.instance == target.instance
    )


def _evaluate_declared_secret_class(
    *, target: RuntimeKeySafetyTarget, binding: SecretBinding
) -> tuple[RuntimeKeySafetyFinding, ...]:
    declared_class = binding.declared_secret_class
    if declared_class is None:
        return ()
    if declared_class in ALLOWED_SECRET_CLASSES_BY_ENVIRONMENT[target.environment_class]:
        return ()
    return (
        RuntimeKeySafetyFinding(
            code="secret_class_not_allowed",
            binding_key=binding.binding_key,
            binding_id=binding.binding_id,
            secret_id=binding.secret_id,
            secret_class=declared_class,
            detail=(
                f"Managed secret binding {binding.binding_key!r} is declared "
                f"{declared_class!r}, which is not allowed for "
                f"{target.environment_class!r} environments."
            ),
        ),
    )


# A production integration key a writer declared shared_safe on a non-production
# lane is the one case where a lane's own binding carries a production key on
# purpose. The class alone says nothing about why, so it needs a recorded reason.
def _missing_sharing_reason(
    *,
    target: RuntimeKeySafetyTarget,
    binding: SecretBinding,
    extra_integration_key_markers: tuple[str, ...],
) -> RuntimeKeySafetyFinding | None:
    if (
        target.environment_class == "prod"
        or binding.declared_secret_class != "shared_safe"
        or binding.sharing_reason is not None
        or not is_integration_runtime_key(
            binding.binding_key, extra_markers=extra_integration_key_markers
        )
    ):
        return None
    return RuntimeKeySafetyFinding(
        code="sharing_reason_missing",
        binding_key=binding.binding_key,
        binding_id=binding.binding_id,
        secret_id=binding.secret_id,
        secret_class=binding.declared_secret_class,
        detail=(
            f"Integration key {binding.binding_key!r} is declared 'shared_safe' on a "
            f"{target.environment_class!r} lane with no recorded sharing reason and evidence."
        ),
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
    return tuple(binding for binding, rank in ranked_bindings if rank == highest_rank)


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
    if not target_allowed and not _instance_allowed_for_diagnostics(target=target, rule=rule):
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
    return any(_target_scope_allowed(target=target, scope=scope) for scope in rule.allowed_targets)


def _context_allowed(*, target: RuntimeKeySafetyTarget, rule: RuntimeSecretSafetyRule) -> bool:
    legacy_restricted = bool(
        rule.allowed_contexts or rule.allowed_instances or rule.allowed_instance_patterns
    )
    if legacy_restricted and (not rule.allowed_contexts or target.context in rule.allowed_contexts):
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


def _legacy_scope_allowed(*, target: RuntimeKeySafetyTarget, rule: RuntimeSecretSafetyRule) -> bool:
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
    return any(fnmatchcase(instance, pattern) for pattern in instance_patterns)
