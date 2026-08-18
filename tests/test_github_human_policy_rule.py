from __future__ import annotations

from dataclasses import replace
import unittest

from control_plane.service_auth import GitHubHumanIdentity, GitHubHumanPolicyRule


def _identity() -> GitHubHumanIdentity:
    return GitHubHumanIdentity(
        login="alice",
        github_id=123,
        name="Alice Example",
        email="alice@example.com",
        organizations=frozenset({"cbusillo"}),
        teams=frozenset({"platform", "cbusillo/platform"}),
        role="admin",
    )


class GitHubHumanPolicyRuleTests(unittest.TestCase):
    def test_every_configured_principal_selector_must_match(self) -> None:
        identity = _identity()
        rule = GitHubHumanPolicyRule(
            github_ids=(identity.github_id,),
            logins=(identity.login,),
            organizations=("cbusillo",),
            teams=("cbusillo/platform",),
            roles=("admin",),
            products=("launchplane",),
            contexts=("launchplane",),
            actions=("authz_policy_grant.write",),
        )

        self.assertTrue(
            rule.matches_principal(
                github_id=identity.github_id,
                login=identity.login,
                organizations=identity.organizations,
                teams=identity.teams,
                role=identity.role,
            )
        )
        self.assertTrue(
            rule.allows(
                identity=identity,
                action="authz_policy_grant.write",
                product="launchplane",
                context="launchplane",
            )
        )

        mismatched_identities = (
            replace(identity, github_id=999),
            replace(identity, login="mallory"),
            replace(identity, organizations=frozenset({"another-org"})),
            replace(identity, teams=frozenset({"cbusillo/another-team"})),
            replace(identity, role="read_only"),
        )
        for mismatched_identity in mismatched_identities:
            with self.subTest(identity=mismatched_identity):
                self.assertFalse(
                    rule.matches_principal(
                        github_id=mismatched_identity.github_id,
                        login=mismatched_identity.login,
                        organizations=mismatched_identity.organizations,
                        teams=mismatched_identity.teams,
                        role=mismatched_identity.role,
                    )
                )

    def test_resource_and_action_scope_remain_separate_from_principal_match(self) -> None:
        identity = _identity()
        rule = GitHubHumanPolicyRule(
            github_ids=(identity.github_id,),
            products=("launchplane",),
            contexts=("launchplane",),
            actions=("authz_policy_grant.write",),
        )

        for action, product, context in (
            ("product_environment.read", "launchplane", "launchplane"),
            ("authz_policy_grant.write", "another-product", "launchplane"),
            ("authz_policy_grant.write", "launchplane", "another-context"),
        ):
            with self.subTest(action=action, product=product, context=context):
                self.assertTrue(
                    rule.matches_principal(
                        github_id=identity.github_id,
                        login=identity.login,
                        organizations=identity.organizations,
                        teams=identity.teams,
                        role=identity.role,
                    )
                )
                self.assertFalse(
                    rule.allows(
                        identity=identity,
                        action=action,
                        product=product,
                        context=context,
                    )
                )
