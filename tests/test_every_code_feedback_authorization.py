from __future__ import annotations

import unittest
from unittest.mock import patch

from pydantic import ValidationError

from control_plane.contracts.authz_policy_record import LaunchplaneAuthzPolicyRecord
from control_plane.every_code_feedback_authorization import (
    resolve_every_code_feedback_resume_actor,
    resolve_every_code_feedback_resume_worker,
)
from control_plane.service_auth import (
    GitHubHumanPolicyRule,
    LaunchplaneAuthzPolicy,
    LocalAdminIdentity,
    TerminalAgentIdentity,
    TerminalAgentPolicyRule,
    limited_remote_user_action_allowed,
)


def human_rule(**changes: object) -> GitHubHumanPolicyRule:
    return GitHubHumanPolicyRule.model_validate(
        {
            "managed_set_id": "feedback-test",
            "managed_rule_id": "human",
            "github_ids": (17,),
            "products": ("launchplane",),
            "contexts": ("launchplane",),
            "instances": ("github-repository:42",),
            "actions": ("every_code_feedback_resume.request",),
            **changes,
        }
    )


def worker_rule(**changes: object) -> TerminalAgentPolicyRule:
    return TerminalAgentPolicyRule.model_validate(
        {
            "managed_set_id": "feedback-test",
            "managed_rule_id": "worker",
            "subjects": ("test-worker",),
            "token_labels": ("test-token",),
            "products": ("launchplane",),
            "contexts": ("launchplane",),
            "instances": ("github-repository:42",),
            "actions": ("every_code_feedback_resume.execute",),
            **changes,
        }
    )


def policy_record(
    *,
    humans: tuple[GitHubHumanPolicyRule, ...] = (),
    workers: tuple[TerminalAgentPolicyRule, ...] = (),
) -> LaunchplaneAuthzPolicyRecord:
    return LaunchplaneAuthzPolicyRecord(
        record_id="test-policy-r7",
        revision=7,
        source="test",
        updated_at="2026-09-08T00:00:00Z",
        policy=LaunchplaneAuthzPolicy(
            schema_version=2, github_humans=humans, terminal_agents=workers
        ),
    )


