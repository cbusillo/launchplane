import unittest

from control_plane.contracts.runtime_key_safety_policy import (
    RuntimeKeySafetyTarget,
    RuntimeSecretSafetyRule,
)
from control_plane.contracts.secret_record import SecretBinding
from control_plane.contracts.secret_record import SecretStatus
from control_plane.runtime_key_safety import evaluate_runtime_key_safety


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


if __name__ == "__main__":
    unittest.main()
