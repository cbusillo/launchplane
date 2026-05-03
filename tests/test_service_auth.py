import unittest
from types import SimpleNamespace
from typing import Literal
from unittest.mock import Mock, patch

from control_plane.service_auth import (
    GitHubActionsIdentity,
    GitHubActionsPolicyRule,
    GitHubHumanIdentity,
    GitHubHumanPolicyRule,
    GitHubOidcVerifier,
    LaunchplaneAuthzPolicy,
)


def _actions_identity(
    *,
    repository: str = "cbusillo/site",
    repository_owner: str = "cbusillo",
    workflow_ref: str = "cbusillo/site/.github/workflows/deploy.yml@refs/heads/main",
    job_workflow_ref: str = "",
    ref: str = "refs/heads/main",
    ref_type: str = "branch",
    event_name: str = "workflow_dispatch",
    environment: str = "production",
    subject: str = "repo:cbusillo/site:environment:production",
    sha: str = "abc123",
    raw_claims: dict[str, object] | None = None,
) -> GitHubActionsIdentity:
    return GitHubActionsIdentity(
        repository=repository,
        repository_owner=repository_owner,
        workflow_ref=workflow_ref,
        job_workflow_ref=job_workflow_ref,
        ref=ref,
        ref_type=ref_type,
        event_name=event_name,
        environment=environment,
        subject=subject,
        sha=sha,
        raw_claims=raw_claims or {},
    )


def _human_identity(
    *,
    login: str = "operator",
    github_id: int = 123,
    name: str = "Operator",
    email: str = "operator@example.com",
    organizations: frozenset[str] = frozenset({"cbusillo"}),
    teams: frozenset[str] = frozenset({"launchplane-admins"}),
    role: Literal["read_only", "admin"] = "admin",
) -> GitHubHumanIdentity:
    return GitHubHumanIdentity(
        login=login,
        github_id=github_id,
        name=name,
        email=email,
        organizations=organizations,
        teams=teams,
        role=role,
    )


class GitHubOidcVerifierTests(unittest.TestCase):
    def test_verifier_requires_non_empty_configuration(self) -> None:
        with self.assertRaises(ValueError):
            GitHubOidcVerifier(audience=" ", jwk_client=Mock())
        with self.assertRaises(ValueError):
            GitHubOidcVerifier(audience="launchplane", issuer=" ", jwk_client=Mock())
        with self.assertRaises(ValueError):
            GitHubOidcVerifier(audience="launchplane", jwks_url=" ")

    def test_verify_requires_repository_and_workflow_ref_claims(self) -> None:
        jwk_client = Mock()
        jwk_client.get_signing_key_from_jwt.return_value = SimpleNamespace(key="signing-key")
        verifier = GitHubOidcVerifier(audience="launchplane", jwk_client=jwk_client)

        with patch("control_plane.service_auth.jwt.decode", return_value={}):
            with self.assertRaises(ValueError):
                verifier.verify("header.payload.signature")

        with patch(
            "control_plane.service_auth.jwt.decode",
            return_value={"repository": "cbusillo/site"},
        ):
            with self.assertRaises(ValueError):
                verifier.verify("header.payload.signature")

    def test_verify_returns_normalized_github_actions_identity(self) -> None:
        jwk_client = Mock()
        jwk_client.get_signing_key_from_jwt.return_value = SimpleNamespace(key="signing-key")
        claims = {
            "repository": " cbusillo/site ",
            "repository_owner": " cbusillo ",
            "workflow_ref": " cbusillo/site/.github/workflows/deploy.yml@refs/heads/main ",
            "event_name": "workflow_dispatch",
        }

        with patch("control_plane.service_auth.jwt.decode", return_value=claims):
            identity = GitHubOidcVerifier(audience=" launchplane ", jwk_client=jwk_client).verify(
                " token "
            )

        jwk_client.get_signing_key_from_jwt.assert_called_once_with("token")
        self.assertEqual(identity.repository, "cbusillo/site")
        self.assertEqual(
            identity.workflow_ref, "cbusillo/site/.github/workflows/deploy.yml@refs/heads/main"
        )
        self.assertEqual(identity.raw_claims, claims)


class LaunchplaneAuthzPolicyTests(unittest.TestCase):
    def test_github_actions_policy_matches_patterns_and_scopes(self) -> None:
        rule = GitHubActionsPolicyRule(
            repository="cbusillo/site",
            workflow_refs=("cbusillo/site/.github/workflows/*.yml@refs/heads/main",),
            event_names=("workflow_dispatch",),
            refs=("refs/heads/main",),
            environments=("production",),
            products=("site",),
            contexts=("site-prod",),
            actions=("generic_web_prod_promotion.execute",),
        )
        identity = _actions_identity()

        self.assertTrue(
            rule.allows(
                identity=identity,
                action="generic_web_prod_promotion.execute",
                product="site",
                context="site-prod",
            )
        )
        self.assertFalse(
            rule.allows(
                identity=identity,
                action="generic_web_prod_promotion.execute",
                product="other-site",
                context="site-prod",
            )
        )

    def test_human_policy_matches_team_and_role_scope(self) -> None:
        rule = GitHubHumanPolicyRule(
            teams=("launchplane-*",),
            roles=("admin",),
            products=("site",),
            contexts=("site-prod",),
            actions=("product_config.write",),
        )
        identity = _human_identity()

        self.assertTrue(
            rule.allows(
                identity=identity,
                action="product_config.write",
                product="site",
                context="site-prod",
            )
        )
        self.assertFalse(
            rule.allows(
                identity=_human_identity(role="read_only"),
                action="product_config.write",
                product="site",
                context="site-prod",
            )
        )

    def test_launchplane_policy_dispatches_by_identity_type(self) -> None:
        policy = LaunchplaneAuthzPolicy(
            github_actions=(
                GitHubActionsPolicyRule(
                    repository="cbusillo/site",
                    actions=("deploy.execute",),
                    products=("site",),
                    contexts=("site-prod",),
                ),
            ),
            github_humans=(
                GitHubHumanPolicyRule(
                    logins=("operator",),
                    roles=("admin",),
                    actions=("deploy.execute",),
                    products=("site",),
                    contexts=("site-prod",),
                ),
            ),
        )

        self.assertTrue(
            policy.allows(
                identity=_actions_identity(),
                action="deploy.execute",
                product="site",
                context="site-prod",
            )
        )
        self.assertTrue(
            policy.allows(
                identity=_human_identity(),
                action="deploy.execute",
                product="site",
                context="site-prod",
            )
        )
        self.assertFalse(
            policy.allows(
                identity=_human_identity(login="viewer"),
                action="deploy.execute",
                product="site",
                context="site-prod",
            )
        )


if __name__ == "__main__":
    unittest.main()