class EveryCodeFeedbackAuthorizationTests(unittest.TestCase):
    def test_policy_cannot_coerce_boolean_or_string_into_immutable_github_id(self) -> None:
        for github_id in (True, "17", 17.0):
            with self.subTest(github_id=github_id), self.assertRaises(ValidationError):
                human_rule(github_ids=(github_id,))

    def test_exact_human_and_worker_evidence_binds_actual_policy(self) -> None:
        record = policy_record(humans=(human_rule(),), workers=(worker_rule(),))
        human = resolve_every_code_feedback_resume_actor(
            policy_record=record, github_id=17, repository_id=42
        )
        worker = resolve_every_code_feedback_resume_worker(
            policy_record=record,
            identity=TerminalAgentIdentity(subject="test-worker", token_label="test-token"),
            repository_id=42,
        )
        self.assertIsNotNone(human)
        self.assertIsNotNone(worker)
        assert human is not None and worker is not None
        self.assertEqual(human.policy_sha256, record.policy_sha256)
        self.assertEqual(worker.policy_revision, 7)
        self.assertNotEqual(human.action, worker.action)

    def test_mutable_or_broad_human_selectors_never_supply_capability(self) -> None:
        cases: tuple[dict[str, object], ...] = (
            {"logins": ("alice",)},
            {"organizations": ("example",)},
            {"teams": ("example/engineering",)},
            {"roles": ("admin",)},
            {"github_ids": ()},
            {"github_ids": (17, 18)},
            {"products": ()},
            {"products": ("*",)},
            {"products": ("launchplane", "other")},
            {"contexts": ()},
            {"contexts": ("*",)},
            {"instances": ("*",)},
            {"instances": ("github-repository:42", "github-repository:43")},
            {"actions": ()},
            {"actions": ("every_code_feedback_resume.request", "deployment.read")},
            {"managed_set_id": None, "managed_rule_id": None},
        )
        for changes in cases:
            with self.subTest(changes=changes):
                self.assertIsNone(
                    resolve_every_code_feedback_resume_actor(
                        policy_record=policy_record(humans=(human_rule(**changes),)),
                        github_id=17,
                        repository_id=42,
                    )
                )

    def test_ambiguous_exact_grants_deny_and_revocation_takes_effect(self) -> None:
        rules = (human_rule(), human_rule(managed_rule_id="human-duplicate"))
        self.assertIsNone(
            resolve_every_code_feedback_resume_actor(
                policy_record=policy_record(humans=rules), github_id=17, repository_id=42
            )
        )
        self.assertIsNone(
            resolve_every_code_feedback_resume_actor(
                policy_record=policy_record(), github_id=17, repository_id=42
            )
        )
        for actor, repository in ((True, 42), (17, True), (0, 42), (17, 43), (18, 42)):
            with self.subTest(actor=actor, repository=repository):
                self.assertIsNone(
                    resolve_every_code_feedback_resume_actor(
                        policy_record=policy_record(humans=(human_rule(),)),
                        github_id=actor,
                        repository_id=repository,
                    )
                )

    def test_worker_requires_both_literal_subject_and_token_label(self) -> None:
        identity = TerminalAgentIdentity(subject="test-worker", token_label="test-token")
        cases: tuple[dict[str, object], ...] = (
            {"subjects": ()},
            {"subjects": ("*",)},
            {"subjects": ("test-*",)},
            {"subjects": ("test-worker", "other")},
            {"token_labels": ()},
            {"token_labels": ("*",)},
            {"token_labels": ("test-token", "other")},
            {"actions": ()},
            {"instances": ("*",)},
            {"managed_set_id": None, "managed_rule_id": None},
        )
        for changes in cases:
            with self.subTest(changes=changes):
                self.assertIsNone(
                    resolve_every_code_feedback_resume_worker(
                        policy_record=policy_record(workers=(worker_rule(**changes),)),
                        identity=identity,
                        repository_id=42,
                    )
                )
        self.assertIsNone(
            resolve_every_code_feedback_resume_worker(
                policy_record=policy_record(workers=(worker_rule(),)),
                identity=LocalAdminIdentity(subject="test-worker", token_label="test-token"),
                repository_id=42,
            )
        )

    def test_matching_malformed_worker_subjects_and_labels_still_deny(self) -> None:
        for field in ("subject", "token_label"):
            for value in ("bad\nvalue", "bad\x00value", "bad?value", "bad[value", "x" * 257):
                with self.subTest(field=field, value=value):
                    identity = TerminalAgentIdentity(
                        subject=value if field == "subject" else "test-worker",
                        token_label=value if field == "token_label" else "test-token",
                    )
                    rule = worker_rule(
                        subjects=(identity.subject,), token_labels=(identity.token_label,)
                    )
                    self.assertIsNone(
                        resolve_every_code_feedback_resume_worker(
                            policy_record=policy_record(workers=(rule,)),
                            identity=identity,
                            repository_id=42,
                        )
                    )

    def test_request_permission_does_not_expand_other_restricted_actions(self) -> None:
        self.assertFalse(limited_remote_user_action_allowed("every_code_feedback_resume.request"))
        self.assertFalse(limited_remote_user_action_allowed("every_code_feedback_resume.execute"))
        self.assertFalse(limited_remote_user_action_allowed("authz_policy.write"))

    def test_github_gesture_does_not_synthesize_a_browser_role(self) -> None:
        record = policy_record(humans=(human_rule(),))
        with patch.object(LaunchplaneAuthzPolicy, "evaluate", side_effect=AssertionError):
            self.assertIsNotNone(
                resolve_every_code_feedback_resume_actor(
                    policy_record=record, github_id=17, repository_id=42
                )
            )


if __name__ == "__main__":
    unittest.main()


class FeedbackHumanPolicySchemaBoundaryTests(unittest.TestCase):
    def test_new_rule_constraints_require_explicit_feedback_resolver_review(self) -> None:
        from control_plane.service_auth import ScopedAuthzPolicyRule

        scope_fields = {
            "actions",
            "contexts",
            "instances",
            "managed_rule_id",
            "managed_set_id",
            "products",
        }
        self.assertEqual(set(ScopedAuthzPolicyRule.model_fields), scope_fields)
        self.assertEqual(
            set(GitHubHumanPolicyRule.model_fields),
            scope_fields | {"github_ids", "logins", "organizations", "roles", "teams"},
        )
