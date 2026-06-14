from __future__ import annotations

import unittest

from control_plane.contracts.runtime_key_safety_policy import (
    RuntimeKeySafetyPolicyRecord,
    RuntimeSecretSafetyRule,
    RuntimeSecretSafetyTargetScope,
)
from control_plane.runtime_key_safety_service import (
    merge_runtime_key_safety_scope_values,
    merge_runtime_key_safety_rules,
    normalize_runtime_key_safety_requested_rule,
    summarize_runtime_key_safety_policy_record,
    write_runtime_key_safety_policy,
)


class _RuntimeKeySafetyPolicyStore:
    def __init__(self, records: tuple[RuntimeKeySafetyPolicyRecord, ...] = ()) -> None:
        self.records = records
        self.written_records: list[RuntimeKeySafetyPolicyRecord] = []

    def list_runtime_key_safety_policy_records(
        self,
        *,
        status: str = "",
        limit: int | None = None,
    ) -> tuple[RuntimeKeySafetyPolicyRecord, ...]:
        records = tuple(record for record in self.records if not status or record.status == status)
        if limit is not None:
            return records[:limit]
        return records

    def write_runtime_key_safety_policy_record(self, record: RuntimeKeySafetyPolicyRecord) -> None:
        self.written_records.append(record)
        self.records = (record,) + self.records


def _rule(
    binding_key: str,
    *,
    secret_class: str = "prod_only",
    contexts: tuple[str, ...] = (),
    instances: tuple[str, ...] = (),
    instance_patterns: tuple[str, ...] = (),
    description: str = "",
) -> RuntimeSecretSafetyRule:
    return RuntimeSecretSafetyRule.model_validate(
        {
            "binding_key": binding_key,
            "secret_class": secret_class,
            "allowed_contexts": contexts,
            "allowed_instances": instances,
            "allowed_instance_patterns": instance_patterns,
            "description": description,
        }
    )


def _target_scope(
    context: str,
    *,
    instances: tuple[str, ...] = (),
    instance_patterns: tuple[str, ...] = (),
) -> RuntimeSecretSafetyTargetScope:
    return RuntimeSecretSafetyTargetScope(
        context=context,
        instances=instances,
        instance_patterns=instance_patterns,
    )


