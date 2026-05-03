from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from control_plane.service_auth import (
    GitHubActionsIdentity,
    GitHubActionsPolicyRule,
    GitHubHumanIdentity,
    GitHubHumanPolicyRule,
    GitHubOidcVerifier,
    LaunchplaneAuthzPolicy,
    parse_authz_policy_toml,
)
from control_plane.service_human_auth import (
    GitHubOAuthConfig,
    HumanSessionManager,
    InMemoryHumanSessionStore,
    LaunchplaneHumanSession,
)


def _actions_identity(**overrides: object) -> GitHubActionsIdentity:
    claims: dict[str, object] = {
        "repository": "cbusillo/verireel",
        "repository_owner": "cbusillo",
        "workflow_ref": "cbusillo/verireel/.github/workflows/preview.yml@refs/heads/main",
        "job_workflow_ref": "cbusillo/launchplane/.github/workflows/reusable.yml@refs/heads/main",
        "ref": "refs/heads/main",
        "ref_type": "branch",
        "event_name": "pull_request",
        "environment": "preview",
        "subject": "repo:cbusillo/verireel:pull_request",
        "sha": "6b3c9d7e8f901234567890abcdef1234567890ab",
    }
    claims.update(overrides)
    return GitHubActionsIdentity(
        repository=str(claims["repository"]),
        repository_owner=str(claims["repository_owner"]),
        workflow_ref=str(claims["workflow_ref"]),
        job_workflow_ref=str(claims["job_workflow_ref"]),
        ref=str(claims["ref"]),
        ref_type=str(claims["ref_type"]),
        event_name=str(claims["event_name"]),
        environment=str(claims["environment"]),
        subject=str(claims["subject"]),
        sha=str(claims["sha"]),
        raw_claims=claims,
    )


def _human_identity(**overrides: object) -> GitHubHumanIdentity:
    values: dict[str, object] = {
        "login": "alice",
        "github_id": 123,
        "name": "Alice Example",
        "email": "alice@example.com",
        "organizations": frozenset({"cbusillo"}),
        "teams": frozenset({"platform", "cbusillo/platform"}),
        "role": "read_only",
    }
    values.update(overrides)
    return GitHubHumanIdentity(
        login=str(values["login"]),
        github_id=values["github_id"],  # type: ignore[arg-type]
        name=str(values["name"]),
        email=str(values["email"]),
        organizations=values["organizations"],  # type: ignore[arg-type]
        teams=values["teams"],  # type: ignore[arg-type]
        role=values["role"],  # type: ignore[arg-type]
    )


