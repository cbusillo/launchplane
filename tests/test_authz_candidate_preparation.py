from __future__ import annotations

from dataclasses import dataclass
import unittest

from control_plane.authz_candidate_preparation import (
    AuthorizationCandidatePreparationError,
    ORDINARY_AGENT_DELIVERY_ADMINISTRATION_ACTIONS,
    ORDINARY_AGENT_DELIVERY_ADMINISTRATION_MANAGED_RULE_ID,
    ORDINARY_AGENT_DELIVERY_ADMINISTRATION_MANAGED_SET_ID,
    compile_ordinary_agent_delivery_administration_candidate,
    is_ordinary_agent_delivery_administration_request,
    ordinary_agent_delivery_administration_state,
)
from control_plane.service_auth import LaunchplaneAuthzPolicy
from tests.test_ordinary_agent_activation_storage import _record, _revoked


@dataclass
class _ActivationStore:
    records: tuple[object, ...] = ()

    def list_ordinary_agent_delivery_activation_records(
        self, *, limit: int | None = None
    ) -> tuple[object, ...]:
        return self.records[:limit]


def _policy(*, candidate_rule: dict[str, object] | None = None) -> LaunchplaneAuthzPolicy:
    rules: list[dict[str, object]] = [
        {
            "managed_set_id": "operator.policy-administration",
            "managed_rule_id": "policy-administrator",
            "github_ids": [123],
            "roles": ["admin"],
            "products": ["launchplane"],
            "contexts": ["launchplane"],
            "actions": ["authz_policy_grant.write", "authz_policy_operation.propose"],
        },
        {
            "managed_set_id": "unrelated.preserved",
            "managed_rule_id": "unrelated-rule",
            "github_ids": [456],
            "products": ["elsewhere"],
            "contexts": ["elsewhere"],
            "actions": [],
        },
    ]
    if candidate_rule is not None:
        rules.append(candidate_rule)
    return LaunchplaneAuthzPolicy.model_validate({"schema_version": 2, "github_humans": rules})


def _exact_rule(github_id: int = 123) -> dict[str, object]:
    return {
        "managed_set_id": ORDINARY_AGENT_DELIVERY_ADMINISTRATION_MANAGED_SET_ID,
        "managed_rule_id": ORDINARY_AGENT_DELIVERY_ADMINISTRATION_MANAGED_RULE_ID,
        "github_ids": [github_id],
        "roles": ["admin"],
        "products": ["launchplane"],
        "contexts": ["launchplane"],
        "actions": list(ORDINARY_AGENT_DELIVERY_ADMINISTRATION_ACTIONS),
    }


