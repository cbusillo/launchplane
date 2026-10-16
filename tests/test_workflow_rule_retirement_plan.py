from __future__ import annotations

import unittest

from control_plane.authz_grant_service import _authz_rule_sha256
from control_plane.authz_rule_retirement import (
    ROUTE,
    WorkflowRuleRetirementRequest,
    workflow_rule_retirement_plan,
)
from control_plane.contracts.authz_policy_record import authz_policy_sha256
from control_plane.service_auth import LaunchplaneAuthzPolicy
from tests.support.http import lifespan_client
from tests.test_authz_administration_read import _record, _operator_read_policy, _database_app


def mixed_policy() -> LaunchplaneAuthzPolicy:
    base = _operator_read_policy().model_dump(mode="json")
    base["github_humans"] = [
        {
            "github_ids": [101, 202],
            "roles": ["admin"],
            "products": ["launchplane"],
            "contexts": ["launchplane"],
            "actions": ["authz_policy_grant.write"],
        }
    ]
    base["github_actions"] = [
        {
            "repository": "example/site",
            "managed_set_id": "operator.mixed",
            "managed_rule_id": name,
            "workflow_refs": [f"example/site/.github/workflows/{name}.yml@refs/heads/main"],
            "products": ["site"],
            "contexts": ["site"],
            "instances": ["testing"],
            "actions": ["product_environment.read"],
        }
        for name in ("obsolete", "authz-operator", "prod-caller")
    ] + [
        {
            "repository": "example/site",
            "workflow_refs": ["example/*/.github/workflows/*.yml@*"],
            "job_workflow_refs": ["example/shared/.github/workflows/deploy.yml@*"],
            "products": ["site", "another-site"],
            "contexts": ["*"],
            "actions": ["product_environment.read"],
        }
    ]
    return LaunchplaneAuthzPolicy.model_validate(base)


def request(
    policy: LaunchplaneAuthzPolicy, *, index: int = 0, **changes: object
) -> WorkflowRuleRetirementRequest:
    active = _record(policy=policy)
    rule = policy.github_actions[index]
    return WorkflowRuleRetirementRequest.model_validate(
        {
            "policy_record_id": active.record_id,
            "policy_revision": active.revision,
            "policy_sha256": active.policy_sha256,
            "mode": "plan",
            "selection": [
                {
                    "rule_sha256": _authz_rule_sha256(rule),
                    "managed_set_id": rule.managed_set_id,
                    "managed_rule_id": rule.managed_rule_id,
                }
            ],
            **changes,
        }
    )


