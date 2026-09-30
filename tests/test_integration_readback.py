from __future__ import annotations

import unittest

from control_plane.contracts.dokploy_target_record import (
    DokployTargetIntegrationAllowance,
    DokployTargetPolicies,
    DokployTargetShopifyPolicy,
)
from control_plane.dokploy.api import JsonValue
from control_plane.dokploy.post_deploy import (
    OdooPostDeployReadbackFailure,
    extract_odoo_post_deploy_readback_markers,
    require_integration_readback_evidence,
)
from control_plane.integration_readback import integration_readback_policy


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

    def test_preview_is_left_to_the_preview_driver(self) -> None:
        policy = integration_readback_policy(
            instance_name="testing", policies=_policies(), workflow_mode="maintenance", preview=True
        )

        self.assertFalse(policy.required)


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