class RuntimeKeySafetyServiceTests(unittest.TestCase):
    def test_scope_value_merge_preserves_open_scope(self) -> None:
        self.assertEqual(merge_runtime_key_safety_scope_values((), ("prod",)), ())
        self.assertEqual(merge_runtime_key_safety_scope_values(("prod",), ()), ())
        self.assertEqual(
            merge_runtime_key_safety_scope_values(("prod",), ("testing",)),
            ("prod", "testing"),
        )

    def test_rule_merge_combines_matching_secret_class_and_sorts_by_binding(self) -> None:
        merged_rules = merge_runtime_key_safety_rules(
            (
                _rule(
                    "SMTP_PASSWORD",
                    contexts=("sellyouroutboard",),
                    instances=("prod",),
                    description="existing",
                ),
            ),
            (
                _rule("RESEND_API_KEY", contexts=("sellyouroutboard",), instances=("prod",)),
                _rule(
                    "SMTP_PASSWORD",
                    contexts=("verireel",),
                    instances=("prod",),
                    description="",
                ),
            ),
        )

        self.assertEqual(
            [rule.binding_key for rule in merged_rules], ["RESEND_API_KEY", "SMTP_PASSWORD"]
        )
        smtp_rule = merged_rules[1]
        self.assertEqual(smtp_rule.allowed_contexts, ("sellyouroutboard", "verireel"))
        self.assertEqual(smtp_rule.allowed_instances, ("prod",))
        self.assertEqual(smtp_rule.description, "existing")

    def test_rule_merge_converts_requested_legacy_instance_patterns_to_paired_targets(
        self,
    ) -> None:
        merged_rules = merge_runtime_key_safety_rules(
            (
                _rule(
                    "POSTGRES_PASSWORD",
                    secret_class="shared_safe",
                    contexts=("verireel",),
                    instances=("testing",),
                ),
            ),
            (
                _rule(
                    "POSTGRES_PASSWORD",
                    secret_class="shared_safe",
                    contexts=("verireel-testing",),
                    instance_patterns=("pr-*",),
                ),
            ),
        )

        self.assertEqual(len(merged_rules), 1)
        merged_rule = merged_rules[0]
        self.assertEqual(merged_rule.allowed_contexts, ("verireel",))
        self.assertEqual(merged_rule.allowed_instances, ("testing",))
        self.assertEqual(merged_rule.allowed_instance_patterns, ())
        self.assertEqual(
            merged_rule.allowed_targets,
            (
                _target_scope(
                    "verireel-testing",
                    instance_patterns=("pr-*",),
                ),
            ),
        )

    def test_requested_legacy_pattern_rule_normalizes_each_context_to_paired_target(
        self,
    ) -> None:
        normalized_rule = normalize_runtime_key_safety_requested_rule(
            RuntimeSecretSafetyRule(
                binding_key="POSTGRES_PASSWORD",
                secret_class="shared_safe",
                allowed_contexts=("verireel-testing", "opw-testing"),
                allowed_instances=("pr-216",),
                allowed_instance_patterns=("pr-*",),
            )
        )

        self.assertEqual(normalized_rule.allowed_contexts, ())
        self.assertEqual(normalized_rule.allowed_instances, ())
        self.assertEqual(normalized_rule.allowed_instance_patterns, ())
        self.assertEqual(
            normalized_rule.allowed_targets,
            (
                _target_scope(
                    "verireel-testing",
                    instances=("pr-216",),
                    instance_patterns=("pr-*",),
                ),
                _target_scope(
                    "opw-testing",
                    instances=("pr-216",),
                    instance_patterns=("pr-*",),
                ),
            ),
        )

    def test_requested_legacy_pattern_rule_requires_context(self) -> None:
        with self.assertRaisesRegex(ValueError, "paired allowed_targets"):
            normalize_runtime_key_safety_requested_rule(
                RuntimeSecretSafetyRule(
                    binding_key="POSTGRES_PASSWORD",
                    secret_class="shared_safe",
                    allowed_instance_patterns=("pr-*",),
                )
            )

    def test_requested_rule_rejects_mixed_legacy_patterns_and_paired_targets(self) -> None:
        with self.assertRaisesRegex(ValueError, "cannot mix"):
            normalize_runtime_key_safety_requested_rule(
                RuntimeSecretSafetyRule(
                    binding_key="POSTGRES_PASSWORD",
                    secret_class="shared_safe",
                    allowed_contexts=("verireel-testing",),
                    allowed_instance_patterns=("pr-*",),
                    allowed_targets=(
                        _target_scope(
                            "verireel-testing",
                            instance_patterns=("pr-*",),
                        ),
                    ),
                )
            )

    def test_rule_merge_adds_paired_preview_target_without_broadening_legacy_scope(
        self,
    ) -> None:
        merged_rules = merge_runtime_key_safety_rules(
            (
                _rule(
                    "POSTGRES_PASSWORD",
                    secret_class="shared_safe",
                    contexts=("verireel",),
                    instances=("testing",),
                ),
            ),
            (
                RuntimeSecretSafetyRule(
                    binding_key="POSTGRES_PASSWORD",
                    secret_class="shared_safe",
                    allowed_targets=(
                        _target_scope(
                            "verireel-testing",
                            instance_patterns=("pr-*",),
                        ),
                    ),
                ),
            ),
        )

        self.assertEqual(len(merged_rules), 1)
        merged_rule = merged_rules[0]
        self.assertEqual(merged_rule.allowed_contexts, ("verireel",))
        self.assertEqual(merged_rule.allowed_instances, ("testing",))
        self.assertEqual(merged_rule.allowed_instance_patterns, ())
        self.assertEqual(
            merged_rule.allowed_targets,
            (
                _target_scope(
                    "verireel-testing",
                    instance_patterns=("pr-*",),
                ),
            ),
        )

    def test_rule_merge_combines_paired_target_scopes_by_context(self) -> None:
        merged_rules = merge_runtime_key_safety_rules(
            (
                RuntimeSecretSafetyRule(
                    binding_key="POSTGRES_PASSWORD",
                    secret_class="shared_safe",
                    allowed_targets=(
                        _target_scope("verireel-testing", instances=("pr-216",)),
                    ),
                ),
            ),
            (
                RuntimeSecretSafetyRule(
                    binding_key="POSTGRES_PASSWORD",
                    secret_class="shared_safe",
                    allowed_targets=(
                        _target_scope(
                            "verireel-testing",
                            instance_patterns=("pr-*",),
                        ),
                    ),
                ),
            ),
        )

        self.assertEqual(len(merged_rules), 1)
        self.assertEqual(
            merged_rules[0].allowed_targets,
            (
                _target_scope(
                    "verireel-testing",
                    instances=("pr-216",),
                    instance_patterns=("pr-*",),
                ),
            ),
        )

    def test_rule_merge_adds_legacy_scope_to_paired_only_rule_without_opening_it(
        self,
    ) -> None:
        merged_rules = merge_runtime_key_safety_rules(
            (
                RuntimeSecretSafetyRule(
                    binding_key="POSTGRES_PASSWORD",
                    secret_class="shared_safe",
                    allowed_targets=(
                        _target_scope(
                            "verireel-testing",
                            instance_patterns=("pr-*",),
                        ),
                    ),
                ),
            ),
            (
                _rule(
                    "POSTGRES_PASSWORD",
                    secret_class="shared_safe",
                    contexts=("verireel",),
                    instances=("testing",),
                ),
            ),
        )

        self.assertEqual(len(merged_rules), 1)
        merged_rule = merged_rules[0]
        self.assertEqual(merged_rule.allowed_contexts, ("verireel",))
        self.assertEqual(merged_rule.allowed_instances, ("testing",))
        self.assertEqual(merged_rule.allowed_instance_patterns, ())
        self.assertEqual(
            merged_rule.allowed_targets,
            (
                _target_scope(
                    "verireel-testing",
                    instance_patterns=("pr-*",),
                ),
            ),
        )

    def test_write_runtime_key_safety_policy_records_only_changed_policy(self) -> None:
        store = _RuntimeKeySafetyPolicyStore()

        record, changed = write_runtime_key_safety_policy(
            record_store=store,
            rules=(_rule("SMTP_PASSWORD", contexts=("sellyouroutboard",), instances=("prod",)),),
            source_label="test:runtime-key-safety-policy",
            now_timestamp=lambda: "2026-05-07T15:45:00Z",
            record_slug=lambda value: value.lower().replace(":", "-"),
        )

        self.assertTrue(changed)
        self.assertEqual(store.written_records, [record])
        self.assertTrue(
            record.record_id.startswith("runtime-key-safety-policy-2026-05-07t15-45-00z-")
        )
        summary = summarize_runtime_key_safety_policy_record(record)
        self.assertEqual(summary["binding_keys"], ["SMTP_PASSWORD"])

        repeated_record, repeated_changed = write_runtime_key_safety_policy(
            record_store=store,
            rules=(_rule("SMTP_PASSWORD", contexts=("sellyouroutboard",), instances=("prod",)),),
            source_label="test:runtime-key-safety-policy",
            now_timestamp=lambda: "2026-05-07T15:46:00Z",
            record_slug=lambda value: value.lower().replace(":", "-"),
        )

        self.assertFalse(repeated_changed)
        self.assertEqual(repeated_record.record_id, record.record_id)
        self.assertEqual(store.written_records, [record])


if __name__ == "__main__":
    unittest.main()
