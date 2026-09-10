from __future__ import annotations

import json
import unittest

from control_plane.authz_diagnostics import (
    AuthzDiagnosticEvaluateEnvelope,
    evaluate_github_actions_authz,
)
from control_plane.service_auth import (
    AuthorizationTarget,
    GitHubActionsIdentity,
    LaunchplaneAuthzPolicy,
)


class AuthzDiagnosticsTests(unittest.TestCase):
    def test_exact_instance_diagnostics_agree_with_evaluator(self) -> None:
        action = "route_binding.external.plan"
        for version in (2, 3):
            policy_payload = _policy(job_workflow_ref="expected-worker").model_dump(mode="json")
            policy_payload["schema_version"] = version
            policy_payload["github_actions"][0].update(actions=[action], instances=["instance-a"])
            policy = LaunchplaneAuthzPolicy.model_validate(policy_payload)
            for case, target, identity in (
                (
                    "exact",
                    AuthorizationTarget(scope="instance", instances=("instance-a",)),
                    _identity(),
                ),
                (
                    "wrong-instance",
                    AuthorizationTarget(scope="instance", instances=("instance-b",)),
                    _identity(),
                ),
                ("missing-instance", AuthorizationTarget(scope="context"), _identity()),
                (
                    "selector",
                    AuthorizationTarget(scope="instance", instances=("instance-a",)),
                    _identity(job_workflow_ref="other-worker"),
                ),
                (
                    "identity",
                    AuthorizationTarget(scope="instance", instances=("instance-a",)),
                    _identity(repository="example-org/other-product"),
                ),
            ):
                with self.subTest(schema_version=version, case=case):
                    request = AuthzDiagnosticEvaluateEnvelope(
                        action=action,
                        product="example-product",
                        context="example-preview",
                        target=target,
                    )
                    diagnostic = evaluate_github_actions_authz(
                        policy=policy,
                        identity=identity,
                        request=request,
                    )
                    actual = policy.evaluate(
                        identity=identity,
                        action=action,
                        product=request.product,
                        context=request.context,
                        target=target,
                        record_context=False,
                    )
                    self.assertEqual(diagnostic.decision, actual.decision)
                    self.assertEqual(
                        diagnostic.decision, "allowed" if case == "exact" else "denied"
                    )

    def test_schema_one_retains_legacy_unpinned_instance_diagnostics(self) -> None:
        payload = _policy(job_workflow_ref="expected-worker").model_dump(mode="json")
        payload["schema_version"] = 1
        policy = LaunchplaneAuthzPolicy.model_validate(payload)
        request = _request().model_copy(
            update={"target": AuthorizationTarget(scope="instance", instances=("instance-a",))}
        )
        diagnostic = evaluate_github_actions_authz(
            policy=policy, identity=_identity(), request=request
        )
        self.assertTrue(
            policy.allows(
                identity=_identity(),
                action=request.action,
                product=request.product,
                context=request.context,
                target=request.target,
            )
        )
        self.assertEqual(diagnostic.decision, "allowed")

    def test_reports_only_failed_selector_categories_for_closest_rule(self) -> None:
        identity = _identity(job_workflow_ref="actual-worker")
        policy = _policy(job_workflow_ref="expected-worker")

        evaluation = evaluate_github_actions_authz(
            policy=policy,
            identity=identity,
            request=_request(),
        )

        self.assertEqual(evaluation.decision, "denied")
        self.assertEqual(evaluation.failure_categories, ("job_workflow_ref",))
        self.assertEqual(len(evaluation.rule_evaluations), 1)
        self.assertEqual(
            evaluation.rule_evaluations[0].failure_categories,
            ("job_workflow_ref",),
        )
        rendered = json.dumps(evaluation.model_dump(mode="json"), sort_keys=True)
        self.assertNotIn("expected-worker", rendered)
        self.assertNotIn("actual-worker", rendered)
        self.assertNotIn("example-product", rendered)

    def test_reports_allowed_when_the_calling_identity_matches(self) -> None:
        identity = _identity(job_workflow_ref="expected-worker")

        evaluation = evaluate_github_actions_authz(
            policy=_policy(job_workflow_ref="expected-worker"),
            identity=identity,
            request=_request(),
        )

        self.assertEqual(evaluation.decision, "allowed")
        self.assertEqual(evaluation.failure_categories, ())
        self.assertEqual(evaluation.rule_evaluations[0].decision, "allowed")

    def test_matches_claims_with_the_same_whitespace_normalization_as_policy(self) -> None:
        identity = _identity(job_workflow_ref=" expected-worker ")
        policy = _policy(job_workflow_ref="expected-worker")

        self.assertTrue(
            policy.allows(
                identity=identity,
                action="preview_refresh.execute",
                product="example-product",
                context="example-preview",
                target=AuthorizationTarget(scope="preview"),
            )
        )
        evaluation = evaluate_github_actions_authz(
            policy=policy,
            identity=identity,
            request=_request(),
        )

        self.assertEqual(evaluation.decision, "allowed")

    def test_limits_rule_fingerprints_when_more_than_three_rules_match_repository(self) -> None:
        base_rule = _policy(job_workflow_ref="expected-worker").github_actions[0]
        policy = LaunchplaneAuthzPolicy.model_validate(
            {
                "schema_version": 2,
                "github_actions": [
                    {
                        **base_rule.model_dump(mode="json", exclude_none=True),
                        "job_workflow_refs": [f"expected-worker-{index}"],
                    }
                    for index in range(4)
                ],
            }
        )

        evaluation = evaluate_github_actions_authz(
            policy=policy,
            identity=_identity(job_workflow_ref="actual-worker"),
            request=_request(),
        )

        self.assertEqual(evaluation.decision, "denied")
        self.assertEqual(len(evaluation.rule_evaluations), 3)
        self.assertTrue(evaluation.rule_evaluations_truncated)

    def test_reports_repository_when_no_candidate_rule_exists(self) -> None:
        evaluation = evaluate_github_actions_authz(
            policy=_policy(job_workflow_ref="expected-worker"),
            identity=_identity(repository="example-org/other-product"),
            request=_request(),
        )

        self.assertEqual(evaluation.decision, "denied")
        self.assertEqual(evaluation.failure_categories, ("repository",))
        self.assertEqual(evaluation.rule_evaluations, ())


