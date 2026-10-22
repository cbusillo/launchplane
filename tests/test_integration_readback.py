from __future__ import annotations

import unittest
from types import SimpleNamespace

from control_plane.contracts.dokploy_target_record import (
    DokployTargetIntegrationAllowance,
    DokployTargetPolicies,
    DokployTargetShopifyPolicy,
    DokployTargetRecord,
)
from control_plane.contracts.runtime_key_safety_policy import (
    RuntimeKeySafetyPolicyRecord,
    RuntimeSecretSafetyRule,
)
from control_plane.contracts.lane_summary import LaunchplaneLaneSummary
from control_plane.contracts.secret_record import SecretBinding
from control_plane.dokploy.api import JsonValue
from control_plane.dokploy.post_deploy import (
    OdooPostDeployReadbackFailure,
    extract_odoo_post_deploy_readback_markers,
    require_integration_readback_evidence,
)
from control_plane.integration_readback import (
    integration_readback_policy,
    integration_credential_warnings,
)


def _policies(*allowed: str, protected: tuple[str, ...] = ()) -> DokployTargetPolicies:
    return DokployTargetPolicies(
        shopify=DokployTargetShopifyPolicy(protected_store_keys=protected),
        integration_allowances=tuple(
            DokployTargetIntegrationAllowance(
                integration=integration, kind="pre_live", reason="Testing is the working site."
            )
            for integration in allowed
        ),
    )


def _integrations(policy_families: tuple[object, ...]) -> set[str]:
    return {getattr(family, "integration") for family in policy_families}


class IntegrationReadbackPolicyTests(unittest.TestCase):
    def test_mail_payment_and_web_push_credentials_warn_without_allowances(self) -> None:
        for key, integration in (
            ("SMTP_PASSWORD", "outgoing_mail"),
            ("SENDGRID_API_KEY", "outgoing_mail"),
            ("STRIPE_SECRET_KEY", "payment"),
            ("VAPID_PRIVATE_KEY", "web_push"),
            ("ODOO_OVERRIDE_SECRET__CONFIG_PARAMETER__MAIL_WEB_PUSH_VAPID_PRIVATE_KEY", "web_push"),
            ("INCOMING_MAIL_PASSWORD", "incoming_mail"),
        ):
            with self.subTest(key=key):
                binding = SecretBinding(
                    binding_id="binding",
                    secret_id="secret",
                    integration="runtime_environment",
                    binding_key=key,
                    context="example",
                    instance="testing",
                    created_at="2026-10-21T00:00:00Z",
                    updated_at="2026-10-21T00:00:00Z",
                )
                summary = LaunchplaneLaneSummary(
                    context="example", instance="testing", secret_bindings=(binding,)
                )
                self.assertTrue(integration_credential_warnings(summary))
                target = DokployTargetRecord(
                    context="example",
                    instance="testing",
                    policies=_policies(integration),
                    updated_at=binding.updated_at,
                )
                self.assertFalse(
                    integration_credential_warnings(
                        summary.model_copy(update={"dokploy_target": target})
                    )
                )

    def test_custom_policy_marker_warns_when_integration_cannot_be_matched(self) -> None:
        cases = (
            ("ACCOUNTING_QB_TOKEN", ("__ACCOUNTING__", "QB"), ("accounting",), True),
            ("ACCOUNTING_QB_TOKEN", ("__ACCOUNTING__", "QB"), ("accounting", "qb"), False),
            ("SHOPIFY_ACCOUNTING_TOKEN", ("ACCOUNTING",), ("shopify",), True),
            ("SHOPIFY_ACCOUNTING_TOKEN", ("ACCOUNTING",), ("shopify", "accounting"), False),
            ("SMTP_PASSWORD", ("SMTP",), ("outgoing_mail",), False),
            ("MAIL_WEB_PUSH_VAPID_PRIVATE_KEY", ("MAIL",), ("web_push",), False),
            ("123VENDOR_TOKEN", ("123VENDOR",), (), True),
            ("ACCOUNTING_TOKEN", (), (), False),
        )
        for key, markers, allowed, warned in cases:
            with self.subTest(key=key, markers=markers, allowed=allowed):
                binding = SecretBinding(
                    binding_id="binding",
                    secret_id="secret",
                    integration="runtime_environment",
                    binding_key=key,
                    context="example",
                    instance="testing",
                    created_at="2026-10-21T00:00:00Z",
                    updated_at="2026-10-21T00:00:00Z",
                )
                policy = RuntimeKeySafetyPolicyRecord(
                    record_id="policy",
                    source="test",
                    updated_at=binding.updated_at,
                    rules=(RuntimeSecretSafetyRule(binding_key=key, secret_class="testing"),),
                    integration_key_markers=markers,
                )
                store = SimpleNamespace(
                    list_runtime_key_safety_policy_records=lambda **_: (policy,)
                )
                target = DokployTargetRecord(
                    context="example",
                    instance="testing",
                    policies=_policies(*allowed),
                    updated_at=binding.updated_at,
                )
                summary = LaunchplaneLaneSummary(
                    context="example",
                    instance="testing",
                    secret_bindings=(binding,),
                    dokploy_target=target,
                )
                self.assertEqual(
                    bool(integration_credential_warnings(summary, record_store=store)), warned
                )

    def test_credential_warnings_use_configured_in_scope_integrations_only(self) -> None:
        binding = SecretBinding(
            binding_id="binding",
            secret_id="secret",
            integration="runtime_environment",
            binding_key="ODOO_OVERRIDE_SECRET__ADDON__SHOPIFY__API_TOKEN",
            context="example",
            instance="testing",
            created_at="2026-10-21T00:00:00Z",
            updated_at="2026-10-21T00:00:00Z",
        )
        summary = LaunchplaneLaneSummary(
            context="example", instance="testing", secret_bindings=(binding,)
        )
        self.assertTrue(integration_credential_warnings(summary))
        for update in (
            {"status": "disabled"},
            {"context": "other"},
            {"instance": "prod"},
            {"binding_key": "DATABASE_PASSWORD"},
            {"binding_key": "NOTSHOPIFY_TOKEN"},
            {"binding_key": "WEB_PUSH_TOKEN"},
            {"integration": "testing_application_state"},
            {"integration": "odoo_walkthrough"},
            {"integration": "odoo_diagnostics"},
        ):
            with self.subTest(update=update):
                self.assertFalse(
                    integration_credential_warnings(
                        summary.model_copy(
                            update={"secret_bindings": (binding.model_copy(update=update),)}
                        )
                    )
                )
        self.assertFalse(
            integration_credential_warnings(summary.model_copy(update={"instance": "prod"}))
        )
        self.assertTrue(
            integration_credential_warnings(
                summary.model_copy(
                    update={
                        "secret_bindings": (
                            binding.model_copy(
                                update={"integration": "shopify", "binding_key": "API_TOKEN"}
                            ),
                        )
                    }
                )
            )
        )

    def test_non_production_lanes_check_every_family_and_carry_allowances(self) -> None:
        for instance in ("testing", "dev", "qa-2"):
            with self.subTest(instance=instance):
                policy = integration_readback_policy(
                    instance_name=instance,
                    policies=_policies("printnode"),
                    workflow_mode="maintenance",
                )

                self.assertTrue(policy.required)
                self.assertIn("payment", _integrations(policy.families))
                self.assertEqual(policy.allowed_integrations, ("printnode",))

    def test_odoo_generated_settings_are_checked_only_after_a_restore(self) -> None:
        restore = integration_readback_policy(
            instance_name="testing", policies=_policies(), workflow_mode="restore"
        )
        for mode in ("maintenance", "bootstrap"):
            with self.subTest(mode=mode):
                later = integration_readback_policy(
                    instance_name="testing", policies=_policies(), workflow_mode=mode
                )
                self.assertNotIn("web_push", _integrations(later.families))
        self.assertIn("web_push", _integrations(restore.families))

    def test_production_checks_only_protected_store_keys(self) -> None:
        unprotected = integration_readback_policy(
            instance_name="prod", policies=_policies(), workflow_mode="restore"
        )
        protected = integration_readback_policy(
            instance_name="prod",
            policies=_policies(protected=("https://Example-Store.myshopify.com/admin",)),
            workflow_mode="maintenance",
        )

        self.assertFalse(unprotected.required)
        self.assertEqual(protected.families, ())
        self.assertTrue(protected.required)
        self.assertEqual(protected.protected_shopify_store_handles, ("example-store",))

    def test_preview_gets_the_full_check_with_no_inherited_allowance(self) -> None:
        # The preview driver builds the preview's target definition without the
        # template lane's policies, so a template's pre_live allowance never applies.
        policy = integration_readback_policy(
            instance_name="pr-45", policies=DokployTargetPolicies(), workflow_mode="maintenance"
        )

        self.assertTrue(policy.required)
        self.assertIn("shopify", _integrations(policy.families))
        self.assertEqual(policy.allowed_integrations, ())


