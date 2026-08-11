import unittest
from pathlib import Path

from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductImageProfile,
)
from control_plane.generic_web_preview_authz import (
    GENERIC_WEB_PREVIEW_MANAGED_SET_ID,
    GenericWebPreviewAuthzPlanRequest,
    build_generic_web_preview_authz_reconcile_request,
    generic_web_preview_ingress_operator_rules,
    generic_web_preview_rules,
    resolve_generic_web_preview_retirement_authority,
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
    return (
        GitHubActionsPolicyRule(
            repository="cbusillo/launchplane",
            repository_id="999",
            repository_owner_id="456",
            workflow_refs=(
                "cbusillo/launchplane/.github/workflows/ingress-route-dry-run.yml@refs/heads/main",
            ),
            job_workflow_refs=(
                "cbusillo/launchplane/.github/workflows/reusable-ingress-route-dry-run.yml@"
                + "b" * 40,
            ),
            event_names=("workflow_dispatch",),
            products=("template",),
            contexts=("template",),
            actions=("ingress_route.plan",),
        ),
        GitHubActionsPolicyRule(
            repository="cbusillo/launchplane",
            repository_id="999",
            repository_owner_id="456",
            workflow_refs=(
                "cbusillo/launchplane/.github/workflows/ingress-route-apply.yml@refs/heads/main",
            ),
            job_workflow_refs=(
                "cbusillo/launchplane/.github/workflows/reusable-ingress-route-apply.yml@"
                + "c" * 40,
            ),
            event_names=("workflow_dispatch",),
            products=("template",),
            contexts=("template",),
            actions=("ingress_route.apply",),
        ),
    )


def _profile(
    *,
    repository: str = "example/demo-web",
    repository_id: str = "123",
    repository_owner_id: str = "456",
) -> LaunchplaneProductProfileRecord:
    return LaunchplaneProductProfileRecord(
        product="demo-web",
        display_name="Demo Web",
        repository=repository,
        repository_id=repository_id,
        repository_owner_id=repository_owner_id,
        driver_id="generic-web",
        image=ProductImageProfile(),
        updated_at="2026-08-11T00:00:00Z",
        source="test",
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

    def test_retire_derives_complete_rule_identity_without_profile(self) -> None:
        target_rules = generic_web_preview_rules(_request())

        authority = resolve_generic_web_preview_retirement_authority(
            current_target_rules=target_rules,
            request=_request(
                operation="retire", repository="", repository_id="", repository_owner_id=""
            ),
            profile=None,
        )

        self.assertEqual(authority.repository, "example/demo-web")
        self.assertEqual(authority.evidence.authority_sources, ("current_product_rules",))
        self.assertEqual(authority.evidence.managed_rule_count, 6)
        self.assertEqual(len(authority.evidence.repository_identity_sha256), 64)
        evidence = authority.evidence.model_dump_json()
        self.assertNotIn('"123"', evidence)
        self.assertNotIn('"456"', evidence)

    def test_retire_ignores_ingress_identity_but_removes_ingress_rules(self) -> None:
        request = _request()
        target_rules = generic_web_preview_rules(request)
        ingress_rules = generic_web_preview_ingress_operator_rules(
            current_policy=LaunchplaneAuthzPolicy(
                schema_version=2,
                github_actions=(*target_rules, *_ingress_templates()),
            ),
            request=_request(include_ingress_operator=True),
        )
        other_rules = generic_web_preview_rules(
            _request(
                target_product="other-web",
                repository="example/other-web",
                repository_id="789",
                preview_context="other-web-preview",
            )
        )
        current_policy = LaunchplaneAuthzPolicy(
            schema_version=2,
            github_actions=(*target_rules, *ingress_rules, *other_rules),
        )

        retired = build_generic_web_preview_authz_reconcile_request(
            current_policy=current_policy,
            request=_request(
                operation="retire", repository="", repository_id="", repository_owner_id=""
            ),
        )

        self.assertEqual(len(retired.desired_policy.github_actions), 6)
        self.assertEqual(
            {rule.products for rule in retired.desired_policy.github_actions}, {("other-web",)}
        )

    def test_retire_legacy_name_only_rules_require_matching_complete_profile(self) -> None:
        target_rules = tuple(
            GitHubActionsPolicyRule.model_validate(
                {
                    **rule.model_dump(mode="json"),
                    "repository_id": "",
                    "repository_owner_id": "",
                }
            )
            for rule in generic_web_preview_rules(_request())
        )
        retire_request = _request(
            operation="retire", repository="", repository_id="", repository_owner_id=""
        )

        with self.assertRaisesRegex(ValueError, "matching product profile with complete"):
            resolve_generic_web_preview_retirement_authority(
                current_target_rules=target_rules,
                request=retire_request,
                profile=None,
            )

        authority = resolve_generic_web_preview_retirement_authority(
            current_target_rules=target_rules,
            request=retire_request,
            profile=_profile(),
        )

        self.assertEqual(authority.repository_id, "123")
        self.assertEqual(
            authority.evidence.authority_sources,
            ("current_product_rules", "product_profile"),
        )

    def test_retire_rejects_ambiguous_or_incomplete_rule_identity(self) -> None:
        target_rules = generic_web_preview_rules(_request())
        mismatched_rule = GitHubActionsPolicyRule.model_validate(
            {
                **target_rules[0].model_dump(mode="json"),
                "repository": "example/other-web",
                "repository_id": "789",
            }
        )
        retire_request = _request(
            operation="retire", repository="", repository_id="", repository_owner_id=""
        )

        with self.assertRaisesRegex(ValueError, "ambiguous repository identities"):
            resolve_generic_web_preview_retirement_authority(
                current_target_rules=(*target_rules[1:], mismatched_rule),
                request=retire_request,
                profile=None,
            )
        with self.assertRaisesRegex(ValueError, "after excluding ingress-operator"):
            resolve_generic_web_preview_retirement_authority(
                current_target_rules=generic_web_preview_ingress_operator_rules(
                    current_policy=LaunchplaneAuthzPolicy(
                        schema_version=2,
                        github_actions=(*target_rules, *_ingress_templates()),
                    ),
                    request=_request(include_ingress_operator=True),
                ),
                request=retire_request,
                profile=None,
            )

    def test_retire_does_not_mistake_a_product_rule_for_ingress_operator_authority(self) -> None:
        target_rules = generic_web_preview_rules(_request())
        ingress_trap = GitHubActionsPolicyRule.model_validate(
            {
                **target_rules[0].model_dump(mode="json"),
                "repository": "cbusillo/launchplane",
                "repository_id": "999",
                "actions": ["ingress_route.plan"],
            }
        )

        with self.assertRaisesRegex(ValueError, "ambiguous repository identities"):
            resolve_generic_web_preview_retirement_authority(
                current_target_rules=(*target_rules[1:], ingress_trap),
                request=_request(
                    operation="retire", repository="", repository_id="", repository_owner_id=""
                ),
                profile=None,
            )

    def test_retire_rejects_profile_or_caller_assertion_disagreement(self) -> None:
        target_rules = generic_web_preview_rules(_request())
        retire_request = _request(operation="retire")

        with self.assertRaisesRegex(ValueError, "profile and rule identity disagreement"):
            resolve_generic_web_preview_retirement_authority(
                current_target_rules=target_rules,
                request=retire_request,
                profile=_profile(repository_id="789"),
            )
        with self.assertRaisesRegex(ValueError, "assertion does not match authority"):
            build_generic_web_preview_authz_reconcile_request(
                current_policy=LaunchplaneAuthzPolicy(
                    schema_version=2,
                    github_actions=target_rules,
                ),
                request=_request(operation="retire", repository="example/other-web"),
            )
        with self.assertRaisesRegex(ValueError, "identity assertion does not match authority"):
            build_generic_web_preview_authz_reconcile_request(
                current_policy=LaunchplaneAuthzPolicy(
                    schema_version=2,
                    github_actions=target_rules,
                ),
                request=_request(
                    operation="retire", repository_id="789", repository_owner_id="456"
                ),
            )

    def test_retire_request_rejects_partial_assertions_and_ingress_expansion(self) -> None:
        with self.assertRaisesRegex(ValueError, "require both repository_id"):
            _request(operation="retire", repository_id="123", repository_owner_id="")
        with self.assertRaisesRegex(ValueError, "rejects include_ingress_operator=true"):
            _request(operation="retire", include_ingress_operator=True)

    def test_non_retire_requires_live_repository_identity(self) -> None:
        with self.assertRaisesRegex(ValueError, "requires repository"):
            _request(operation="onboard", repository="")
        with self.assertRaisesRegex(ValueError, "requires immutable repository identity"):
            _request(operation="expand", repository_id="", repository_owner_id="")


if __name__ == "__main__":
    unittest.main()