class WorkflowRulePlanTests(unittest.TestCase):
    def test_mixed_set_and_unmanaged_survivors_remain_exact(self) -> None:
        policy = mixed_policy()
        result = workflow_rule_retirement_plan(_record(policy=policy), request(policy))
        candidate = LaunchplaneAuthzPolicy.model_validate(
            {
                **policy.model_dump(mode="json"),
                "github_actions": [r.model_dump(mode="json") for r in policy.github_actions[1:]],
            }
        )
        self.assertEqual(result["candidate_policy_sha256"], authz_policy_sha256(candidate))
        self.assertFalse(result["apply_supported"])
        selected = result["selected_rules"][0]  # type: ignore[index]
        self.assertEqual(
            selected["scope"], policy.github_actions[0].model_dump(mode="json", exclude_none=True)
        )
        self.assertFalse(result["blockers"])

    def test_unmanaged_broad_and_shared_rule_scope_is_explicit(self) -> None:
        policy = mixed_policy()
        result = workflow_rule_retirement_plan(_record(policy=policy), request(policy, index=-1))
        selected = result["selected_rules"][0]  # type: ignore[index]
        self.assertEqual(selected["management"], "unmanaged")
        self.assertTrue(selected["broad_or_shared_scope"])
        self.assertEqual(selected["scope"]["products"], list(policy.github_actions[-1].products))
        self.assertEqual(result["dependency_verification"], "unknown_requires_source_review")

    def test_policy_drift_fingerprint_identity_and_duplicate_selection_refuse(self) -> None:
        policy = mixed_policy()
        active = _record(policy=policy)
        body = request(policy)
        for change in (
            {"policy_revision": active.revision + 1},
            {"policy_record_id": "stale"},
            {"selection": [{"rule_sha256": "0" * 64}]},
            {"selection": [{**body.selection[0].model_dump(), "managed_rule_id": "wrong"}]},
        ):
            with self.subTest(change=change), self.assertRaises(ValueError):
                workflow_rule_retirement_plan(active, request(policy, **change))
        with self.assertRaises(ValueError):
            request(policy, selection=[body.selection[0], body.selection[0]])
        ambiguous = LaunchplaneAuthzPolicy.model_validate(
            {
                **policy.model_dump(mode="json"),
                "github_actions": [policy.github_actions[-1].model_dump(mode="json")] * 2,
            }
        )
        with self.assertRaises(ValueError):
            workflow_rule_retirement_plan(_record(policy=ambiguous), request(ambiguous))

    def test_missing_human_admin_is_a_blocker_even_for_workflow_only_contraction(self) -> None:
        policy = mixed_policy().model_copy(update={"github_humans": ()})
        result = workflow_rule_retirement_plan(_record(policy=policy), request(policy))
        self.assertEqual(result["blockers"], ["last_administrator_protection"])

    def test_inspect_returns_full_scopes_without_inventing_a_removal_or_apply(self) -> None:
        policy = mixed_policy()
        result = workflow_rule_retirement_plan(
            _record(policy=policy), request(policy, mode="inspect")
        )
        self.assertNotIn("candidate_policy_sha256", result)
        self.assertFalse(result["apply_supported"])
        with self.assertRaises(ValueError):
            request(policy, mode="apply")


class WorkflowRulePlanHttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_director_standing_read_works_without_policy_read_grant(self) -> None:
        policy = mixed_policy()
        payload = policy.model_dump(mode="json")
        payload["local_operators"] = []
        with _database_app(LaunchplaneAuthzPolicy.model_validate(payload), director_agent=True) as (
            _,
            active,
            app,
        ):
            body = request(policy).model_copy(
                update={
                    "policy_record_id": active.record_id,
                    "policy_revision": active.revision,
                    "policy_sha256": active.policy_sha256,
                }
            )
            async with lifespan_client(app) as client:
                result = await client.post(
                    ROUTE,
                    json=body.model_dump(mode="json"),
                    headers={"Authorization": "Bearer reader-token"},
                )
            self.assertEqual(result.status_code, 200, result.text)
            self.assertFalse(result.json()["result"]["apply_supported"])

    async def test_explicit_admin_reader_gets_exact_scopes_without_a_policy_write(self) -> None:
        policy = mixed_policy()
        with _database_app(policy) as (store, active, app):
            before = store.list_authz_policy_records()
            async with lifespan_client(app) as client:
                result = await client.post(
                    ROUTE,
                    json=request(policy).model_dump(mode="json"),
                    headers={"Authorization": "Bearer reader-token"},
                )
            self.assertEqual(result.status_code, 200, result.text)
            self.assertEqual(result.headers["cache-control"], "no-store")
            self.assertEqual(store.list_authz_policy_records(), before)
            self.assertEqual(result.json()["result"]["policy_sha256"], active.policy_sha256)

    async def test_client_scope_cannot_read_whole_rule_selectors(self) -> None:
        policy = mixed_policy()
        # An ordinary Client with only product read coverage cannot use this full export.
        payload = policy.model_dump(mode="json")
        payload["local_operators"][0]["actions"] = ["product_environment.read"]
        with _database_app(LaunchplaneAuthzPolicy.model_validate(payload)) as (_, _, app):
            async with lifespan_client(app) as client:
                result = await client.post(
                    ROUTE,
                    json=request(policy).model_dump(mode="json"),
                    headers={"Authorization": "Bearer reader-token"},
                )
            self.assertEqual(result.status_code, 403, result.text)
