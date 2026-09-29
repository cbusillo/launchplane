import hashlib
import json
import unittest

from control_plane.contracts.runtime_key_safety_policy import (
    RuntimeEnvironmentClass,
    RuntimeKeySafetyPolicyRecord,
    RuntimeKeySafetyTarget,
    RuntimeSecretClass,
    RuntimeSecretSafetyRule,
    RuntimeSecretSafetyTargetScope,
)
from control_plane.contracts.secret_record import SecretBinding
from control_plane.contracts.secret_record import SecretStatus
from control_plane.runtime_key_safety import (
    evaluate_runtime_key_safety,
    evaluate_runtime_key_safety_from_store,
    is_integration_runtime_key,
    is_secret_shaped_runtime_key,
    latest_active_runtime_key_safety_policy,
    runtime_key_safety_environment_class,
)


class _FakeRuntimeKeySafetyStore:
    def __init__(
        self,
        *,
        policies: tuple[RuntimeKeySafetyPolicyRecord, ...],
        bindings: tuple[SecretBinding, ...],
    ) -> None:
        self.policies = policies
        self.bindings = bindings
        self.requested_context = ""
        self.requested_instance = ""

    def list_runtime_key_safety_policy_records(
        self,
        *,
        status: str = "",
        limit: int | None = None,
    ) -> tuple[RuntimeKeySafetyPolicyRecord, ...]:
        records = tuple(record for record in self.policies if not status or record.status == status)
        return records[:limit] if limit is not None else records

    def list_secret_bindings(
        self,
        *,
        integration: str = "",
        context_name: str = "",
        instance_name: str = "",
        limit: int | None = None,
    ) -> tuple[SecretBinding, ...]:
        self.requested_context = context_name
        self.requested_instance = instance_name
        records = tuple(
            binding
            for binding in self.bindings
            if (not integration or binding.integration == integration)
            and (not context_name or binding.context == context_name)
            and (not instance_name or binding.instance == instance_name)
        )
        return records[:limit] if limit is not None else records


def _binding(
    *,
    binding_key: str,
    binding_id: str = "binding-shopify-token",
    secret_id: str = "secret-shopify-token",
    context: str = "opw",
    instance: str = "testing",
    status: SecretStatus = "configured",
) -> SecretBinding:
    return SecretBinding(
        binding_id=binding_id,
        secret_id=secret_id,
        integration="runtime_environment",
        binding_key=binding_key,
        context=context,
        instance=instance,
        status=status,
        created_at="2026-05-05T20:00:00Z",
        updated_at="2026-05-05T20:00:00Z",
    )


class RuntimeKeySafetyAuthorityTests(unittest.TestCase):
    def test_runtime_environment_classification_preserves_all_aliases_and_boundaries(self) -> None:
        cases = {
            "prod": "prod",
            " Production ": "prod",
            "testing": "testing",
            "STAGE": "testing",
            "preview": "preview",
            "pr": "preview",
            " PR-1727 ": "preview",
            "dev": "dev",
            "development": "dev",
            "previewer": "unknown",
            "prerender": "unknown",
            "": "unknown",
        }

        for instance_name, expected_class in cases.items():
            with self.subTest(instance_name=instance_name):
                self.assertEqual(
                    runtime_key_safety_environment_class(instance_name), expected_class
                )

    def test_secret_shaped_key_detection_matches_whole_key_parts(self) -> None:
        cases = {
            "API_TOKEN": True,
            "PASSWORD": True,
            "service_secret": True,
            "DATABASE_URL": False,
            "TOKENIZED": False,
            "KEYBOARD": False,
            "PRIVATEKEY": False,
        }

        for key_name, expected in cases.items():
            with self.subTest(key_name=key_name):
                self.assertEqual(is_secret_shaped_runtime_key(key_name), expected)


