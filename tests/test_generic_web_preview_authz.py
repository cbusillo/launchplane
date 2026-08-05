import unittest

from control_plane.generic_web_preview_authz import (
    GENERIC_WEB_PREVIEW_MANAGED_SET_ID,
    GenericWebPreviewAuthzPlanRequest,
    build_generic_web_preview_authz_reconcile_request,
    generic_web_preview_rules,
)
from control_plane.service_auth import LaunchplaneAuthzPolicy


def _request(**overrides: str) -> GenericWebPreviewAuthzPlanRequest:
    values = {
        "target_product": "demo-web",
        "repository": "example/demo-web",
        "repository_id": "123",
        "repository_owner_id": "456",
        "default_branch": "main",
        "preview_context": "demo-web-preview",
        "launchplane_sha": "a" * 40,
        "reason": "Onboard demo web.",
        "related_issue": "#1970",
    }
    values.update(overrides)
    return GenericWebPreviewAuthzPlanRequest.model_validate(values)


class GenericWebPreviewAuthzTests(unittest.TestCase):
    def test_onboarding_generates_exact_six_rule_contract(self) -> None:
        rules = generic_web_preview_rules(_request())

        self.assertEqual(len(rules), 6)
        self.assertEqual(
            {rule.actions[0] for rule in rules},
            {
                "authz_diagnostic.evaluate",
                "preview_destroy.execute",
                "preview_generation.write",
                "preview_pr_feedback.write",
                "preview_refresh.execute",
            },
        )
        self.assertEqual(
            sum(rule.actions == ("preview_pr_feedback.write",) for rule in rules),
            2,
        )
        for rule in rules:
            self.assertEqual(rule.managed_set_id, GENERIC_WEB_PREVIEW_MANAGED_SET_ID)
            self.assertEqual(rule.repository, "example/demo-web")
            self.assertEqual(rule.repository_id, "123")
            self.assertEqual(rule.repository_owner_id, "456")
            self.assertEqual(rule.products, ("demo-web",))
            self.assertEqual(rule.contexts, ("demo-web-preview",))
            self.assertEqual(len(rule.workflow_refs), 1)
            self.assertEqual(len(rule.job_workflow_refs), 1)
            self.assertTrue(rule.job_workflow_refs[0].endswith("@" + "a" * 40))

    def test_onboard_preserves_other_products_and_adds_target(self) -> None:
        other_request = _request(
            target_product="other-web",
            repository="example/other-web",
            repository_id="789",
            preview_context="other-web-preview",
        )
        current_policy = LaunchplaneAuthzPolicy(
            schema_version=2,
            github_actions=generic_web_preview_rules(other_request),
        )

        reconcile = build_generic_web_preview_authz_reconcile_request(
            current_policy=current_policy,
            request=_request(),
        )

        self.assertEqual(len(reconcile.desired_policy.github_actions), 12)
        self.assertEqual(
            {rule.products[0] for rule in reconcile.desired_policy.github_actions},
            {"demo-web", "other-web"},
        )

    def test_onboard_rejects_existing_product_rules(self) -> None:
        request = _request()
        current_policy = LaunchplaneAuthzPolicy(
            schema_version=2,
            github_actions=generic_web_preview_rules(request),
        )

        with self.assertRaisesRegex(ValueError, "no current managed preview rules"):
            build_generic_web_preview_authz_reconcile_request(
                current_policy=current_policy,
                request=request,
            )

    def test_expand_rejects_missing_product_rules(self) -> None:
        with self.assertRaisesRegex(ValueError, "expand requires current product rules"):
            build_generic_web_preview_authz_reconcile_request(
                current_policy=LaunchplaneAuthzPolicy(schema_version=2),
                request=_request(operation="expand"),
            )

    def test_expand_then_contract_supports_sha_rotation(self) -> None:
        original = _request()
        current_policy = LaunchplaneAuthzPolicy(
            schema_version=2,
            github_actions=generic_web_preview_rules(original),
        )
        expanded = build_generic_web_preview_authz_reconcile_request(
            current_policy=current_policy,
            request=_request(operation="expand", launchplane_sha="b" * 40),
        )
        self.assertEqual(len(expanded.desired_policy.github_actions), 12)

        contracted = build_generic_web_preview_authz_reconcile_request(
            current_policy=expanded.desired_policy,
            request=_request(operation="contract", launchplane_sha="b" * 40),
        )
        self.assertEqual(len(contracted.desired_policy.github_actions), 6)
        self.assertTrue(
            all(
                rule.job_workflow_refs[0].endswith("@" + "b" * 40)
                for rule in contracted.desired_policy.github_actions
            )
        )

    def test_retire_removes_only_target_product(self) -> None:
        target = _request()
        other = _request(
            target_product="other-web",
            repository="example/other-web",
            repository_id="789",
            preview_context="other-web-preview",
        )
        current_policy = LaunchplaneAuthzPolicy(
            schema_version=2,
            github_actions=(*generic_web_preview_rules(target), *generic_web_preview_rules(other)),
        )

        retired = build_generic_web_preview_authz_reconcile_request(
            current_policy=current_policy,
            request=_request(operation="retire"),
        )

        self.assertEqual(len(retired.desired_policy.github_actions), 6)
        self.assertEqual(
            {rule.products[0] for rule in retired.desired_policy.github_actions},
            {"other-web"},
        )


if __name__ == "__main__":
    unittest.main()