def _request() -> AuthzDiagnosticEvaluateEnvelope:
    return AuthzDiagnosticEvaluateEnvelope(
        action="preview_refresh.execute",
        product="example-product",
        context="example-preview",
        target=AuthorizationTarget(scope="preview"),
    )


def _identity(
    *,
    repository: str = "example-org/example-product",
    job_workflow_ref: str = "expected-worker",
) -> GitHubActionsIdentity:
    return GitHubActionsIdentity(
        repository=repository,
        repository_owner="example-org",
        workflow_ref=(
            "example-org/example-product/.github/workflows/preview.yml@refs/pull/42/merge"
        ),
        job_workflow_ref=job_workflow_ref,
        ref="refs/pull/42/merge",
        ref_type="branch",
        event_name="pull_request",
        environment="",
        subject="repo:example-org/example-product:pull_request",
        sha="a" * 40,
        raw_claims={},
        repository_id="1001",
        repository_owner_id="2001",
    )


def _policy(*, job_workflow_ref: str) -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "schema_version": 2,
            "github_actions": [
                {
                    "repository": "example-org/example-product",
                    "repository_id": "1001",
                    "repository_owner_id": "2001",
                    "workflow_refs": [
                        "example-org/example-product/.github/workflows/preview.yml@refs/pull/*/merge"
                    ],
                    "job_workflow_refs": [job_workflow_ref],
                    "event_names": ["pull_request"],
                    "products": ["example-product"],
                    "contexts": ["example-preview"],
                    "actions": ["preview_refresh.execute"],
                }
            ],
        }
    )
