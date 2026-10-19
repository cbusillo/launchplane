import unittest
from http.client import IncompleteRead
from unittest.mock import Mock, patch

from control_plane.access_policy_review import (
    build_access_policy_review,
    _public_github_account_login,
    bounded_account_logins,
    _principal,
    _scope,
    _role_effects,
    _public_github_login_account,
)
from control_plane.authz_grant_service import AuthzManagedRuleEntry
from control_plane.contracts.ordinary_agent import OrdinaryAgentPolicyRule, OrdinaryAgentTarget
from control_plane.service_auth import (
    GitHubActionsPolicyRule,
    GitHubHumanPolicyRule,
    LocalOperatorPolicyRule,
)
from control_plane.contracts.privileged_operation import ManagedAuthzPolicySetProposalInput
from control_plane.privileged_operation_registry import plan_managed_authz_policy_set
from control_plane.service_auth import LaunchplaneAuthzPolicy
from control_plane.contracts.access_policy_review import AccessPolicyReview
from tests.test_privileged_operation_http import _policy, _policy_record


class AccessPolicyReviewTests(unittest.TestCase):
    def review(
        self, *, covered: bool = False, local: bool = False, group_overlap: bool = False
    ) -> AccessPolicyReview:
        payload = _policy().model_dump(mode="json")
        rule: dict[str, object] = {
            "managed_set_id": "review.subjects",
            "managed_rule_id": "removed-rule",
            "actions": ["operations.read"],
            "products": ["launchplane"],
            "contexts": ["launchplane"],
        }
        if local:
            rule.update(subjects=["agent:fixture"], token_labels=["fixture-label"])
            payload["local_operators"] = [rule]
        else:
            rule.update(github_ids=[789], logins=["fixture-person"], roles=["read_only"])
            payload["github_humans"].append(rule)
            if covered:
                payload["github_humans"].append(
                    {**rule, "managed_set_id": "other", "managed_rule_id": "covering-rule"}
                )
            if group_overlap:
                payload["github_humans"].append(
                    {
                        **rule,
                        "managed_set_id": "other",
                        "managed_rule_id": "team-rule",
                        "github_ids": [],
                        "logins": [],
                        "teams": ["fixture/team"],
                    }
                )
        policy = LaunchplaneAuthzPolicy.model_validate(payload)
        request = ManagedAuthzPolicySetProposalInput(
            managed_set_id="review.subjects",
            desired_policy=LaunchplaneAuthzPolicy(schema_version=policy.schema_version),
            reason="secret-sentinel-must-not-render",
        )
        store = Mock()
        store.list_authz_policy_records.return_value = (_policy_record(policy),)
        evidence = plan_managed_authz_policy_set(store, request)
        return build_access_policy_review(
            record_store=store,
            request=request,
            evidence=evidence,
            login_for_id=lambda _: "fixture-person",
        )

    def test_removing_only_role_names_principal_and_loss(self) -> None:
        review = self.review()
        self.assertEqual(review.state, "available")
        removed = review.rules[0]
        self.assertIn("fixture-person", removed.principal)
        self.assertEqual(removed.roles, ("read_only",))
        self.assertEqual(removed.permissions, ("operations.read",))
        self.assertEqual(removed.role_effects[0].before, "read_only")
        self.assertIsNone(removed.role_effects[0].after)
        self.assertEqual(removed.role_effects[0].state, "lost")

    def test_another_rule_preserves_role(self) -> None:
        effect = self.review(covered=True).rules[0].role_effects[0]
        self.assertEqual(effect.before, "read_only")
        self.assertEqual(effect.after, "read_only")
        self.assertEqual(effect.state, "retained")

    def test_local_operator_names_subject_and_permission_without_browser_role(self) -> None:
        rule = self.review(local=True).rules[0]
        self.assertIn("agent:fixture", rule.principal)
        self.assertEqual(rule.permissions, ("operations.read",))
        self.assertEqual(rule.role_effects, ())

    def test_review_never_copies_free_text_or_secret_values(self) -> None:
        self.assertNotIn("secret-sentinel-must-not-render", self.review().model_dump_json())

    def test_unknown_team_membership_is_not_a_claim_of_entire_role_loss(self) -> None:
        effect = self.review(group_overlap=True).rules[0].role_effects[0]
        self.assertEqual(effect.state, "unknown")

    def test_public_account_label_is_id_bound_and_uses_no_credentials(self) -> None:
        _public_github_account_login.cache_clear()
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = (
            b'{"id":789,"login":"fixture-person","email":"private-value-sentinel"}'
        )
        with patch("control_plane.access_policy_review.urlopen", return_value=response) as read:
            self.assertEqual(_public_github_account_login(789, 0), "fixture-person")
            request = read.call_args.args[0]
            self.assertIsNone(request.get_header("Authorization"))
            self.assertEqual(request.get_method(), "GET")
            self.assertEqual(_public_github_account_login(790, 0), "")
        _public_github_account_login.cache_clear()

    def test_display_lookup_budget_is_shared_and_truncated_responses_are_unavailable(self) -> None:
        _public_github_account_login.cache_clear()
        with patch("control_plane.access_policy_review.urlopen", side_effect=IncompleteRead(b"")):
            self.assertEqual(_public_github_account_login(789, 0), "")
        _public_github_account_login.cache_clear()
        reader = Mock(return_value="fixture-person")
        with patch("control_plane.access_policy_review.monotonic", return_value=0) as clock:
            bounded, by_login = bounded_account_logins(reader, lambda _: (789, "fixture-person"))
            self.assertEqual(bounded(789), "fixture-person")
            self.assertEqual(bounded(789), "fixture-person")
            clock.return_value = 2
            self.assertEqual(bounded(790), "")
            self.assertIsNone(by_login("fixture-person"))
        reader.assert_called_once_with(789)

    def test_scope_display_preserves_target_and_workflow_conditions(self) -> None:
        target = OrdinaryAgentTarget(
            repository_id=42, repository="fixture/target", base_branch="next"
        )
        ordinary = OrdinaryAgentPolicyRule(
            managed_set_id="fixture",
            managed_rule_id="ordinary",
            principal_id="fixture-agent",
            target=target,
            actions=("self_read",),
        )
        summary = _scope(AuthzManagedRuleEntry("ordinary_agents", ordinary), 3)
        for value in (target.repository, str(target.repository_id), target.base_branch):
            self.assertIn(value, summary)
        conditions = {
            "job_workflow_refs": ("fixture/shared/.github/workflows/deploy.yml@" + "a" * 40,),
            "event_names": ("push",),
            "refs": ("refs/heads/next",),
            "environments": ("testing",),
        }
        workflow = GitHubActionsPolicyRule.model_validate(
            {
                "repository": "fixture/source",
                "repository_id": "123",
                "repository_owner_id": "456",
                **conditions,
            }
        )
        summary = _scope(AuthzManagedRuleEntry("github_actions", workflow), 3)
        for values in conditions.values():
            for value in values:
                self.assertIn(value, summary)
        principal = _principal(AuthzManagedRuleEntry("github_actions", workflow), lambda _: "")
        for value in (workflow.repository_id, workflow.repository_owner_id):
            self.assertIn(value, principal)
        token_rule = LocalOperatorPolicyRule(token_labels=("fixture-label",))
        self.assertIn(
            token_rule.token_labels[0],
            _principal(AuthzManagedRuleEntry("local_operators", token_rule), lambda _: ""),
        )
        self.assertNotEqual(
            _scope(AuthzManagedRuleEntry("github_actions", workflow), 3),
            _scope(
                AuthzManagedRuleEntry(
                    "github_actions", GitHubActionsPolicyRule(repository=workflow.repository)
                ),
                3,
            ),
        )

    def test_role_effects_never_invent_identity_or_hide_unknown_previous_membership(self) -> None:
        rule = GitHubHumanPolicyRule(
            github_ids=(701, 702), logins=("fixture-name",), roles=("read_only",)
        )
        before = LaunchplaneAuthzPolicy(github_humans=(rule,))
        after = LaunchplaneAuthzPolicy()
        effects = _role_effects(
            rule, before, after, lambda account_id: "fixture-name" if account_id == 701 else ""
        )
        self.assertEqual(effects[0].state, "lost")
        self.assertEqual(effects[1].state, "unknown")
        self.assertNotEqual(effects[0].principal, effects[1].principal)
        self.assertIsNone(effects[1].before)
        self.assertIsNone(effects[1].after)
        verified = _role_effects(
            rule,
            before,
            after,
            lambda account_id: "fixture-name" if account_id == 701 else "different-person",
        )
        self.assertEqual(verified[1].state, "unaffected")
        team = GitHubHumanPolicyRule(github_ids=(701,), teams=("fixture/team",), roles=("admin",))
        remaining = LaunchplaneAuthzPolicy(github_humans=(rule,))
        uncertain_before = LaunchplaneAuthzPolicy(github_humans=(rule, team))
        uncertain = _role_effects(team, uncertain_before, remaining, lambda _: "fixture-name")[0]
        self.assertEqual(uncertain.state, "unknown")
        self.assertIsNone(uncertain.before)

    def test_login_only_roles_use_verified_account_metadata_and_canonical_login(self) -> None:
        rule = GitHubHumanPolicyRule(logins=("fixture-name",), roles=("read_only",))
        admin = GitHubHumanPolicyRule(github_ids=(123,), roles=("admin",))
        before = LaunchplaneAuthzPolicy(github_humans=(rule, admin))
        after = LaunchplaneAuthzPolicy(github_humans=(admin,))
        effects = _role_effects(rule, before, after, lambda _: "", lambda _: (789, "fixture-name"))
        self.assertEqual(effects[0].state, "lost")
        self.assertEqual(effects[0].before, "read_only")
        renamed = _role_effects(
            rule, before, after, lambda _: "", lambda _: (789, "renamed-person")
        )
        self.assertEqual(renamed[0].state, "unaffected")
        unknown = _role_effects(rule, before, after, lambda _: "")
        self.assertEqual(unknown[0].state, "unknown")
        _public_github_login_account.cache_clear()
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b'{"id":789,"login":"renamed-person"}'
        with patch("control_plane.access_policy_review.urlopen", return_value=response) as read:
            self.assertEqual(
                _public_github_login_account("fixture-name", 0), (789, "renamed-person")
            )
            self.assertIsNone(read.call_args.args[0].get_header("Authorization"))
        _public_github_login_account.cache_clear()
