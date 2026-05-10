from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
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
    LocalOperatorIdentity,
    TerminalAgentIdentity,
    TerminalAgentPolicyRule,
    action_safety,
    agent_authz_audit,
    agent_consumer_subject,
    limited_remote_user_action_allowed,
    local_operator_action_allowed,
    parse_authz_policy_toml,
)
from control_plane.contracts.authz_policy_record import authz_policy_sha256
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


def _terminal_agent_identity(**overrides: object) -> TerminalAgentIdentity:
    values: dict[str, object] = {
        "subject": "local-owner-agent",
        "token_label": "local-owner-read",
    }
    values.update(overrides)
    return TerminalAgentIdentity(
        subject=str(values["subject"]),
        token_label=str(values["token_label"]),
    )


def _local_operator_identity(**overrides: object) -> LocalOperatorIdentity:
    values: dict[str, object] = {
        "subject": "local-owner-agent",
        "token_label": "local-owner-write",
    }
    values.update(overrides)
    return LocalOperatorIdentity(
        subject=str(values["subject"]),
        token_label=str(values["token_label"]),
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
    def test_agent_consumer_subject_classifies_terminal_agent_as_read_only_context(self) -> None:
        subject = agent_consumer_subject(
            identity=_terminal_agent_identity(),
            action="product_environment.read",
            product="sellyouroutboard",
            context="sellyouroutboard-testing",
        )

        self.assertEqual(subject.subject_type, "terminal_agent")
        self.assertEqual(subject.access_profile, "owner_local_agent")
        self.assertEqual(subject.role, "worker")
        self.assertEqual(subject.action_safety, "read")
        self.assertTrue(subject.read_only_context)
        self.assertFalse(subject.approval_capable)

    def test_agent_consumer_subject_classifies_local_operator_as_approval_capable(
        self,
    ) -> None:
        subject = agent_consumer_subject(
            identity=_local_operator_identity(),
            action="product_config.apply",
            product="sellyouroutboard",
            context="sellyouroutboard",
        )

        self.assertEqual(subject.subject_type, "local_operator")
        self.assertEqual(subject.access_profile, "owner_local_agent")
        self.assertEqual(subject.role, "operator")
        self.assertEqual(subject.action_safety, "mutation")
        self.assertFalse(subject.read_only_context)
        self.assertTrue(subject.approval_capable)

    def test_agent_consumer_subject_classifies_human_admin_as_approval_capable(self) -> None:
        subject = agent_consumer_subject(
            identity=_human_identity(role="admin"),
            action="generic_web_prod_promotion.execute",
            product="verireel",
            context="verireel",
        )

        self.assertEqual(subject.subject_type, "github_human")
        self.assertEqual(subject.access_profile, "human_admin")
        self.assertEqual(subject.role, "admin")
        self.assertEqual(subject.action_safety, "prod")
        self.assertFalse(subject.read_only_context)
        self.assertTrue(subject.approval_capable)

    def test_agent_consumer_subject_classifies_actions_by_requested_safety(self) -> None:
        read_subject = agent_consumer_subject(
            identity=_actions_identity(),
            action="product_environment.read",
            product="verireel",
            context="verireel-testing",
        )
        prod_subject = agent_consumer_subject(
            identity=_actions_identity(),
            action="verireel_prod_deploy.execute",
            product="verireel",
            context="verireel",
        )

        self.assertEqual(read_subject.subject_type, "github_actions")
        self.assertEqual(read_subject.access_profile, "automation_worker")
        self.assertTrue(read_subject.read_only_context)
        self.assertFalse(read_subject.approval_capable)
        self.assertEqual(prod_subject.action_safety, "prod")
        self.assertFalse(prod_subject.read_only_context)
        self.assertTrue(prod_subject.approval_capable)

    def test_action_safety_classifies_privileged_action_families(self) -> None:
        cases = {
            "product_environment.read": "read",
            "work_graph.rank": "read",
            "preview_pr_feedback.write": "safe_write",
            "every_code_work_request.rerun": "safe_write",
            "product_config.apply": "mutation",
            "product_config.apply.secret": "secret_backed",
            "generic_web_prod_promotion.execute": "prod",
            "preview_destroy.execute": "destructive",
            "secret_binding.apply": "secret_backed",
            "authz_policy_grant.write": "policy_admin",
        }

        for action, expected_safety in cases.items():
            with self.subTest(action=action):
                self.assertEqual(action_safety(action), expected_safety)

    def test_limited_remote_user_action_allowlist_excludes_privileged_families(self) -> None:
        allowed_actions = (
            "product_environment.read",
            "work_graph.rank",
            "preview_pr_feedback.write",
        )
        denied_actions = (
            "product_config.apply",
            "product_config.apply.secret",
            "generic_web_prod_promotion.execute",
            "preview_destroy.execute",
        )

        for action in allowed_actions:
            with self.subTest(action=action):
                self.assertTrue(limited_remote_user_action_allowed(action))
        for action in denied_actions:
            with self.subTest(action=action):
                self.assertFalse(limited_remote_user_action_allowed(action))

    def test_human_read_only_role_fails_closed_for_privileged_actions(self) -> None:
        policy = LaunchplaneAuthzPolicy(
            github_humans=(
                GitHubHumanPolicyRule(
                    logins=("alice",),
                    roles=("read_only",),
                    products=("verireel",),
                    contexts=("verireel", "verireel-testing"),
                    actions=(
                        "product_environment.read",
                        "preview_pr_feedback.write",
                        "generic_web_prod_promotion.execute",
                        "product_config.apply.secret",
                    ),
                ),
            )
        )
        identity = _human_identity(role="read_only")

        self.assertTrue(
            policy.allows(
                identity=identity,
                action="product_environment.read",
                product="verireel",
                context="verireel-testing",
            )
        )
        self.assertTrue(
            policy.allows(
                identity=identity,
                action="preview_pr_feedback.write",
                product="verireel",
                context="verireel-testing",
            )
        )
        self.assertFalse(
            policy.allows(
                identity=identity,
                action="generic_web_prod_promotion.execute",
                product="verireel",
                context="verireel",
            )
        )
        self.assertFalse(
            policy.allows(
                identity=identity,
                action="product_config.apply.secret",
                product="verireel",
                context="verireel-testing",
            )
        )

    def test_agent_authz_audit_records_safe_denial_metadata(self) -> None:
        audit = agent_authz_audit(
            identity=_actions_identity(),
            action="preview_pr_feedback.write",
            product="verireel",
            context="verireel-testing",
            decision="denied",
            reason_code="authorization_denied",
            policy_source="db",
            policy_sha256="abc123",
        )

        self.assertEqual(audit.decision, "denied")
        self.assertEqual(audit.reason_code, "authorization_denied")
        self.assertEqual(audit.subject.subject_type, "github_actions")
        self.assertEqual(audit.subject.action_safety, "safe_write")
        self.assertEqual(audit.source_kind, "authz_policy")
        self.assertEqual(audit.policy_source, "db")
        self.assertEqual(audit.policy_sha256, "abc123")

    def test_agent_write_intent_actions_use_existing_policy_action_names(self) -> None:
        from control_plane.contracts.agent_write_intent import (
            authz_action_for_agent_write_intent,
        )

        self.assertEqual(
            authz_action_for_agent_write_intent("every_code_rerun"),
            "every_code_work_request.write",
        )
        self.assertEqual(
            authz_action_for_agent_write_intent("preview_refresh"),
            "preview_refresh.execute",
        )
        self.assertEqual(
            authz_action_for_agent_write_intent("promotion_dispatch"),
            "generic_web_prod_promotion_workflow.execute",
        )

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

    def test_terminal_agent_policy_fails_closed_by_subject_label_and_scope(self) -> None:
        rule = TerminalAgentPolicyRule(
            subjects=("local-*-agent",),
            token_labels=("local-owner-*",),
            products=("sellyouroutboard",),
            contexts=("sellyouroutboard-testing",),
            actions=("product_environment.read",),
        )
        identity = _terminal_agent_identity()

        self.assertTrue(
            rule.allows(
                identity=identity,
                action="product_environment.read",
                product="sellyouroutboard",
                context="sellyouroutboard-testing",
            )
        )
        denied_cases = (
            (
                "subject",
                _terminal_agent_identity(subject="other-agent"),
                "sellyouroutboard",
                "sellyouroutboard-testing",
                "product_environment.read",
            ),
            (
                "token_label",
                _terminal_agent_identity(token_label="write-token"),
                "sellyouroutboard",
                "sellyouroutboard-testing",
                "product_environment.read",
            ),
            (
                "product",
                identity,
                "other-product",
                "sellyouroutboard-testing",
                "product_environment.read",
            ),
            ("context", identity, "sellyouroutboard", "other-context", "product_environment.read"),
            (
                "action",
                identity,
                "sellyouroutboard",
                "sellyouroutboard-testing",
                "generic_web_prod_promotion.execute",
            ),
        )
        for name, case_identity, product, context, action in denied_cases:
            with self.subTest(name=name):
                self.assertFalse(
                    rule.allows(
                        identity=case_identity,
                        action=action,
                        product=product,
                        context=context,
                    )
                )

    def test_combined_policy_separates_terminal_agents_from_other_identities(self) -> None:
        policy = LaunchplaneAuthzPolicy(
            terminal_agents=(
                TerminalAgentPolicyRule(
                    subjects=("local-owner-agent",),
                    token_labels=("local-owner-read",),
                    products=("sellyouroutboard",),
                    contexts=("sellyouroutboard-testing",),
                    actions=("product_environment.read",),
                ),
            )
        )

        self.assertTrue(
            policy.allows(
                identity=_terminal_agent_identity(),
                action="product_environment.read",
                product="sellyouroutboard",
                context="sellyouroutboard-testing",
            )
        )
        self.assertFalse(
            policy.allows(
                identity=_actions_identity(repository="cbusillo/sellyouroutboard"),
                action="product_environment.read",
                product="sellyouroutboard",
                context="sellyouroutboard-testing",
            )
        )

    def test_local_operator_policy_allows_only_product_config_actions(self) -> None:
        policy = LaunchplaneAuthzPolicy()
        identity = _local_operator_identity()

        self.assertTrue(
            policy.allows(
                identity=identity,
                action="product_config.plan",
                product="sellyouroutboard",
                context="sellyouroutboard",
            )
        )
        self.assertTrue(
            policy.allows(
                identity=identity,
                action="product_config.apply",
                product="sellyouroutboard",
                context="sellyouroutboard",
            )
        )
        self.assertFalse(
            policy.allows(
                identity=identity,
                action="promotion.execute",
                product="sellyouroutboard",
                context="sellyouroutboard",
            )
        )
        self.assertFalse(
            policy.allows(
                identity=identity,
                action="preview_destroy.execute",
                product="sellyouroutboard",
                context="sellyouroutboard-testing",
            )
        )
        self.assertFalse(
            policy.allows(
                identity=identity,
                action="authz_policy.grant_terminal_agent",
                product="launchplane",
                context="launchplane",
            )
        )

    def test_local_operator_action_requires_expected_subject_and_label(self) -> None:
        self.assertTrue(
            local_operator_action_allowed(
                identity=_local_operator_identity(),
                action="product_config.apply",
            )
        )
        self.assertFalse(
            local_operator_action_allowed(
                identity=_local_operator_identity(subject="different-owner"),
                action="product_config.apply",
            )
        )
        self.assertFalse(
            local_operator_action_allowed(
                identity=_local_operator_identity(token_label="other-write-token"),
                action="product_config.apply",
            )
        )

    def test_local_operator_login_does_not_grant_human_access(self) -> None:
        policy = LaunchplaneAuthzPolicy()

        self.assertFalse(
            policy.allows(
                identity=_human_identity(login="local-owner-agent"),
                action="product_environment.read",
                product="sellyouroutboard",
                context="sellyouroutboard-testing",
            )
        )

    def test_syo_preview_grant_covers_pull_request_branch_refs(self) -> None:
        rule = GitHubActionsPolicyRule(
            repository="cbusillo/sellyouroutboard",
            workflow_refs=(
                "cbusillo/sellyouroutboard/.github/workflows/preview-control-plane.yml@*",
            ),
            event_names=("pull_request",),
            products=("sellyouroutboard",),
            contexts=("sellyouroutboard-testing",),
            actions=("preview_refresh.execute",),
        )
        identity = _actions_identity(
            repository="cbusillo/sellyouroutboard",
            workflow_ref=(
                "cbusillo/sellyouroutboard/.github/workflows/preview-control-plane.yml"
                "@refs/heads/replace-mike-photo-batch"
            ),
            event_name="pull_request",
            ref="refs/pull/41/merge",
            subject="repo:cbusillo/sellyouroutboard:pull_request",
        )

        self.assertTrue(
            rule.allows(
                identity=identity,
                action="preview_refresh.execute",
                product="sellyouroutboard",
                context="sellyouroutboard-testing",
            )
        )
        self.assertFalse(
            rule.allows(
                identity=identity,
                action="preview_destroy.execute",
                product="sellyouroutboard",
                context="sellyouroutboard-testing",
            )
        )

    def test_syo_preview_grant_covers_canonical_context(self) -> None:
        rule = GitHubActionsPolicyRule(
            repository="cbusillo/sellyouroutboard",
            workflow_refs=(
                "cbusillo/sellyouroutboard/.github/workflows/preview-control-plane.yml@*",
            ),
            event_names=("pull_request",),
            products=("sellyouroutboard",),
            contexts=("sellyouroutboard",),
            actions=("preview_refresh.execute",),
        )
        identity = _actions_identity(
            repository="cbusillo/sellyouroutboard",
            workflow_ref=(
                "cbusillo/sellyouroutboard/.github/workflows/preview-control-plane.yml"
                "@refs/heads/replace-mike-photo-batch"
            ),
            event_name="pull_request",
            ref="refs/pull/41/merge",
            subject="repo:cbusillo/sellyouroutboard:pull_request",
        )

        self.assertTrue(
            rule.allows(
                identity=identity,
                action="preview_refresh.execute",
                product="sellyouroutboard",
                context="sellyouroutboard",
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

            [[terminal_agents]]
            subjects = ["local-owner-agent"]
            token_labels = ["local-owner-read"]
            products = ["sellyouroutboard"]
            contexts = ["sellyouroutboard-testing"]
            actions = ["product_environment.read"]
            """
        )

        self.assertEqual(len(policy.github_actions), 1)
        self.assertEqual(len(policy.github_humans), 1)
        self.assertEqual(len(policy.terminal_agents), 1)
        self.assertEqual(policy.github_actions[0].repository, "cbusillo/verireel")
        self.assertEqual(policy.github_humans[0].roles, ("admin",))
        self.assertEqual(policy.terminal_agents[0].subjects, ("local-owner-agent",))


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


class GitHubOidcVerifierHardeningTests(unittest.TestCase):
    def test_verifier_requires_non_empty_configuration(self) -> None:
        with self.assertRaises(ValueError):
            GitHubOidcVerifier(audience=" ", jwk_client=Mock())
        with self.assertRaises(ValueError):
            GitHubOidcVerifier(audience="launchplane", issuer=" ", jwk_client=Mock())
        with self.assertRaises(ValueError):
            GitHubOidcVerifier(audience="launchplane", jwks_url=" ")

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


class LaunchplaneAuthzPolicyCompatibilityTests(unittest.TestCase):
    def test_policy_sha256_omits_empty_terminal_agents_for_existing_records(self) -> None:
        policy = LaunchplaneAuthzPolicy(
            github_actions=(
                GitHubActionsPolicyRule(
                    repository="cbusillo/launchplane",
                    products=("launchplane",),
                    contexts=("launchplane",),
                    actions=("launchplane_service_deploy.execute",),
                ),
            )
        )
        legacy_payload = policy.model_dump(mode="json", exclude_none=True)
        legacy_payload.pop("terminal_agents", None)
        legacy_sha256 = hashlib.sha256(
            json.dumps(legacy_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

        self.assertEqual(authz_policy_sha256(policy), legacy_sha256)

    def test_policy_sha256_includes_terminal_agent_rules_when_present(self) -> None:
        base_policy = LaunchplaneAuthzPolicy(
            github_actions=(
                GitHubActionsPolicyRule(
                    repository="cbusillo/launchplane",
                    products=("launchplane",),
                    contexts=("launchplane",),
                    actions=("launchplane_service_deploy.execute",),
                ),
            )
        )
        terminal_policy = base_policy.model_copy(
            update={
                "terminal_agents": (
                    TerminalAgentPolicyRule(
                        subjects=("local-owner-agent",),
                        token_labels=("local-owner-read",),
                        products=("sellyouroutboard",),
                        contexts=("sellyouroutboard",),
                        actions=("product_environment.read",),
                    ),
                )
            }
        )

        self.assertNotEqual(authz_policy_sha256(terminal_policy), authz_policy_sha256(base_policy))

    def test_actions_policy_matches_patterns_and_scopes(self) -> None:
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

        self.assertTrue(
            rule.allows(
                identity=_actions_identity(
                    repository="cbusillo/site",
                    workflow_ref="cbusillo/site/.github/workflows/deploy.yml@refs/heads/main",
                    event_name="workflow_dispatch",
                    ref="refs/heads/main",
                    environment="production",
                ),
                action="generic_web_prod_promotion.execute",
                product="site",
                context="site-prod",
            )
        )

    def test_human_policy_matches_team_and_role_scope(self) -> None:
        rule = GitHubHumanPolicyRule(
            organizations=("cbusillo",),
            teams=("cbusillo/platform",),
            roles=("admin",),
            products=("site",),
            contexts=("site-prod",),
            actions=("driver.execute",),
        )

        self.assertTrue(
            rule.allows(
                identity=_human_identity(
                    login="operator",
                    teams=frozenset({"cbusillo/platform"}),
                    role="admin",
                ),
                action="driver.execute",
                product="site",
                context="site-prod",
            )
        )

    def test_launchplane_policy_dispatches_by_identity_type(self) -> None:
        policy = LaunchplaneAuthzPolicy(
            github_actions=(
                GitHubActionsPolicyRule(
                    repository="cbusillo/site",
                    workflow_refs=("*",),
                    products=("site",),
                    actions=("driver.execute",),
                ),
            ),
            github_humans=(
                GitHubHumanPolicyRule(
                    teams=("launchplane-admins",),
                    roles=("admin",),
                    products=("site",),
                    actions=("driver.execute",),
                ),
            ),
        )

        self.assertTrue(
            policy.allows(
                identity=_actions_identity(repository="cbusillo/site"),
                action="driver.execute",
                product="site",
                context="",
            )
        )
        self.assertTrue(
            policy.allows(
                identity=_human_identity(teams=frozenset({"launchplane-admins"}), role="admin"),
                action="driver.execute",
                product="site",
                context="",
            )
        )


if __name__ == "__main__":
    unittest.main()
