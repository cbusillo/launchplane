from __future__ import annotations

import unittest

from control_plane.contracts.authz_policy_write_transition import (
    AuthzPolicySchemaV3TransitionDeniedError,
    classify_authz_policy_schema_v3_transition,
)
from control_plane.service_auth import LaunchplaneAuthzPolicy


def _ordinary_rule(*, actions: tuple[str, ...] = ("self_read", "preflight")) -> dict[str, object]:
    return {
        "managed_set_id": "ordinary-agent.pilot",
        "managed_rule_id": "agent-one",
        "principal_id": "agent_one",
        "target": {
            "repository_id": 1001,
            "repository": "example/launchplane",
            "base_branch": "main",
        },
        "actions": actions,
    }


def _policy(
    schema_version: int, rules: tuple[dict[str, object], ...] = ()
) -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(
        {"schema_version": schema_version, "ordinary_agents": rules}
    )


class AuthzPolicyWriteTransitionTests(unittest.TestCase):
    def test_adding_or_widening_ordinary_rules_is_refused(self) -> None:
        for current, candidate in (
            (_policy(2), _policy(3, (_ordinary_rule(),))),
            (_policy(2), _policy(3)),
            (_policy(3), _policy(3, (_ordinary_rule(),))),
            (
                _policy(3, (_ordinary_rule(),)),
                _policy(3, (_ordinary_rule(actions=("self_read",)),)),
            ),
        ):
            with self.subTest(current=current.schema_version, rules=len(candidate.ordinary_agents)):
                with self.assertRaises(AuthzPolicySchemaV3TransitionDeniedError):
                    classify_authz_policy_schema_v3_transition(current, candidate)

    def test_exact_carry_and_removal_are_maintenance(self) -> None:
        active = _policy(3, (_ordinary_rule(),))
        carried = classify_authz_policy_schema_v3_transition(active, active)
        removed = classify_authz_policy_schema_v3_transition(active, _policy(3))
        self.assertEqual(carried.kind, "v3_maintenance")
        self.assertEqual(removed.kind, "v3_maintenance")


if __name__ == "__main__":
    unittest.main()
