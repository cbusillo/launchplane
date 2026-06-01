from __future__ import annotations

import json
import unittest
from typing import cast

from control_plane.authz_grant_service import (
    AuthzPolicyGitHubActionsGrant,
    AuthzPolicyGitHubActionsGrantEnvelope,
    AuthzPolicyGitHubActionsRemovalEnvelope,
    AuthzPolicyGitHubHumanGrant,
    AuthzPolicyGitHubHumanGrantEnvelope,
    authz_policy_grant_response_audit_payload,
    build_authz_policy_github_actions_removal_service_result,
    build_authz_policy_grant_service_result,
    plan_github_actions_authz_policy_removal,
    plan_github_actions_authz_policy_grant,
    plan_github_human_authz_policy_grant,
    write_github_actions_authz_policy_removal,
    write_github_actions_authz_policy_grant,
    write_github_human_authz_policy_grant,
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


def _removal_request(mode: str = "apply") -> AuthzPolicyGitHubActionsRemovalEnvelope:
    return AuthzPolicyGitHubActionsRemovalEnvelope.model_validate(
        {
            "product": "launchplane",
            "mode": mode,
            "reason": "Remove broad Launchplane deploy authority after narrowing routes.",
            "related_issue": "cbusillo/launchplane#1049",
            "removal": {
                "repository": "cbusillo/launchplane",
                "products": ["launchplane"],
                "contexts": ["launchplane"],
                "actions": ["launchplane_service_deploy.execute"],
                "source_label": "test:authz-removal",
            },
        }
    )


def _human_grant_request(mode: str = "apply") -> AuthzPolicyGitHubHumanGrantEnvelope:
    return AuthzPolicyGitHubHumanGrantEnvelope.model_validate(
        {
            "product": "launchplane",
            "mode": mode,
            "reason": "Grant SYO promotion workflow dispatch to the operator.",
            "related_issue": "cbusillo/launchplane#153",
            "grant": {
                "logins": ["cbusillo"],
                "roles": ["admin"],
                "products": ["sellyouroutboard"],
                "contexts": ["sellyouroutboard"],
                "actions": ["generic_web_prod_promotion.dispatch"],
                "source_label": "test:human-grant",
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

    def test_human_grant_normalizes_tuple_values(self) -> None:
        grant = AuthzPolicyGitHubHumanGrant.model_validate(
            {
                "logins": [" cbusillo ", ""],
                "roles": ["admin"],
                "actions": [" generic_web_prod_promotion.dispatch "],
            }
        )

        self.assertEqual(grant.logins, ("cbusillo",))
        self.assertEqual(grant.roles, ("admin",))
        self.assertEqual(grant.actions, ("generic_web_prod_promotion.dispatch",))

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
        self.assertEqual(write_diff["new_github_humans_rule_count"], 0)

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

    def test_plan_and_write_human_grant_records_changed_policy(self) -> None:
        store = _AuthzPolicyStore((_active_record(),))
        request = _human_grant_request()

        _current_policy, current_record, diff = plan_github_human_authz_policy_grant(
            record_store=store,
            grant=request.grant,
        )
        self.assertEqual(current_record.record_id, store.records[0].record_id)
        self.assertEqual(diff["changed"], True)
        self.assertEqual(diff["new_github_actions_rule_count"], 1)
        self.assertEqual(diff["new_github_humans_rule_count"], 1)

        updated_policy, record, changed, write_diff, audit = (
            write_github_human_authz_policy_grant(
                record_store=store,
                request=request,
                identity=_identity(),
                trace_id="trace-human",
                now_timestamp=lambda: "2026-05-07T16:00:00Z",
            )
        )

        self.assertTrue(changed)
        self.assertEqual(write_diff, diff)
        self.assertEqual(store.written_records, [record])
        self.assertEqual(len(updated_policy.github_humans), 1)
        self.assertEqual(audit["reason"], request.reason)
        self.assertIn("logins", json.dumps(audit, sort_keys=True))

        result, driver_result = build_authz_policy_grant_service_result(
            authz_policy_record=record,
            changed=changed,
            mode=request.mode,
            diff=write_diff,
            audit=audit,
        )
        self.assertEqual(result["authz_policy_record_id"], record.record_id)
        driver_audit = cast(dict[str, object], driver_result["audit"])
        requested_grant_summary = cast(dict[str, object], driver_audit["requested_grant_summary"])
        self.assertEqual(requested_grant_summary["principal_type"], "github_human")
        self.assertEqual(requested_grant_summary["login_count"], 1)
        self.assertNotIn("logins", requested_grant_summary)

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

    def test_plan_and_write_removal_records_changed_policy(self) -> None:
        store = _AuthzPolicyStore((_active_record(),))
        request = _removal_request()

        _current_policy, current_record, diff = plan_github_actions_authz_policy_removal(
            record_store=store,
            removal=request.removal,
        )
        self.assertEqual(current_record.record_id, store.records[0].record_id)
        self.assertEqual(diff["changed"], True)
        self.assertEqual(diff["matched_rule_count"], 1)
        self.assertEqual(diff["new_github_actions_rule_count"], 0)

        updated_policy, record, changed, write_diff, audit = (
            write_github_actions_authz_policy_removal(
                record_store=store,
                request=request,
                identity=_identity(),
                trace_id="trace-remove",
                now_timestamp=lambda: "2026-05-07T16:00:00Z",
            )
        )

        self.assertTrue(changed)
        self.assertEqual(write_diff, diff)
        self.assertEqual(store.written_records, [record])
        self.assertEqual(updated_policy.github_actions, ())
        self.assertEqual(audit["reason"], request.reason)
        self.assertIn("requested_removal", audit)

        result, driver_result = build_authz_policy_github_actions_removal_service_result(
            authz_policy_record=record,
            changed=changed,
            mode=request.mode,
            diff=write_diff,
            audit=audit,
        )
        self.assertEqual(result["authz_policy_record_id"], record.record_id)
        driver_audit = cast(dict[str, object], driver_result["audit"])
        self.assertNotIn("requested_removal", driver_audit)
        requested_removal_summary = cast(
            dict[str, object], driver_audit["requested_removal_summary"]
        )
        self.assertEqual(
            requested_removal_summary["actions"], ["launchplane_service_deploy.execute"]
        )

    def test_repeated_removal_does_not_write_new_record(self) -> None:
        store = _AuthzPolicyStore((_active_record(),))
        request = _removal_request()
        _updated_policy, record, _changed, _diff, _audit = (
            write_github_actions_authz_policy_removal(
                record_store=store,
                request=request,
                identity=_identity(),
                trace_id="trace-remove",
                now_timestamp=lambda: "2026-05-07T16:00:00Z",
            )
        )

        _same_policy, same_record, same_changed, same_diff, same_audit = (
            write_github_actions_authz_policy_removal(
                record_store=store,
                request=request,
                identity=_identity(),
                trace_id="trace-remove-repeat",
                now_timestamp=lambda: "2026-05-07T16:01:00Z",
            )
        )

        self.assertFalse(same_changed)
        self.assertEqual(same_record.record_id, record.record_id)
        self.assertEqual(same_diff["matched_rule_count"], 0)
        self.assertEqual(len(store.written_records), 1)
        self.assertEqual(same_audit["changed"], False)

    def test_removal_requires_exact_rule_match(self) -> None:
        store = _AuthzPolicyStore((_active_record(),))
        request = AuthzPolicyGitHubActionsRemovalEnvelope.model_validate(
            {
                "product": "launchplane",
                "mode": "dry_run",
                "removal": {
                    "repository": "cbusillo/launchplane",
                    "workflow_refs": [
                        "cbusillo/launchplane/.github/workflows/deploy-launchplane.yml@refs/heads/main"
                    ],
                    "products": ["launchplane"],
                    "contexts": ["launchplane"],
                    "actions": ["launchplane_service_deploy.execute"],
                },
            }
        )

        _policy, _record, diff = plan_github_actions_authz_policy_removal(
            record_store=store,
            removal=request.removal,
        )

        self.assertEqual(diff["changed"], False)
        self.assertEqual(diff["matched_rule_count"], 0)
        self.assertEqual(diff["new_github_actions_rule_count"], 1)

    def test_removal_deletes_duplicate_exact_rules(self) -> None:
        record = _active_record()
        duplicate_policy = record.policy.model_copy(
            update={"github_actions": record.policy.github_actions * 2}
        )
        duplicate_record = LaunchplaneAuthzPolicyRecord(
            record_id="launchplane-authz-policy-duplicate-test",
            status="active",
            source="test:duplicates",
            updated_at="2026-05-07T15:30:00Z",
            policy_sha256=authz_policy_sha256(duplicate_policy),
            policy=duplicate_policy,
        )
        store = _AuthzPolicyStore((duplicate_record,))
        request = _removal_request()

        updated_policy, _record, changed, diff, _audit = (
            write_github_actions_authz_policy_removal(
                record_store=store,
                request=request,
                identity=_identity(),
                trace_id="trace-remove-duplicates",
                now_timestamp=lambda: "2026-05-07T16:00:00Z",
            )
        )

        self.assertTrue(changed)
        self.assertEqual(diff["matched_rule_count"], 2)
        self.assertEqual(diff["removed_rule_count"], 2)
        self.assertEqual(updated_policy.github_actions, ())

    def test_response_audit_replaces_requested_grant_with_summary(self) -> None:
        audit: dict[str, object] = {
            "requested_grant": _grant_request().grant.to_policy_rule().model_dump(mode="json"),
            "mode": "dry_run",
        }

        response_audit = authz_policy_grant_response_audit_payload(audit)

        self.assertNotIn("requested_grant", response_audit)
        requested_grant_summary = cast(dict[str, object], response_audit["requested_grant_summary"])
        self.assertEqual(requested_grant_summary["actions"], ["product_profile.read"])

    def test_response_audit_summarizes_human_grant_without_logins(self) -> None:
        audit: dict[str, object] = {
            "requested_grant": _human_grant_request().grant.to_policy_rule().model_dump(
                mode="json"
            ),
            "mode": "dry_run",
        }

        response_audit = authz_policy_grant_response_audit_payload(audit)

        self.assertNotIn("requested_grant", response_audit)
        self.assertNotIn("cbusillo", json.dumps(response_audit, sort_keys=True))
        requested_grant_summary = cast(dict[str, object], response_audit["requested_grant_summary"])
        self.assertEqual(requested_grant_summary["principal_type"], "github_human")
        self.assertEqual(requested_grant_summary["login_count"], 1)


if __name__ == "__main__":
    unittest.main()