class RuntimeKeySafetyTests(unittest.TestCase):
    def test_testing_environment_rejects_prod_only_secret(self) -> None:
        evaluation = evaluate_runtime_key_safety(
            target=RuntimeKeySafetyTarget(
                context="opw",
                instance="testing",
                environment_class="testing",
            ),
            required_binding_keys=("SHOPIFY_ACCESS_TOKEN",),
            secret_bindings=(
                _binding(binding_key="SHOPIFY_ACCESS_TOKEN", secret_id="secret-prod-shopify-token"),
            ),
            secret_rules=(
                RuntimeSecretSafetyRule(
                    binding_key="SHOPIFY_ACCESS_TOKEN",
                    secret_class="prod_only",
                ),
            ),
        )

        self.assertEqual(evaluation.status, "fail")
        self.assertEqual(evaluation.findings[0].code, "secret_class_not_allowed")
        self.assertEqual(evaluation.findings[0].binding_key, "SHOPIFY_ACCESS_TOKEN")
        self.assertEqual(evaluation.findings[0].secret_id, "secret-prod-shopify-token")

    def test_testing_environment_accepts_testing_secret(self) -> None:
        evaluation = evaluate_runtime_key_safety(
            target=RuntimeKeySafetyTarget(
                context="opw",
                instance="testing",
                environment_class="testing",
            ),
            required_binding_keys=("SHOPIFY_ACCESS_TOKEN",),
            secret_bindings=(_binding(binding_key="SHOPIFY_ACCESS_TOKEN"),),
            secret_rules=(
                RuntimeSecretSafetyRule(
                    binding_key="SHOPIFY_ACCESS_TOKEN",
                    secret_class="testing",
                    allowed_contexts=("opw",),
                    allowed_instances=("testing",),
                ),
            ),
        )

        self.assertEqual(evaluation.status, "pass")
        self.assertEqual(evaluation.findings, ())
        self.assertEqual(evaluation.checked_binding_keys, ("SHOPIFY_ACCESS_TOKEN",))

    def test_preview_instance_pattern_accepts_matching_preview_instance(self) -> None:
        evaluation = evaluate_runtime_key_safety(
            target=RuntimeKeySafetyTarget(
                context="verireel-testing",
                instance="pr-217",
                environment_class="preview",
            ),
            required_binding_keys=("POSTGRES_PASSWORD",),
            secret_bindings=(
                _binding(
                    binding_key="POSTGRES_PASSWORD",
                    binding_id="binding-postgres-password",
                    secret_id="secret-postgres-password",
                    context="verireel-testing",
                    instance="pr-217",
                ),
            ),
            secret_rules=(
                RuntimeSecretSafetyRule(
                    binding_key="POSTGRES_PASSWORD",
                    secret_class="shared_safe",
                    allowed_contexts=("verireel-testing",),
                    allowed_instance_patterns=("pr-*",),
                ),
            ),
        )

        self.assertEqual(evaluation.status, "pass")
        self.assertEqual(evaluation.findings, ())

    def test_paired_target_scope_accepts_matching_preview_instance(self) -> None:
        evaluation = evaluate_runtime_key_safety(
            target=RuntimeKeySafetyTarget(
                context="verireel-testing",
                instance="pr-217",
                environment_class="preview",
            ),
            required_binding_keys=("POSTGRES_PASSWORD",),
            secret_bindings=(
                _binding(
                    binding_key="POSTGRES_PASSWORD",
                    binding_id="binding-postgres-password",
                    secret_id="secret-postgres-password",
                    context="verireel-testing",
                    instance="pr-217",
                ),
            ),
            secret_rules=(
                RuntimeSecretSafetyRule(
                    binding_key="POSTGRES_PASSWORD",
                    secret_class="shared_safe",
                    allowed_contexts=("verireel",),
                    allowed_instances=("testing",),
                    allowed_targets=(
                        RuntimeSecretSafetyTargetScope(
                            context="verireel-testing",
                            instance_patterns=("pr-*",),
                        ),
                    ),
                ),
            ),
        )

        self.assertEqual(evaluation.status, "pass")
        self.assertEqual(evaluation.findings, ())

    def test_paired_target_scope_rejects_cross_product_preview_instance(self) -> None:
        evaluation = evaluate_runtime_key_safety(
            target=RuntimeKeySafetyTarget(
                context="verireel",
                instance="pr-217",
                environment_class="preview",
            ),
            required_binding_keys=("POSTGRES_PASSWORD",),
            secret_bindings=(
                _binding(
                    binding_key="POSTGRES_PASSWORD",
                    binding_id="binding-postgres-password",
                    secret_id="secret-postgres-password",
                    context="verireel",
                    instance="pr-217",
                ),
            ),
            secret_rules=(
                RuntimeSecretSafetyRule(
                    binding_key="POSTGRES_PASSWORD",
                    secret_class="shared_safe",
                    allowed_contexts=("verireel",),
                    allowed_instances=("testing",),
                    allowed_targets=(
                        RuntimeSecretSafetyTargetScope(
                            context="verireel-testing",
                            instance_patterns=("pr-*",),
                        ),
                    ),
                ),
            ),
        )

        self.assertEqual(evaluation.status, "fail")
        self.assertEqual(
            [finding.code for finding in evaluation.findings],
            ["instance_not_allowed"],
        )

    def test_paired_target_scope_rejects_cross_product_stable_instance(self) -> None:
        evaluation = evaluate_runtime_key_safety(
            target=RuntimeKeySafetyTarget(
                context="verireel-testing",
                instance="testing",
                environment_class="testing",
            ),
            required_binding_keys=("POSTGRES_PASSWORD",),
            secret_bindings=(
                _binding(
                    binding_key="POSTGRES_PASSWORD",
                    binding_id="binding-postgres-password",
                    secret_id="secret-postgres-password",
                    context="verireel-testing",
                    instance="testing",
                ),
            ),
            secret_rules=(
                RuntimeSecretSafetyRule(
                    binding_key="POSTGRES_PASSWORD",
                    secret_class="shared_safe",
                    allowed_contexts=("verireel",),
                    allowed_instances=("testing",),
                    allowed_targets=(
                        RuntimeSecretSafetyTargetScope(
                            context="verireel-testing",
                            instance_patterns=("pr-*",),
                        ),
                    ),
                ),
            ),
        )

        self.assertEqual(evaluation.status, "fail")
        self.assertEqual(
            [finding.code for finding in evaluation.findings],
            ["instance_not_allowed"],
        )

    def test_preview_instance_pattern_still_rejects_wrong_context(self) -> None:
        evaluation = evaluate_runtime_key_safety(
            target=RuntimeKeySafetyTarget(
                context="opw-testing",
                instance="pr-217",
                environment_class="preview",
            ),
            required_binding_keys=("POSTGRES_PASSWORD",),
            secret_bindings=(
                _binding(
                    binding_key="POSTGRES_PASSWORD",
                    binding_id="binding-postgres-password",
                    secret_id="secret-postgres-password",
                    context="opw-testing",
                    instance="pr-217",
                ),
            ),
            secret_rules=(
                RuntimeSecretSafetyRule(
                    binding_key="POSTGRES_PASSWORD",
                    secret_class="shared_safe",
                    allowed_contexts=("verireel-testing",),
                    allowed_instance_patterns=("pr-*",),
                ),
            ),
        )

        self.assertEqual(evaluation.status, "fail")
        self.assertEqual(
            [finding.code for finding in evaluation.findings],
            ["context_not_allowed"],
        )

    def test_preview_instance_pattern_still_rejects_non_matching_instance(self) -> None:
        evaluation = evaluate_runtime_key_safety(
            target=RuntimeKeySafetyTarget(
                context="verireel-testing",
                instance="testing",
                environment_class="testing",
            ),
            required_binding_keys=("POSTGRES_PASSWORD",),
            secret_bindings=(
                _binding(
                    binding_key="POSTGRES_PASSWORD",
                    binding_id="binding-postgres-password",
                    secret_id="secret-postgres-password",
                    context="verireel-testing",
                    instance="testing",
                ),
            ),
            secret_rules=(
                RuntimeSecretSafetyRule(
                    binding_key="POSTGRES_PASSWORD",
                    secret_class="shared_safe",
                    allowed_contexts=("verireel-testing",),
                    allowed_instance_patterns=("pr-*",),
                ),
            ),
        )

        self.assertEqual(evaluation.status, "fail")
        self.assertEqual(
            [finding.code for finding in evaluation.findings],
            ["instance_not_allowed"],
        )

    def test_preview_instance_pattern_does_not_match_separator_bearing_target(self) -> None:
        evaluation = evaluate_runtime_key_safety(
            target=RuntimeKeySafetyTarget(
                context="verireel-testing",
                instance="pr-217/other",
                environment_class="preview",
            ),
            required_binding_keys=("POSTGRES_PASSWORD",),
            secret_bindings=(
                _binding(
                    binding_key="POSTGRES_PASSWORD",
                    binding_id="binding-postgres-password",
                    secret_id="secret-postgres-password",
                    context="verireel-testing",
                    instance="pr-217/other",
                ),
            ),
            secret_rules=(
                RuntimeSecretSafetyRule(
                    binding_key="POSTGRES_PASSWORD",
                    secret_class="shared_safe",
                    allowed_contexts=("verireel-testing",),
                    allowed_instance_patterns=("pr-*",),
                ),
            ),
        )

        self.assertEqual(evaluation.status, "fail")
        self.assertEqual(
            [finding.code for finding in evaluation.findings],
            ["instance_not_allowed"],
        )

    def test_preview_instance_pattern_rejects_path_separator(self) -> None:
        with self.assertRaisesRegex(ValueError, "path separators"):
            RuntimeSecretSafetyRule(
                binding_key="POSTGRES_PASSWORD",
                secret_class="shared_safe",
                allowed_instance_patterns=("pr-*/*",),
            )

    def test_preview_instance_pattern_rejects_universal_pattern(self) -> None:
        with self.assertRaisesRegex(ValueError, "literal character"):
            RuntimeSecretSafetyRule(
                binding_key="POSTGRES_PASSWORD",
                secret_class="shared_safe",
                allowed_instance_patterns=("*",),
            )

    def test_preview_instance_pattern_rejects_whitespace(self) -> None:
        with self.assertRaisesRegex(ValueError, "whitespace"):
            RuntimeSecretSafetyRule(
                binding_key="POSTGRES_PASSWORD",
                secret_class="shared_safe",
                allowed_instance_patterns=("pr-* preview",),
            )

    def test_paired_target_scope_rejects_universal_pattern(self) -> None:
        with self.assertRaisesRegex(ValueError, "literal character"):
            RuntimeSecretSafetyTargetScope(
                context="verireel-testing",
                instance_patterns=("*",),
            )

    def test_unclassified_shared_secret_fails_closed(self) -> None:
        for shared_binding in (
            _binding(binding_key="SHOPIFY_ACCESS_TOKEN", instance=""),
            _binding(binding_key="SHOPIFY_ACCESS_TOKEN", context="", instance=""),
        ):
            with self.subTest(context=shared_binding.context, instance=shared_binding.instance):
                evaluation = evaluate_runtime_key_safety(
                    target=RuntimeKeySafetyTarget(
                        context="opw",
                        instance="testing",
                        environment_class="testing",
                    ),
                    required_binding_keys=("SHOPIFY_ACCESS_TOKEN",),
                    secret_bindings=(shared_binding,),
                    secret_rules=(),
                )

                self.assertEqual(evaluation.status, "fail")
                self.assertEqual(evaluation.findings[0].code, "unclassified_binding")

    def test_unclassified_ordinary_secret_stored_for_the_exact_stable_lane_passes(self) -> None:
        lanes: tuple[tuple[str, RuntimeEnvironmentClass], ...] = (
            ("testing", "testing"),
            ("dev", "dev"),
            ("prod", "prod"),
        )
        for instance, environment_class in lanes:
            with self.subTest(instance=instance):
                evaluation = evaluate_runtime_key_safety(
                    target=RuntimeKeySafetyTarget(
                        context="opw",
                        instance=instance,
                        environment_class=environment_class,
                    ),
                    required_binding_keys=("CONTACT_ALERT_DISCORD_WEBHOOK_URL",),
                    secret_bindings=(
                        _binding(
                            binding_key="CONTACT_ALERT_DISCORD_WEBHOOK_URL",
                            instance=instance,
                        ),
                    ),
                    secret_rules=(),
                )

                self.assertEqual(evaluation.status, "pass")
                self.assertEqual(evaluation.findings, ())

    def test_declared_class_on_an_exact_lane_binding_must_suit_the_lane(self) -> None:
        cases: tuple[tuple[RuntimeSecretClass, str], ...] = (
            ("testing", "pass"),
            ("non_prod", "pass"),
            ("prod_only", "fail"),
            ("preview", "fail"),
        )
        for declared_class, expected_status in cases:
            with self.subTest(declared_class=declared_class):
                evaluation = evaluate_runtime_key_safety(
                    target=RuntimeKeySafetyTarget(
                        context="opw",
                        instance="testing",
                        environment_class="testing",
                    ),
                    required_binding_keys=("DEV_STORE_API_TOKEN",),
                    secret_bindings=(
                        _binding(binding_key="DEV_STORE_API_TOKEN", instance="testing").model_copy(
                            update={"declared_secret_class": declared_class}
                        ),
                    ),
                    secret_rules=(),
                )

                self.assertEqual(evaluation.status, expected_status)
                if expected_status == "fail":
                    self.assertEqual(evaluation.findings[0].code, "secret_class_not_allowed")
                    self.assertEqual(evaluation.findings[0].secret_class, declared_class)

    def test_declared_secret_class_requires_an_exact_lane_binding(self) -> None:
        with self.assertRaises(ValueError):
            SecretBinding(
                binding_id="binding-1",
                secret_id="secret-1",
                integration="runtime_environment",
                binding_key="DEV_STORE_API_TOKEN",
                context="opw",
                declared_secret_class="testing",
                created_at="2026-09-29T00:00:00Z",
                updated_at="2026-09-29T00:00:00Z",
            )

    def test_unclassified_integration_secret_on_a_non_production_lane_fails(self) -> None:
        lanes: tuple[tuple[str, RuntimeEnvironmentClass], ...] = (
            ("testing", "testing"),
            ("dev", "dev"),
        )
        for binding_key in (
            "SHOPIFY_ACCESS_TOKEN",
            "ENV_OVERRIDE_SHOPIFY__API_TOKEN",
            "STRIPE_SECRET_KEY",
            "SMTP_PASSWORD",
            "PRINTNODE_API_KEY",
            "REPAIRSHOPR_API_KEY",
            "FISHBOWL_PASSWORD",
            "RESEND_API_KEY",
        ):
            for instance, environment_class in lanes:
                with self.subTest(binding_key=binding_key, instance=instance):
                    evaluation = evaluate_runtime_key_safety(
                        target=RuntimeKeySafetyTarget(
                            context="opw",
                            instance=instance,
                            environment_class=environment_class,
                        ),
                        required_binding_keys=(binding_key,),
                        secret_bindings=(_binding(binding_key=binding_key, instance=instance),),
                        secret_rules=(),
                    )

                    self.assertEqual(evaluation.status, "fail")
                    self.assertEqual(evaluation.findings[0].code, "unclassified_binding")

    def test_declared_class_classifies_an_integration_secret_on_a_testing_lane(self) -> None:
        cases: tuple[tuple[RuntimeSecretClass, str], ...] = (
            ("testing", "pass"),
            ("prod_only", "fail"),
        )
        for declared_class, expected_status in cases:
            with self.subTest(declared_class=declared_class):
                evaluation = evaluate_runtime_key_safety(
                    target=RuntimeKeySafetyTarget(
                        context="opw",
                        instance="testing",
                        environment_class="testing",
                    ),
                    required_binding_keys=("SHOPIFY_ACCESS_TOKEN",),
                    secret_bindings=(
                        _binding(binding_key="SHOPIFY_ACCESS_TOKEN", instance="testing").model_copy(
                            update={"declared_secret_class": declared_class}
                        ),
                    ),
                    secret_rules=(),
                )

                self.assertEqual(evaluation.status, expected_status)
                if expected_status == "fail":
                    self.assertEqual(evaluation.findings[0].code, "secret_class_not_allowed")

    def test_policy_integration_key_markers_extend_the_default_markers(self) -> None:
        target = RuntimeKeySafetyTarget(
            context="cm",
            instance="testing",
            environment_class="testing",
        )
        binding = _binding(
            binding_key="ENV_OVERRIDE_CM_DATA__DB_PASSWORD",
            context="cm",
            instance="testing",
        )

        without_marker = evaluate_runtime_key_safety(
            target=target,
            required_binding_keys=(binding.binding_key,),
            secret_bindings=(binding,),
            secret_rules=(),
        )
        with_marker = evaluate_runtime_key_safety(
            target=target,
            required_binding_keys=(binding.binding_key,),
            secret_bindings=(binding,),
            secret_rules=(),
            integration_key_markers=("CM_DATA",),
        )

        self.assertEqual(without_marker.status, "pass")
        self.assertEqual(with_marker.status, "fail")
        self.assertEqual(with_marker.findings[0].code, "unclassified_binding")

    def test_classified_integration_secret_on_a_testing_lane_passes(self) -> None:
        evaluation = evaluate_runtime_key_safety(
            target=RuntimeKeySafetyTarget(
                context="opw",
                instance="testing",
                environment_class="testing",
            ),
            required_binding_keys=("SHOPIFY_ACCESS_TOKEN",),
            secret_bindings=(_binding(binding_key="SHOPIFY_ACCESS_TOKEN", instance="testing"),),
            secret_rules=(
                RuntimeSecretSafetyRule(
                    binding_key="SHOPIFY_ACCESS_TOKEN",
                    secret_class="testing",
                ),
            ),
        )

        self.assertEqual(evaluation.status, "pass")

    def test_integration_marker_matches_whole_key_parts_only(self) -> None:
        self.assertTrue(is_integration_runtime_key("SMTP_PASSWORD"))
        self.assertTrue(is_integration_runtime_key("ENV_OVERRIDE_SHOPIFY__WEBHOOK_KEY"))
        self.assertTrue(is_integration_runtime_key("AUTHORIZE_NET_LOGIN"))
        self.assertFalse(is_integration_runtime_key("EMAIL_ALERT_WEBHOOK_URL"))
        self.assertFalse(is_integration_runtime_key("SQUARESPACE_TOKEN"))
        self.assertFalse(is_integration_runtime_key("ODOO_KEY"))
        self.assertTrue(
            is_integration_runtime_key("cm_data.db.password", extra_markers=("CM_DATA",))
        )

    def test_policy_record_normalizes_integration_key_markers(self) -> None:
        record = RuntimeKeySafetyPolicyRecord(
            record_id="runtime-key-safety-policy-1",
            source="test",
            updated_at="2026-09-29T00:00:00Z",
            rules=(RuntimeSecretSafetyRule(binding_key="A", secret_class="shared_safe"),),
            integration_key_markers=(" cm_data ", "CM_DATA"),
        )

        self.assertEqual(record.integration_key_markers, ("CM_DATA",))
        with self.assertRaises(ValueError):
            RuntimeKeySafetyPolicyRecord(
                record_id="runtime-key-safety-policy-1",
                source="test",
                updated_at="2026-09-29T00:00:00Z",
                rules=(RuntimeSecretSafetyRule(binding_key="A", secret_class="shared_safe"),),
                integration_key_markers=("CM-DATA",),
            )

    def test_policy_sha256_is_unchanged_for_records_without_markers(self) -> None:
        record = RuntimeKeySafetyPolicyRecord(
            record_id="runtime-key-safety-policy-1",
            source="test",
            updated_at="2026-09-29T00:00:00Z",
            rules=(RuntimeSecretSafetyRule(binding_key="A", secret_class="shared_safe"),),
        )
        with_marker = record.model_copy(update={"integration_key_markers": ("CM_DATA",)})
        legacy_payload = {
            "schema_version": 1,
            "status": "active",
            "rules": [rule.model_dump(mode="json") for rule in record.rules],
        }
        legacy_sha = hashlib.sha256(
            json.dumps(legacy_payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

        self.assertEqual(record.policy_sha256, legacy_sha)
        self.assertNotEqual(with_marker.policy_sha256, legacy_sha)

    def test_unclassified_lane_secret_still_fails_for_a_preview_target(self) -> None:
        # Preview checks retarget the template lane's bindings to the preview,
        # so a copied lane secret looks lane-exact and must still need a rule.
        evaluation = evaluate_runtime_key_safety(
            target=RuntimeKeySafetyTarget(
                context="opw-preview",
                instance="pr-12",
                environment_class="preview",
            ),
            required_binding_keys=("SHOPIFY_ACCESS_TOKEN",),
            secret_bindings=(
                _binding(
                    binding_key="SHOPIFY_ACCESS_TOKEN",
                    context="opw-preview",
                    instance="pr-12",
                ),
            ),
            secret_rules=(),
        )

        self.assertEqual(evaluation.status, "fail")
        self.assertEqual(evaluation.findings[0].code, "unclassified_binding")

    def test_missing_or_disabled_secret_binding_fails_closed(self) -> None:
        missing_evaluation = evaluate_runtime_key_safety(
            target=RuntimeKeySafetyTarget(
                context="opw",
                instance="testing",
                environment_class="testing",
            ),
            required_binding_keys=("SHOPIFY_ACCESS_TOKEN",),
            secret_bindings=(),
            secret_rules=(
                RuntimeSecretSafetyRule(
                    binding_key="SHOPIFY_ACCESS_TOKEN",
                    secret_class="testing",
                ),
            ),
        )
        disabled_evaluation = evaluate_runtime_key_safety(
            target=RuntimeKeySafetyTarget(
                context="opw",
                instance="testing",
                environment_class="testing",
            ),
            required_binding_keys=("SHOPIFY_ACCESS_TOKEN",),
            secret_bindings=(
                _binding(
                    binding_key="SHOPIFY_ACCESS_TOKEN",
                    status="disabled",
                ),
            ),
            secret_rules=(
                RuntimeSecretSafetyRule(
                    binding_key="SHOPIFY_ACCESS_TOKEN",
                    secret_class="testing",
                ),
            ),
        )

        self.assertEqual(missing_evaluation.status, "fail")
        self.assertEqual(missing_evaluation.findings[0].code, "binding_missing")
        self.assertEqual(disabled_evaluation.status, "fail")
        self.assertEqual(disabled_evaluation.findings[0].code, "binding_disabled")

    def test_more_specific_binding_satisfies_target_when_context_binding_also_exists(
        self,
    ) -> None:
        evaluation = evaluate_runtime_key_safety(
            target=RuntimeKeySafetyTarget(
                context="opw",
                instance="testing",
                environment_class="testing",
            ),
            required_binding_keys=("SHOPIFY_ACCESS_TOKEN",),
            secret_bindings=(
                _binding(
                    binding_key="SHOPIFY_ACCESS_TOKEN",
                    binding_id="binding-context-token",
                    secret_id="secret-context-token",
                ).model_copy(update={"instance": ""}),
                _binding(
                    binding_key="SHOPIFY_ACCESS_TOKEN",
                    binding_id="binding-instance-token",
                    secret_id="secret-instance-token",
                ),
            ),
            secret_rules=(
                RuntimeSecretSafetyRule(
                    binding_key="SHOPIFY_ACCESS_TOKEN",
                    secret_class="testing",
                    allowed_contexts=("opw",),
                    allowed_instances=("testing",),
                ),
            ),
        )

        self.assertEqual(evaluation.status, "pass")
        self.assertEqual(evaluation.findings, ())

    def test_unrelated_context_binding_does_not_satisfy_target(self) -> None:
        evaluation = evaluate_runtime_key_safety(
            target=RuntimeKeySafetyTarget(
                context="opw",
                instance="prod",
                environment_class="prod",
            ),
            required_binding_keys=("ODOO_ADMIN_PASSWORD",),
            secret_bindings=(
                _binding(
                    binding_key="ODOO_ADMIN_PASSWORD",
                    binding_id="binding-cm-admin-password",
                    secret_id="secret-cm-admin-password",
                ).model_copy(update={"context": "cm", "instance": "prod"}),
            ),
            secret_rules=(
                RuntimeSecretSafetyRule(
                    binding_key="ODOO_ADMIN_PASSWORD",
                    secret_class="shared_safe",
                    allowed_contexts=("cm", "opw"),
                    allowed_instances=("testing", "prod"),
                ),
            ),
        )

        self.assertEqual(evaluation.status, "fail")
        self.assertEqual(evaluation.findings[0].code, "binding_missing")
        self.assertEqual(evaluation.findings[0].binding_key, "ODOO_ADMIN_PASSWORD")

    def test_global_binding_satisfies_allowed_shared_target(self) -> None:
        evaluation = evaluate_runtime_key_safety(
            target=RuntimeKeySafetyTarget(
                context="cm",
                instance="prod",
                environment_class="prod",
            ),
            required_binding_keys=("ODOO_DB_PASSWORD",),
            secret_bindings=(
                _binding(
                    binding_key="ODOO_DB_PASSWORD",
                    binding_id="binding-global-db-password",
                    secret_id="secret-global-db-password",
                ).model_copy(update={"context": "", "instance": ""}),
            ),
            secret_rules=(
                RuntimeSecretSafetyRule(
                    binding_key="ODOO_DB_PASSWORD",
                    secret_class="shared_safe",
                    allowed_contexts=("cm", "opw"),
                    allowed_instances=("testing", "prod"),
                ),
            ),
        )

        self.assertEqual(evaluation.status, "pass")
        self.assertEqual(evaluation.findings, ())

    def test_context_binding_takes_precedence_over_global_binding(self) -> None:
        evaluation = evaluate_runtime_key_safety(
            target=RuntimeKeySafetyTarget(
                context="cm",
                instance="prod",
                environment_class="prod",
            ),
            required_binding_keys=("ODOO_DB_PASSWORD",),
            secret_bindings=(
                _binding(
                    binding_key="ODOO_DB_PASSWORD",
                    binding_id="binding-global-db-password",
                    secret_id="secret-global-db-password",
                ).model_copy(update={"context": "", "instance": ""}),
                _binding(
                    binding_key="ODOO_DB_PASSWORD",
                    binding_id="binding-context-db-password",
                    secret_id="secret-context-db-password",
                ).model_copy(update={"context": "cm", "instance": ""}),
            ),
            secret_rules=(
                RuntimeSecretSafetyRule(
                    binding_key="ODOO_DB_PASSWORD",
                    secret_class="prod_only",
                    allowed_contexts=("cm",),
                    allowed_instances=("prod",),
                ),
            ),
        )

        self.assertEqual(evaluation.status, "pass")
        self.assertEqual(evaluation.findings, ())

    def test_equally_specific_duplicate_bindings_remain_ambiguous(self) -> None:
        evaluation = evaluate_runtime_key_safety(
            target=RuntimeKeySafetyTarget(
                context="opw",
                instance="testing",
                environment_class="testing",
            ),
            required_binding_keys=("SHOPIFY_ACCESS_TOKEN",),
            secret_bindings=(
                _binding(
                    binding_key="SHOPIFY_ACCESS_TOKEN",
                    binding_id="binding-first-token",
                    secret_id="secret-first-token",
                ),
                _binding(
                    binding_key="SHOPIFY_ACCESS_TOKEN",
                    binding_id="binding-second-token",
                    secret_id="secret-second-token",
                ),
            ),
            secret_rules=(
                RuntimeSecretSafetyRule(
                    binding_key="SHOPIFY_ACCESS_TOKEN",
                    secret_class="testing",
                ),
            ),
        )

        self.assertEqual(evaluation.status, "fail")
        self.assertEqual(evaluation.findings[0].code, "ambiguous_binding")

    def test_context_and_instance_restrictions_fail_closed(self) -> None:
        evaluation = evaluate_runtime_key_safety(
            target=RuntimeKeySafetyTarget(
                context="opw",
                instance="testing",
                environment_class="testing",
            ),
            required_binding_keys=("SHOPIFY_ACCESS_TOKEN",),
            secret_bindings=(_binding(binding_key="SHOPIFY_ACCESS_TOKEN"),),
            secret_rules=(
                RuntimeSecretSafetyRule(
                    binding_key="SHOPIFY_ACCESS_TOKEN",
                    secret_class="testing",
                    allowed_contexts=("cm",),
                    allowed_instances=("preview",),
                ),
            ),
        )

        self.assertEqual(evaluation.status, "fail")
        self.assertEqual(
            [finding.code for finding in evaluation.findings],
            ["context_not_allowed", "instance_not_allowed"],
        )

    def test_policy_record_rejects_duplicate_binding_keys(self) -> None:
        with self.assertRaisesRegex(ValueError, "unique by binding_key"):
            RuntimeKeySafetyPolicyRecord(
                record_id="runtime-key-safety-policy-test",
                source="test",
                updated_at="2026-05-05T20:00:00Z",
                rules=(
                    RuntimeSecretSafetyRule(
                        binding_key="SHOPIFY_ACCESS_TOKEN",
                        secret_class="testing",
                    ),
                    RuntimeSecretSafetyRule(
                        binding_key="SHOPIFY_ACCESS_TOKEN",
                        secret_class="preview",
                    ),
                ),
            )

    def test_policy_sha256_ignores_record_metadata(self) -> None:
        first_record = RuntimeKeySafetyPolicyRecord(
            record_id="runtime-key-safety-policy-first",
            source="test:first",
            updated_at="2026-05-05T20:00:00Z",
            rules=(
                RuntimeSecretSafetyRule(
                    binding_key="SHOPIFY_ACCESS_TOKEN",
                    secret_class="testing",
                ),
            ),
        )
        second_record = first_record.model_copy(
            update={
                "record_id": "runtime-key-safety-policy-second",
                "source": "test:second",
                "updated_at": "2026-05-05T21:00:00Z",
            }
        )

        self.assertEqual(first_record.policy_sha256, second_record.policy_sha256)

    def test_evaluate_from_store_uses_latest_active_policy_and_target_bindings(self) -> None:
        policy = RuntimeKeySafetyPolicyRecord(
            record_id="runtime-key-safety-policy-20260505T200000Z-test",
            status="active",
            source="test",
            updated_at="2026-05-05T20:00:00Z",
            rules=(
                RuntimeSecretSafetyRule(
                    binding_key="SHOPIFY_ACCESS_TOKEN",
                    secret_class="testing",
                    allowed_contexts=("opw",),
                    allowed_instances=("testing",),
                ),
            ),
        )
        store = _FakeRuntimeKeySafetyStore(
            policies=(policy,),
            bindings=(
                _binding(binding_key="SHOPIFY_ACCESS_TOKEN"),
                _binding(
                    binding_key="SHOPIFY_ACCESS_TOKEN",
                    binding_id="binding-other-token",
                    secret_id="secret-other-token",
                ).model_copy(update={"context": "other", "instance": "testing"}),
            ),
        )

        evaluation = evaluate_runtime_key_safety_from_store(
            record_store=store,
            target=RuntimeKeySafetyTarget(
                context="opw",
                instance="testing",
                environment_class="testing",
            ),
            required_binding_keys=("SHOPIFY_ACCESS_TOKEN",),
        )

        self.assertEqual(evaluation.status, "pass")
        self.assertEqual(store.requested_context, "")
        self.assertEqual(store.requested_instance, "")

    def test_evaluate_from_store_allows_global_binding_candidates(self) -> None:
        policy = RuntimeKeySafetyPolicyRecord(
            record_id="runtime-key-safety-policy-20260505T200000Z-test",
            status="active",
            source="test",
            updated_at="2026-05-05T20:00:00Z",
            rules=(
                RuntimeSecretSafetyRule(
                    binding_key="ODOO_DB_PASSWORD",
                    secret_class="shared_safe",
                    allowed_contexts=("cm", "opw"),
                    allowed_instances=("testing", "prod"),
                ),
            ),
        )
        store = _FakeRuntimeKeySafetyStore(
            policies=(policy,),
            bindings=(
                _binding(
                    binding_key="ODOO_DB_PASSWORD",
                    binding_id="binding-global-db-password",
                    secret_id="secret-global-db-password",
                ).model_copy(update={"context": "", "instance": ""}),
            ),
        )

        evaluation = evaluate_runtime_key_safety_from_store(
            record_store=store,
            target=RuntimeKeySafetyTarget(
                context="cm",
                instance="prod",
                environment_class="prod",
            ),
            required_binding_keys=("ODOO_DB_PASSWORD",),
        )

        self.assertEqual(evaluation.status, "pass")
        self.assertEqual(evaluation.findings, ())
        self.assertEqual(store.requested_context, "")
        self.assertEqual(store.requested_instance, "")

    def test_missing_active_policy_fails_closed(self) -> None:
        store = _FakeRuntimeKeySafetyStore(policies=(), bindings=())

        with self.assertRaisesRegex(ValueError, "No active runtime key-safety policy"):
            latest_active_runtime_key_safety_policy(store)


if __name__ == "__main__":
    unittest.main()
