import unittest

from control_plane.contracts.runtime_key_safety_policy import (
    RuntimeKeySafetyPolicyRecord,
    RuntimeKeySafetyTarget,
    RuntimeSecretSafetyRule,
)
from control_plane.contracts.secret_record import SecretBinding
from control_plane.contracts.secret_record import SecretStatus
from control_plane.runtime_key_safety import (
    evaluate_runtime_key_safety,
    evaluate_runtime_key_safety_from_store,
    latest_active_runtime_key_safety_policy,
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
    status: SecretStatus = "configured",
) -> SecretBinding:
    return SecretBinding(
        binding_id=binding_id,
        secret_id=secret_id,
        integration="runtime_environment",
        binding_key=binding_key,
        context="opw",
        instance="testing",
        status=status,
        created_at="2026-05-05T20:00:00Z",
        updated_at="2026-05-05T20:00:00Z",
    )


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

    def test_unclassified_secret_fails_closed(self) -> None:
        evaluation = evaluate_runtime_key_safety(
            target=RuntimeKeySafetyTarget(
                context="opw",
                instance="testing",
                environment_class="testing",
            ),
            required_binding_keys=("SHOPIFY_ACCESS_TOKEN",),
            secret_bindings=(_binding(binding_key="SHOPIFY_ACCESS_TOKEN"),),
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