class IntegrationReadbackEvidenceTests(unittest.TestCase):
    def test_success_requires_the_schedule_to_prove_the_readback_passed(self) -> None:
        policy = integration_readback_policy(
            instance_name="testing", policies=_policies(), workflow_mode="restore"
        )
        refused_logs = [
            "integration_readback_refused=printnode/printnode.api_key,payment/payment_provider",
            "integration_readback_ok=false",
        ]
        for logs in ([], ["integration_readback_ok=false"], refused_logs):
            with self.subTest(logs=logs):
                evidence = {
                    "log_available": "true",
                    **extract_odoo_post_deploy_readback_markers({"logs": list[JsonValue](logs)}),
                }
                with self.assertRaises(OdooPostDeployReadbackFailure) as raised:
                    require_integration_readback_evidence(evidence, policy)
                if logs == refused_logs:
                    self.assertIn(
                        "refused printnode/printnode.api_key, payment/payment_provider",
                        str(raised.exception),
                    )
                    self.assertEqual(
                        raised.exception.evidence["integration_readback_refused"],
                        "printnode/printnode.api_key,payment/payment_provider",
                    )

        require_integration_readback_evidence(
            {"log_available": "true", "integration_readback_ok": "true"}, policy
        )

    def test_lane_without_a_readback_needs_no_proof(self) -> None:
        policy = integration_readback_policy(
            instance_name="prod", policies=_policies(), workflow_mode="maintenance"
        )

        require_integration_readback_evidence({"log_available": "false"}, policy)


if __name__ == "__main__":
    unittest.main()
