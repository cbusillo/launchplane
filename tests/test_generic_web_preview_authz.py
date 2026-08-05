import unittest
from pathlib import Path

from control_plane.generic_web_preview_authz import (
    GENERIC_WEB_PREVIEW_MANAGED_SET_ID,
    GenericWebPreviewAuthzPlanRequest,
    build_generic_web_preview_authz_reconcile_request,
    generic_web_preview_ingress_operator_rules,
    generic_web_preview_rules,
)
from control_plane.service_auth import GitHubActionsPolicyRule, LaunchplaneAuthzPolicy


def _request(**overrides: object) -> GenericWebPreviewAuthzPlanRequest:
    values: dict[str, object] = {
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


def _ingress_templates() -> tuple[GitHubActionsPolicyRule, ...]:
    common = {
        "repository": "cbusillo/launchplane",
        "repository_id": "999",
        "repository_owner_id": "456",
        "job_workflow_refs": (
            "cbusillo/launchplane/.github/workflows/reusable-ingress-route-dry-run.yml@" + "b" * 40,
        ),
        "event_names": ("workflow_dispatch",),
        "products": ("template",),
        "contexts": ("template",),
    }
    return (
        GitHubActionsPolicyRule(
            **common,
            workflow_refs=(
                "cbusillo/launchplane/.github/workflows/ingress-route-dry-run.yml@refs/heads/main",
            ),
            actions=("ingress_route.plan",),
        ),
        GitHubActionsPolicyRule(
            **{
                **common,
                "job_workflow_refs": (
                    "cbusillo/launchplane/.github/workflows/"
                    "reusable-ingress-route-apply.yml@" + "c" * 40,
                ),
            },
            workflow_refs=(
                "cbusillo/launchplane/.github/workflows/ingress-route-apply.yml@refs/heads/main",
            ),
            actions=("ingress_route.apply",),
        ),
    )


class GenericWebPreviewAuthzTests(unittest.TestCase):
    def test_operator_workflow_exposes_scoped_ingress_expansion(self) -> None:
        workflow_text = Path(".github/workflows/generic-web-preview-authorization.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn("include_ingress_operator", workflow_text)
        self.assertIn("launchplane_sha", workflow_text)

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
            self.assertIn(len(rule.job_workflow_refs), {1, 2})
            self.assertTrue(
                all(
                    job_workflow_ref.endswith("@" + "a" * 40)
                    for job_workflow_ref in rule.job_workflow_refs
                )
            )

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

    def test_onboard_replays_identical_existing_product_rules(self) -> None:
        request = _request()
        current_policy = LaunchplaneAuthzPolicy(
            schema_version=2,
            github_actions=generic_web_preview_rules(request),
        )

        reconcile = build_generic_web_preview_authz_reconcile_request(
            current_policy=current_policy,
            request=request,
        )

        self.assertEqual(len(reconcile.desired_policy.github_actions), 6)

    def test_onboard_rejects_different_existing_product_rules(self) -> None:
        current_policy = LaunchplaneAuthzPolicy(
            schema_version=2,
            github_actions=generic_web_preview_rules(_request()),
        )

        with self.assertRaisesRegex(ValueError, "use expand and contract"):
            build_generic_web_preview_authz_reconcile_request(
                current_policy=current_policy,
                request=_request(launchplane_sha="b" * 40),
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

    def test_expand_and_contract_support_temporary_ingress_operator_rules(self) -> None:
        request = _request()
        current_policy = LaunchplaneAuthzPolicy(
            schema_version=2,
            github_actions=(*generic_web_preview_rules(request), *_ingress_templates()),
        )

        ingress_rules = generic_web_preview_ingress_operator_rules(
            current_policy=current_policy,
            request=_request(include_ingress_operator=True),
        )
        self.assertEqual(len(ingress_rules), 2)
        self.assertEqual(
            {rule.actions[0] for rule in ingress_rules},
            {"ingress_route.apply", "ingress_route.plan"},
        )
        for rule in ingress_rules:
            self.assertEqual(rule.repository, "cbusillo/launchplane")
            self.assertEqual(rule.repository_id, "999")
            self.assertEqual(rule.repository_owner_id, "456")
            self.assertEqual(rule.products, ("demo-web",))
            self.assertEqual(rule.contexts, ("demo-web-preview",))
            self.assertEqual(rule.event_names, ("workflow_dispatch",))
            self.assertTrue(rule.job_workflow_refs)

        expanded = build_generic_web_preview_authz_reconcile_request(
            current_policy=current_policy,
            request=_request(operation="expand", include_ingress_operator=True),
        )
        self.assertEqual(len(expanded.desired_policy.github_actions), 8)

        contracted = build_generic_web_preview_authz_reconcile_request(
            current_policy=expanded.desired_policy,
            request=_request(operation="contract"),
        )
        self.assertEqual(len(contracted.desired_policy.github_actions), 6)
        self.assertFalse(
            any(
                rule.actions[0].startswith("ingress_route.")
                for rule in contracted.desired_policy.github_actions
            )
        )

    def test_ingress_operator_expansion_requires_pinned_templates(self) -> None:
        with self.assertRaisesRegex(ValueError, "requires one unambiguous pinned"):
            build_generic_web_preview_authz_reconcile_request(
                current_policy=LaunchplaneAuthzPolicy(
                    schema_version=2,
                    github_actions=generic_web_preview_rules(_request()),
                ),
                request=_request(operation="expand", include_ingress_operator=True),
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
