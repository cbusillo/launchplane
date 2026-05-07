from __future__ import annotations

import json
import unittest
from typing import cast

from control_plane.authz_grant_service import (
    AuthzPolicyGitHubActionsGrant,
    AuthzPolicyGitHubActionsGrantEnvelope,
    authz_policy_grant_response_audit_payload,
    build_authz_policy_grant_service_result,
    plan_github_actions_authz_policy_grant,
    write_github_actions_authz_policy_grant,
)
from control_plane.contracts.authz_policy_record import (
    LaunchplaneAuthzPolicyRecord,
    authz_policy_sha256,
    build_authz_policy_record_id,
)
from control_plane.service_auth import GitHubActionsIdentity, LaunchplaneAuthzPolicy


class _AuthzPolicyStore:
    def __init__(self, records: tuple[LaunchplaneAuthzPolicyRecord, ...]) -> None:
        self.records = records
        self.written_records: list[LaunchplaneAuthzPolicyRecord] = []

    def list_authz_policy_records(
        self,
        *,
        status: str = "",
        limit: int | None = None,
    ) -> tuple[LaunchplaneAuthzPolicyRecord, ...]:
        records = tuple(record for record in self.records if not status or record.status == status)
        if limit is not None:
            return records[:limit]
        return records

    def write_authz_policy_record(self, record: LaunchplaneAuthzPolicyRecord) -> None:
        self.written_records.append(record)
        self.records = (record,) + self.records


def _identity() -> GitHubActionsIdentity:
    return GitHubActionsIdentity(
        repository="cbusillo/launchplane",
        repository_owner="cbusillo",
        workflow_ref="cbusillo/launchplane/.github/workflows/deploy-launchplane.yml@refs/heads/main",
        job_workflow_ref="",
        event_name="workflow_dispatch",
        ref="refs/heads/main",
        ref_type="branch",
        environment="",
        subject="repo:cbusillo/launchplane:ref:refs/heads/main",
        sha="abc123",
        raw_claims={},
    )


def _active_record() -> LaunchplaneAuthzPolicyRecord:
    policy = LaunchplaneAuthzPolicy.model_validate(
        {
            "github_actions": [
                {
                    "repository": "cbusillo/launchplane",
                    "products": ["launchplane"],
                    "contexts": ["launchplane"],
                    "actions": ["launchplane_service_deploy.execute"],
                }
            ]
        }
    )
    policy_sha256 = authz_policy_sha256(policy)
    return LaunchplaneAuthzPolicyRecord(
        record_id=build_authz_policy_record_id(
            updated_at="2026-05-07T15:00:00Z",
            policy_sha256=policy_sha256,
        ),
        status="active",
        source="test:bootstrap",
        updated_at="2026-05-07T15:00:00Z",
        policy_sha256=policy_sha256,
        policy=policy,
    )


def _grant_request(mode: str = "apply") -> AuthzPolicyGitHubActionsGrantEnvelope:
    return AuthzPolicyGitHubActionsGrantEnvelope.model_validate(
        {
            "product": "launchplane",
            "mode": mode,
            "reason": "Grant product profile read for SYO promotion diagnostics.",
            "related_issue": "cbusillo/launchplane#83",
            "grant": {
                "repository": "cbusillo/launchplane",
                "workflow_refs": [
                    "cbusillo/launchplane/.github/workflows/deploy-launchplane.yml@refs/heads/main"
                ],
                "event_names": ["workflow_dispatch"],
                "products": ["sellyouroutboard"],
                "contexts": ["launchplane"],
                "actions": ["product_profile.read"],
                "source_label": "test:audit-grant",
            },
        }
    )


class AuthzGrantServiceTests(unittest.TestCase):
    def test_grant_normalizes_tuple_values(self) -> None:
        grant = AuthzPolicyGitHubActionsGrant.model_validate(
            {
                "repository": " cbusillo/launchplane ",
                "workflow_refs": [" workflow ", ""],
                "actions": [" product_profile.read "],
            }
        )

        self.assertEqual(grant.repository, "cbusillo/launchplane")
        self.assertEqual(grant.workflow_refs, ("workflow",))
        self.assertEqual(grant.actions, ("product_profile.read",))

    def test_plan_and_write_grant_records_changed_policy(self) -> None:
        store = _AuthzPolicyStore((_active_record(),))
        request = _grant_request()

        _current_policy, current_record, diff = plan_github_actions_authz_policy_grant(
            record_store=store,
            grant=request.grant,
        )
        self.assertEqual(current_record.record_id, store.records[0].record_id)
        self.assertEqual(diff["changed"], True)
        self.assertEqual(diff["new_github_actions_rule_count"], 2)

        updated_policy, record, changed, write_diff, audit = (
            write_github_actions_authz_policy_grant(
                record_store=store,
                request=request,
                identity=_identity(),
                trace_id="trace-1",
                now_timestamp=lambda: "2026-05-07T16:00:00Z",
            )
        )

        self.assertTrue(changed)
        self.assertEqual(write_diff, diff)
        self.assertEqual(store.written_records, [record])
        self.assertEqual(len(updated_policy.github_actions), 2)
        self.assertEqual(audit["reason"], request.reason)
        self.assertIn("workflow_refs", json.dumps(audit, sort_keys=True))

        result, driver_result = build_authz_policy_grant_service_result(
            authz_policy_record=record,
            changed=changed,
            mode=request.mode,
            diff=write_diff,
            audit=audit,
        )
        self.assertEqual(result["authz_policy_record_id"], record.record_id)
        driver_audit = cast(dict[str, object], driver_result["audit"])
        self.assertNotIn("requested_grant", driver_audit)
        requested_grant_summary = cast(dict[str, object], driver_audit["requested_grant_summary"])
        self.assertEqual(requested_grant_summary["workflow_ref_count"], 1)

    def test_repeated_grant_does_not_write_new_record(self) -> None:
        store = _AuthzPolicyStore((_active_record(),))
        request = _grant_request()
        _updated_policy, record, _changed, _diff, _audit = write_github_actions_authz_policy_grant(
            record_store=store,
            request=request,
            identity=_identity(),
            trace_id="trace-1",
            now_timestamp=lambda: "2026-05-07T16:00:00Z",
        )

        _same_policy, same_record, same_changed, _same_diff, same_audit = (
            write_github_actions_authz_policy_grant(
                record_store=store,
                request=request,
                identity=_identity(),
                trace_id="trace-2",
                now_timestamp=lambda: "2026-05-07T16:01:00Z",
            )
        )

        self.assertFalse(same_changed)
        self.assertEqual(same_record.record_id, record.record_id)
        self.assertEqual(len(store.written_records), 1)
        self.assertEqual(same_audit["changed"], False)

    def test_response_audit_replaces_requested_grant_with_summary(self) -> None:
        audit: dict[str, object] = {
            "requested_grant": _grant_request().grant.to_policy_rule().model_dump(mode="json"),
            "mode": "dry_run",
        }

        response_audit = authz_policy_grant_response_audit_payload(audit)

        self.assertNotIn("requested_grant", response_audit)
        requested_grant_summary = cast(dict[str, object], response_audit["requested_grant_summary"])
        self.assertEqual(requested_grant_summary["actions"], ["product_profile.read"])


if __name__ == "__main__":
    unittest.main()
