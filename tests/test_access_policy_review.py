import unittest
from unittest.mock import Mock, patch

from control_plane.access_policy_review import (
    build_access_policy_review,
    _public_github_account_login,
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
        return build_access_policy_review(record_store=store, request=request, evidence=evidence)

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
        self.assertIn("Local operator", rule.principal)
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
