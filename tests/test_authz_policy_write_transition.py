from __future__ import annotations

from datetime import datetime, timezone
import unittest

from control_plane.contracts.authz_policy_write_transition import (
    AuthzPolicySchemaV3TransitionDeniedError,
    classify_authz_policy_schema_v3_transition,
    require_authz_policy_source_status,
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
    def test_v2_to_v3_requires_one_ordinary_scope(self) -> None:
        result = classify_authz_policy_schema_v3_transition(
            _policy(2), _policy(3, (_ordinary_rule(),))
        )
        self.assertEqual(result.kind, "v2_to_v3_enable")
        self.assertEqual(
            result.added_or_changed_ordinary_rule_keys,
            ("ordinary-agent.pilot\x1fagent-one",),
        )
        with self.assertRaises(AuthzPolicySchemaV3TransitionDeniedError):
            classify_authz_policy_schema_v3_transition(_policy(2), _policy(3))

    def test_exact_carry_and_removal_are_maintenance(self) -> None:
        active = _policy(3, (_ordinary_rule(),))
        carried = classify_authz_policy_schema_v3_transition(active, active)
        removed = classify_authz_policy_schema_v3_transition(active, _policy(3))
        self.assertEqual(carried.kind, "v3_maintenance")
        self.assertEqual(removed.kind, "v3_maintenance")
        self.assertEqual(removed.removed_ordinary_rule_keys, ("ordinary-agent.pilot\x1fagent-one",))

    def test_any_rule_update_is_enabling(self) -> None:
        result = classify_authz_policy_schema_v3_transition(
            _policy(3, (_ordinary_rule(),)),
            _policy(3, (_ordinary_rule(actions=("self_read",)),)),
        )
        self.assertEqual(result.kind, "v3_enable_or_expand")

    def test_source_status_includes_executing_and_fences_expired_plan(self) -> None:
        now = datetime(2026, 9, 10, 12, tzinfo=timezone.utc)
        require_authz_policy_source_status(
            status="executing", expires_at="2026-09-10T11:00:00+00:00", observed_at=now
        )
        with self.assertRaises(AuthzPolicySchemaV3TransitionDeniedError):
            require_authz_policy_source_status(
                status="approved", expires_at="2026-09-10T12:00:00+00:00", observed_at=now
            )


if __name__ == "__main__":
    unittest.main()