class GitHubOidcVerifierBoundaryTests(unittest.TestCase):
    def test_rejects_blank_token_before_key_lookup(self) -> None:
        jwk_client = Mock()
        verifier = GitHubOidcVerifier(audience="launchplane.example", jwk_client=jwk_client)

        with self.assertRaisesRegex(ValueError, "bearer token is required"):
            verifier.verify("  ")

        jwk_client.get_signing_key_from_jwt.assert_not_called()

    def test_decodes_expected_github_actions_claims(self) -> None:
        jwk_client = Mock()
        jwk_client.get_signing_key_from_jwt.return_value = SimpleNamespace(key="signing-key")
        claims = {
            "repository": "cbusillo/verireel",
            "repository_owner": "cbusillo",
            "workflow_ref": "cbusillo/verireel/.github/workflows/preview.yml@refs/heads/main",
            "job_workflow_ref": "cbusillo/launchplane/.github/workflows/reusable.yml@refs/heads/main",
            "ref": "refs/heads/main",
            "ref_type": "branch",
            "event_name": "pull_request",
            "environment": "preview",
            "sub": "repo:cbusillo/verireel:pull_request",
            "sha": "6b3c9d7e8f901234567890abcdef1234567890ab",
        }

        with patch("control_plane.service_auth.jwt.decode", return_value=claims) as decode_mock:
            verifier = GitHubOidcVerifier(
                audience="launchplane.example",
                jwk_client=jwk_client,
            )
            identity = verifier.verify("header.payload.signature")

        jwk_client.get_signing_key_from_jwt.assert_called_once_with("header.payload.signature")
        decode_mock.assert_called_once_with(
            "header.payload.signature",
            "signing-key",
            algorithms=["RS256"],
            audience="launchplane.example",
            issuer="https://token.actions.githubusercontent.com",
        )
        self.assertEqual(identity.repository, "cbusillo/verireel")
        self.assertEqual(identity.repository_owner, "cbusillo")
        self.assertEqual(identity.workflow_ref, claims["workflow_ref"])
        self.assertEqual(identity.job_workflow_ref, claims["job_workflow_ref"])
        self.assertEqual(identity.raw_claims, claims)

    def test_requires_repository_and_workflow_claims(self) -> None:
        required_claims = {
            "repository": "OIDC token is missing repository claim",
            "workflow_ref": "OIDC token is missing workflow_ref claim",
        }
        for missing_claim, expected_message in required_claims.items():
            with self.subTest(missing_claim=missing_claim):
                jwk_client = Mock()
                jwk_client.get_signing_key_from_jwt.return_value = SimpleNamespace(
                    key="signing-key"
                )
                claims = {
                    "repository": "cbusillo/verireel",
                    "workflow_ref": "cbusillo/verireel/.github/workflows/preview.yml@refs/heads/main",
                }
                claims[missing_claim] = ""

                with patch("control_plane.service_auth.jwt.decode", return_value=claims):
                    verifier = GitHubOidcVerifier(
                        audience="launchplane.example",
                        jwk_client=jwk_client,
                    )
                    with self.assertRaisesRegex(ValueError, expected_message):
                        verifier.verify("header.payload.signature")


class LaunchplaneAuthzPolicyBoundaryTests(unittest.TestCase):
    def test_actions_policy_fails_closed_by_claim_and_scope(self) -> None:
        rule = GitHubActionsPolicyRule(
            repository="cbusillo/verireel",
            workflow_refs=("cbusillo/verireel/.github/workflows/preview.yml@refs/heads/*",),
            job_workflow_refs=("cbusillo/launchplane/.github/workflows/*.yml@refs/heads/main",),
            event_names=("pull_request",),
            refs=("refs/heads/main",),
            environments=("preview",),
            products=("verireel",),
            contexts=("verireel-testing",),
            actions=("verireel_preview_refresh.execute",),
        )
        identity = _actions_identity()

        self.assertTrue(
            rule.allows(
                identity=identity,
                action="verireel_preview_refresh.execute",
                product="verireel",
                context="verireel-testing",
            )
        )
        denied_cases = (
            (
                "repository",
                _actions_identity(repository="cbusillo/other"),
                "verireel",
                "verireel-testing",
            ),
            (
                "workflow",
                _actions_identity(
                    workflow_ref="cbusillo/verireel/.github/workflows/preview.yml@refs/tags/v1"
                ),
                "verireel",
                "verireel-testing",
            ),
            (
                "job_workflow",
                _actions_identity(
                    job_workflow_ref="cbusillo/launchplane/.github/workflows/reusable.yml@refs/heads/dev"
                ),
                "verireel",
                "verireel-testing",
            ),
            ("event", _actions_identity(event_name="push"), "verireel", "verireel-testing"),
            ("ref", _actions_identity(ref="refs/heads/feature"), "verireel", "verireel-testing"),
            ("environment", _actions_identity(environment="prod"), "verireel", "verireel-testing"),
            ("product", identity, "other-product", "verireel-testing"),
            ("context", identity, "verireel", "other-context"),
        )
        for name, case_identity, product, context in denied_cases:
            with self.subTest(name=name):
                self.assertFalse(
                    rule.allows(
                        identity=case_identity,
                        action="verireel_preview_refresh.execute",
                        product=product,
                        context=context,
                    )
                )
        self.assertFalse(
            rule.allows(
                identity=identity,
                action="verireel_preview_destroy.execute",
                product="verireel",
                context="verireel-testing",
            )
        )

    def test_combined_policy_separates_actions_and_human_identities(self) -> None:
        policy = LaunchplaneAuthzPolicy(
            github_actions=(
                GitHubActionsPolicyRule(
                    repository="cbusillo/verireel",
                    products=("verireel",),
                    contexts=("verireel-testing",),
                    actions=("driver.read",),
                ),
            ),
            github_humans=(
                GitHubHumanPolicyRule(
                    teams=("cbusillo/platform",),
                    roles=("read_only",),
                    products=("launchplane",),
                    contexts=("launchplane",),
                    actions=("driver.read",),
                ),
            ),
        )

        self.assertTrue(
            policy.allows(
                identity=_actions_identity(),
                action="driver.read",
                product="verireel",
                context="verireel-testing",
            )
        )
        self.assertTrue(
            policy.allows(
                identity=_human_identity(),
                action="driver.read",
                product="launchplane",
                context="launchplane",
            )
        )
        self.assertFalse(
            policy.allows(
                identity=_human_identity(),
                action="driver.read",
                product="verireel",
                context="verireel-testing",
            )
        )

    def test_parse_authz_policy_toml_preserves_human_and_actions_rules(self) -> None:
        policy = parse_authz_policy_toml(
            """
            [[github_actions]]
            repository = "cbusillo/verireel"
            workflow_refs = ["cbusillo/verireel/.github/workflows/*.yml@*"]
            products = ["verireel"]
            contexts = ["verireel-testing"]
            actions = ["driver.read"]

            [[github_humans]]
            organizations = ["cbusillo"]
            teams = ["cbusillo/platform"]
            roles = ["admin"]
            actions = ["*"]
            """
        )

        self.assertEqual(len(policy.github_actions), 1)
        self.assertEqual(len(policy.github_humans), 1)
        self.assertEqual(policy.github_actions[0].repository, "cbusillo/verireel")
        self.assertEqual(policy.github_humans[0].roles, ("admin",))


