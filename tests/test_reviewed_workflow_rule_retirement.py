from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from control_plane.access_policy_review import build_access_policy_review
from control_plane.authz_grant_service import plan_managed_authz_policy_reconcile
from control_plane.authz_rule_retirement import workflow_rule_retirement_plan
from control_plane.contracts.privileged_operation import ManagedAuthzPolicySetProposalInput
from control_plane.contracts.authz_policy_record import LaunchplaneAuthzPolicyRecord
from control_plane.privileged_operation_registry import plan_managed_authz_policy_set
from control_plane.privileged_operation_worker import execute_approved_privileged_operations_once
from control_plane.service_auth import LaunchplaneAuthzPolicy
from control_plane.storage.postgres import PostgresRecordStore
from tests.support.stores import _sqlite_database_url
from tests.test_privileged_operation_worker import (
    FIXED_NOW,
    _policy_admin_record,
    _prepare_approved_policy_operation,
)
from tests.test_workflow_rule_retirement_plan import mixed_policy, request
from tests.test_authz_administration_read import _record


def retirement_policy() -> LaunchplaneAuthzPolicy:
    policy = mixed_policy()
    rule = policy.github_actions[0].model_copy(
        update={"managed_set_id": None, "managed_rule_id": None}
    )
    return policy.model_copy(update={"github_actions": (rule, *policy.github_actions[1:])})


def retirement_proposal(policy: LaunchplaneAuthzPolicy) -> ManagedAuthzPolicySetProposalInput:
    return ManagedAuthzPolicySetProposalInput.model_validate(
        {
            "managed_set_id": "",
            "desired_policy": {"schema_version": policy.schema_version},
            "workflow_rule_retirement": request(policy).model_dump(mode="json"),
            "reason": "Retire only the reviewed obsolete workflow identity.",
        }
    )


class ReviewedWorkflowRuleRetirementTests(unittest.TestCase):
    def test_stale_selection_and_schema_three_are_checked_without_writes(self) -> None:
        policy = retirement_policy().model_copy(update={"schema_version": 3})
        active = _record(policy=policy)
        store = Mock()
        store.list_authz_policy_records.return_value = [active]
        proposal = retirement_proposal(policy)
        _, _, candidate, _ = plan_managed_authz_policy_reconcile(
            record_store=store, request=proposal.reconcile_request()
        )
        self.assertEqual(candidate.schema_version, policy.schema_version)
        self.assertEqual(
            candidate.model_dump(exclude={"github_actions"}),
            policy.model_dump(exclude={"github_actions"}),
        )
        store.list_authz_policy_records.return_value = [
            active.model_copy(update={"revision": active.revision + 1})
        ]
        with self.assertRaises(ValueError):
            plan_managed_authz_policy_reconcile(
                record_store=store, request=proposal.reconcile_request()
            )
        store.compare_and_write_authz_policy_record.assert_not_called()

    def test_proposal_plans_exact_contraction_and_displays_unmanaged_scope(self) -> None:
        policy = retirement_policy()
        rule = policy.github_actions[0]
        active = _record(policy=policy)
        store = Mock()
        store.list_authz_policy_records.return_value = [active]
        proposal = retirement_proposal(policy)
        evidence = plan_managed_authz_policy_set(store, proposal)
        _, _, candidate, diff = plan_managed_authz_policy_reconcile(
            record_store=store, request=proposal.reconcile_request()
        )
        expected = workflow_rule_retirement_plan(active, request(policy))
        self.assertEqual(diff.desired_policy_sha256, expected["candidate_policy_sha256"])
        self.assertEqual(candidate.github_actions, policy.github_actions[1:])
        self.assertEqual(
            candidate.model_dump(exclude={"github_actions"}),
            policy.model_dump(exclude={"github_actions"}),
        )
        self.assertIsNotNone(proposal.workflow_rule_retirement)
        assert proposal.workflow_rule_retirement is not None
        self.assertEqual(diff.removed_rule_count, len(proposal.workflow_rule_retirement.selection))
        self.assertEqual(
            diff.added_rule_count + diff.adopted_rule_count + diff.updated_rule_count, 0
        )
        review = build_access_policy_review(record_store=store, request=proposal, evidence=evidence)
        self.assertEqual(review.state, "available")
        self.assertEqual(len(review.rules), 1)
        self.assertEqual(review.rules[0].permissions, rule.actions)
        self.assertIn(rule.workflow_refs[0], review.rules[0].scope_summary)
        store.compare_and_write_authz_policy_record.assert_not_called()

    def test_retirement_cannot_mix_with_grants_or_managed_set_changes(self) -> None:
        policy = retirement_policy()
        payload = {
            "managed_set_id": "",
            "desired_policy": {"schema_version": policy.schema_version},
            "workflow_rule_retirement": request(policy).model_dump(mode="json"),
            "reason": "Retire reviewed workflow identity.",
        }
        for change in (
            {"managed_set_id": "operator.mixed"},
            {"desired_policy": policy.model_dump(mode="json")},
            {"administrator_quorum_change": 1},
            {"schema_migration": "migrate_v2_to_v3"},
            {"workflow_rule_retirement": request(mixed_policy()).model_dump(mode="json")},
        ):
            with self.subTest(change=change), self.assertRaises(ValueError):
                ManagedAuthzPolicySetProposalInput.model_validate({**payload, **change})

    def test_approved_worker_removes_only_selection_and_replays_without_more_writes(self) -> None:
        with TemporaryDirectory() as temporary:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(Path(temporary) / "retirement.sqlite3")
            )
            self.addCleanup(store.close)
            store.ensure_schema()
            approval_policy = _policy_admin_record()
            policy = approval_policy.policy.model_copy(
                update={"github_actions": retirement_policy().github_actions}
            )
            active = approval_policy.model_copy(update={"policy": policy, "policy_sha256": ""})
            active = LaunchplaneAuthzPolicyRecord.model_validate(active.model_dump())
            active = store.seed_authz_policy_if_absent(active)
            selection = request(policy).model_copy(
                update={
                    "policy_record_id": active.record_id,
                    "policy_revision": active.revision,
                    "policy_sha256": active.policy_sha256,
                }
            )
            proposal = retirement_proposal(policy).model_copy(
                update={"workflow_rule_retirement": selection}
            )
            operation_id = _prepare_approved_policy_operation(
                store, approval_policy=active, request=proposal
            )
            with patch(
                "control_plane.privileged_operation_worker.privileged_operation_execution_token",
                return_value="execution-fixture",
            ):
                completed = execute_approved_privileged_operations_once(
                    record_store=store, now=lambda: FIXED_NOW
                )
                replay = execute_approved_privileged_operations_once(
                    record_store=store, now=lambda: FIXED_NOW + timedelta(seconds=1)
                )
            self.assertEqual(completed[0].operation_id, operation_id)
            self.assertEqual(completed[0].status, "executed")
            self.assertFalse(replay)
            resulting = store.list_authz_policy_records(status="active", limit=2)
            self.assertEqual(len(resulting), 1)
            self.assertEqual(resulting[0].policy.github_actions, policy.github_actions[1:])
            self.assertEqual(
                resulting[0].policy.model_dump(exclude={"github_actions"}),
                policy.model_dump(exclude={"github_actions"}),
            )