class AuthorizationCandidateCompilerTests(unittest.TestCase):
    def test_add_compiles_only_exact_closed_rule_and_preserves_schema(self) -> None:
        state, request = compile_ordinary_agent_delivery_administration_candidate(
            current_policy=_policy(),
            github_id=123,
            intent="add",
            record_store=_ActivationStore(),
        )

        self.assertEqual(state, "planned")
        self.assertIsNotNone(request)
        assert request is not None
        self.assertTrue(is_ordinary_agent_delivery_administration_request(request))
        self.assertEqual(request.desired_policy.schema_version, 2)
        self.assertEqual(request.schema_migration, "reject")
        self.assertIsNone(request.administrator_quorum_change)
        self.assertEqual(len(request.desired_policy.github_humans), 1)
        rule = request.desired_policy.github_humans[0]
        self.assertEqual(rule.github_ids, (123,))
        self.assertEqual(rule.roles, ("admin",))
        self.assertEqual(rule.products, ("launchplane",))
        self.assertEqual(rule.contexts, ("launchplane",))
        self.assertEqual(rule.actions, ORDINARY_AGENT_DELIVERY_ADMINISTRATION_ACTIONS)
        self.assertFalse(rule.logins or rule.organizations or rule.teams or rule.instances)
        self.assertFalse(
            is_ordinary_agent_delivery_administration_request(
                request.model_copy(update={"administrator_quorum_change": 2})
            )
        )

    def test_exact_desired_state_is_bounded_noop(self) -> None:
        active = _policy(candidate_rule=_exact_rule())
        self.assertEqual(
            ordinary_agent_delivery_administration_state(active, github_id=123),
            "active",
        )
        add_state, add_request = compile_ordinary_agent_delivery_administration_candidate(
            current_policy=active,
            github_id=123,
            intent="add",
            record_store=_ActivationStore(),
        )
        remove_state, remove_request = compile_ordinary_agent_delivery_administration_candidate(
            current_policy=_policy(),
            github_id=123,
            intent="remove",
            record_store=_ActivationStore(),
        )
        self.assertEqual((add_state, add_request), ("already_satisfied", None))
        self.assertEqual((remove_state, remove_request), ("already_satisfied", None))

    def test_remove_compiles_empty_set_through_standard_planner_shape(self) -> None:
        state, request = compile_ordinary_agent_delivery_administration_candidate(
            current_policy=_policy(candidate_rule=_exact_rule()),
            github_id=123,
            intent="remove",
            record_store=_ActivationStore(),
        )

        self.assertEqual(state, "planned")
        assert request is not None
        self.assertTrue(is_ordinary_agent_delivery_administration_request(request))
        self.assertEqual(request.desired_policy.github_humans, ())

    def test_occupied_malformed_and_explicit_overlap_fail_closed(self) -> None:
        occupied = _policy(candidate_rule=_exact_rule(456))
        overlap_payload = _policy().model_dump(mode="json")
        overlap_payload["terminal_agents"] = [
            {
                "managed_set_id": "other.set",
                "managed_rule_id": "other-rule",
                "subjects": ["agent:test"],
                "actions": [ORDINARY_AGENT_DELIVERY_ADMINISTRATION_ACTIONS[0]],
            }
        ]
        overlap = LaunchplaneAuthzPolicy.model_validate(overlap_payload)

        malformed_rule = _exact_rule()
        malformed_rule["actions"] = [
            *ORDINARY_AGENT_DELIVERY_ADMINISTRATION_ACTIONS,
            "authz_policy_grant.write",
        ]
        malformed = _policy(candidate_rule=malformed_rule)
        for policy, reason in (
            (occupied, "candidate_set_conflict"),
            (malformed, "candidate_set_conflict"),
            (overlap, "candidate_action_overlap"),
        ):
            with self.subTest(reason=reason):
                with self.assertRaises(AuthorizationCandidatePreparationError) as caught:
                    compile_ordinary_agent_delivery_administration_candidate(
                        current_policy=policy,
                        github_id=123,
                        intent="add",
                        record_store=_ActivationStore(),
                    )
                self.assertEqual(caught.exception.reason_code, reason)

        # Other explicit authority must not prevent reducing this isolated set.
        overlap_payload["github_humans"].append(_exact_rule())
        active_with_overlap = LaunchplaneAuthzPolicy.model_validate(overlap_payload)
        state, removal = compile_ordinary_agent_delivery_administration_candidate(
            current_policy=active_with_overlap,
            github_id=123,
            intent="remove",
            record_store=_ActivationStore(),
        )
        self.assertEqual(state, "planned")
        assert removal is not None
        self.assertEqual(removal.desired_policy.github_humans, ())

    def test_remove_requires_bounded_readable_activation_history(self) -> None:
        active = _policy(candidate_rule=_exact_rule())
        for store, expected_reason in (
            (object(), "activation_storage_unavailable"),
            (_ActivationStore(records=(object(),)), "activation_storage_unavailable"),
            (_ActivationStore(records=(object(),) * 1001), "activation_history_truncated"),
        ):
            with self.subTest(reason=expected_reason):
                with self.assertRaises(AuthorizationCandidatePreparationError) as caught:
                    compile_ordinary_agent_delivery_administration_candidate(
                        current_policy=active,
                        github_id=123,
                        intent="remove",
                        record_store=store,
                    )
                self.assertEqual(caught.exception.reason_code, expected_reason)

    def test_removal_needs_stop_even_after_activation_expiry(self) -> None:
        activation = _record(
            operation_id="test-candidate-preparation",
            installed_at="2026-09-10T20:00:00Z",
            expires_at="2026-09-10T21:00:00Z",
        )
        active = _policy(candidate_rule=_exact_rule())
        with self.assertRaises(AuthorizationCandidatePreparationError) as caught:
            compile_ordinary_agent_delivery_administration_candidate(
                current_policy=active,
                github_id=123,
                intent="remove",
                record_store=_ActivationStore(records=(activation,)),
            )
        self.assertEqual(caught.exception.reason_code, "current_activation_requires_stop")
        state, request = compile_ordinary_agent_delivery_administration_candidate(
            current_policy=active,
            github_id=123,
            intent="remove",
            record_store=_ActivationStore(
                records=(_revoked(activation, occurred_at="2026-09-10T22:00:00Z"),)
            ),
        )
        self.assertEqual(state, "planned")
        self.assertIsNotNone(request)

    def test_schema_three_and_sensitive_changes_cannot_get_closed_candidate_summary(self) -> None:
        payload = _policy().model_dump(mode="json")
        payload["schema_version"] = 3
        policy = LaunchplaneAuthzPolicy.model_validate(payload)
        _, request = compile_ordinary_agent_delivery_administration_candidate(
            current_policy=policy,
            github_id=123,
            intent="add",
            record_store=_ActivationStore(),
        )
        assert request is not None
        self.assertEqual(request.desired_policy.schema_version, 3)
        self.assertTrue(is_ordinary_agent_delivery_administration_request(request))
        for change in (
            {"administrator_quorum_change": 1},
            {"schema_migration": "migrate_v2_to_v3"},
        ):
            with self.subTest(change=change):
                modified = type(request).model_validate(
                    {**request.model_dump(mode="json"), **change}
                )
                self.assertFalse(is_ordinary_agent_delivery_administration_request(modified))