class HumanSessionBoundaryTests(unittest.TestCase):
    def _session_manager(self) -> tuple[HumanSessionManager, InMemoryHumanSessionStore]:
        config = GitHubOAuthConfig(
            client_id="client-id",
            client_secret="client-secret",
            public_url="https://launchplane.example",
            session_secret="session-secret",
            cookie_secure=False,
        )
        store = InMemoryHumanSessionStore()
        manager = HumanSessionManager(config=config, session_store=store)
        return manager, store

    def test_session_cookie_round_trips_and_can_be_deleted(self) -> None:
        manager, store = self._session_manager()
        session = manager.issue(_human_identity())
        cookie = manager.session_cookie_header(session)

        self.assertIn("launchplane_session=", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertNotIn("Secure", cookie)
        loaded_session = manager.read_cookie(cookie)
        self.assertIsNotNone(loaded_session)
        assert loaded_session is not None
        self.assertEqual(loaded_session.session_id, session.session_id)

        manager.delete_cookie_session(cookie)

        self.assertIsNone(store.read_session(session.session_id))

    def test_session_cookie_rejects_missing_and_malformed_values(self) -> None:
        manager, _store = self._session_manager()

        self.assertIsNone(manager.read_cookie("other=value"))
        self.assertIsNone(manager.read_cookie("launchplane_session=bad value"))

    def test_expired_session_is_removed_on_read(self) -> None:
        manager, store = self._session_manager()
        expired_session = LaunchplaneHumanSession(
            session_id="expired-session",
            identity=_human_identity(),
            created_at=datetime.now(timezone.utc) - timedelta(hours=2),
            expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        )
        store.write_session(expired_session)

        self.assertIsNone(manager.read_cookie("launchplane_session=expired-session"))
        self.assertIsNone(store.read_session("expired-session"))


if __name__ == "__main__":
    unittest.main()
