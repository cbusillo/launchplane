from __future__ import annotations

from tempfile import TemporaryDirectory
import unittest

from control_plane.contracts.every_code_feedback_resume import (
    EveryCodeFeedbackAcceptanceRecord,
    EveryCodeFeedbackHandoffReceiptRecord,
    EveryCodeFeedbackLaunchBinding,
    EveryCodeFeedbackPolicyDecisionProvenance,
    EveryCodeFeedbackRecoveryDispositionRecord,
    EveryCodeFeedbackResumeIntentRecord,
    EveryCodeFeedbackResumeOperationRecord,
    EveryCodeFeedbackStartupReceiptRecord,
    EveryCodeLinkedPullRequestClosureRecord,
    EveryCodeVerifiedFeedbackRevision,
)
from control_plane.contracts.every_code_work_request import EveryCodeWorkRequestRecord
from control_plane.storage.postgres import (
    EveryCodeFeedbackResumeStorageConflictError,
    PostgresRecordStore,
)


T0 = "2026-09-07T12:00:00.000000Z"
T1 = "2026-09-07T12:01:00.000000Z"
EXPIRY = "2026-09-08T12:00:00.000000Z"
SHA = "a" * 64


def _policy(action: str) -> EveryCodeFeedbackPolicyDecisionProvenance:
    return EveryCodeFeedbackPolicyDecisionProvenance.model_validate(
        {
            "action": action,
            "instance": "github-repository:34",
            "managed_set_id": "set-1",
            "managed_rule_id": "rule-1",
            "policy_record_id": "policy-1",
            "policy_revision": 1,
            "policy_sha256": SHA,
        }
    )


def _acceptance(**updates: object) -> EveryCodeFeedbackAcceptanceRecord:
    values: dict[str, object] = {
        "acceptance_id": "acceptance-1",
        "request_id": "request-1",
        "issue_number": 2328,
        "issue_url": "https://github.com/cbusillo/launchplane/issues/2328",
        "retained_pull_request_url": "https://github.com/cbusillo/launchplane/pull/1",
        "revision": EveryCodeVerifiedFeedbackRevision(
            repository_owner_id=12,
            repository_id=34,
            repository="cbusillo/launchplane",
            pull_request_number=1,
            pull_request_node_id="PR_node",
            feedback_id="feedback-1",
            feedback_kind="issue_comment",
            object_node_id="IC_node",
            object_id=78,
            actor_github_id=90,
            actor_login="display-name",
            provider_updated_at=T0,
            body_sha256=SHA,
        ),
        "policy": _policy("every_code_feedback_resume.request"),
        "github_delivery_id": "delivery-1",
        "received_at": T0,
        "created_at": T0,
        "eligible_until": EXPIRY,
        "status": "accepted",
        "reason_code": "exact-policy-match",
    }
    values.update(updates)
    return EveryCodeFeedbackAcceptanceRecord.model_validate(values)


def _intent(acceptance: EveryCodeFeedbackAcceptanceRecord) -> EveryCodeFeedbackResumeIntentRecord:
    return EveryCodeFeedbackResumeIntentRecord(
        intent_id="intent-1",
        request_id=acceptance.request_id,
        acceptance_id=acceptance.acceptance_id,
        acceptance_digest=acceptance.acceptance_digest,
        expected_lifecycle_id="lifecycle-1",
        expected_terminal_state="done",
        expected_fencing_token=2,
        retained_host="worker-1",
        retained_pull_request_url=acceptance.retained_pull_request_url,
        issued_at=T0,
        eligible_until=acceptance.eligible_until,
        worker_idempotency_key="worker-key-1",
    )


def _binding() -> EveryCodeFeedbackLaunchBinding:
    return EveryCodeFeedbackLaunchBinding(
        request_id="request-1",
        lifecycle_id="5f901440-1c5b-4fde-83db-58b3085eb465",
        fencing_token=3,
        host="worker-1",
        launch_nonce="nonce-1",
        launch_attempt=1,
    )


def _operation(
    acceptance: EveryCodeFeedbackAcceptanceRecord,
    intent: EveryCodeFeedbackResumeIntentRecord,
) -> EveryCodeFeedbackResumeOperationRecord:
    return EveryCodeFeedbackResumeOperationRecord(
        operation_id="operation-1",
        intent_id=intent.intent_id,
        acceptance_id=acceptance.acceptance_id,
        binding=_binding(),
        execution_policy=_policy("every_code_feedback_resume.execute"),
        state="launch_pending",
        committed_at=T0,
        updated_at=T0,
    )


class EveryCodeFeedbackResumeStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.store = PostgresRecordStore(
            database_url=f"sqlite+pysqlite:///{self.temporary_directory.name}/records.db"
        )
        self.store.ensure_schema()
        self.addCleanup(self.store.close)
        self.store.write_every_code_work_request_record(
            EveryCodeWorkRequestRecord(
                request_id="request-1",
                lifecycle_id="lifecycle-1",
                source="github_issue_label",
                state="done",
                repository="cbusillo/launchplane",
                issue_number=2328,
                issue_url="https://github.com/cbusillo/launchplane/issues/2328",
                trigger_label="every-code",
                queued_at=T0,
                updated_at=T0,
                claimed_at=T0,
                claimed_by_host="worker-1",
                fencing_token=2,
                attempt=2,
                started_at=T0,
                finished_at=T0,
                result_pr_url="https://github.com/cbusillo/launchplane/pull/1",
            )
        )

    def test_immutable_acceptance_replays_and_conflicts(self) -> None:
        accepted = _acceptance()
        self.assertEqual(self.store.write_every_code_feedback_acceptance_record(accepted), accepted)
        self.assertEqual(self.store.write_every_code_feedback_acceptance_record(accepted), accepted)
        conflicting = _acceptance(
            acceptance_id=accepted.acceptance_id,
            github_delivery_id="delivery-2",
        )
        with self.assertRaises(EveryCodeFeedbackResumeStorageConflictError):
            self.store.write_every_code_feedback_acceptance_record(conflicting)

    def test_same_verified_revision_replays_first_receipt(self) -> None:
        first = _acceptance()
        redelivery = _acceptance(
            acceptance_id="acceptance-2",
            github_delivery_id="delivery-2",
            received_at=T1,
            created_at=T1,
            eligible_until=EXPIRY,
        )
        self.store.write_every_code_feedback_acceptance_record(first)
        self.assertEqual(self.store.write_every_code_feedback_acceptance_record(redelivery), first)

    def test_intent_and_operation_reject_incoherent_references(self) -> None:
        accepted = _acceptance()
        intent = _intent(accepted)
        with self.assertRaisesRegex(ValueError, "acceptance does not exist"):
            self.store._write_every_code_feedback_resume_intent_fixture_record(intent)
        self.store.write_every_code_feedback_acceptance_record(accepted)
        self.store._write_every_code_feedback_resume_intent_fixture_record(intent)
        operation = _operation(accepted, intent)
        mismatched = operation.model_copy(
            update={"binding": operation.binding.model_copy(update={"request_id": "request-2"})}
        )
        with self.assertRaisesRegex(ValueError, "does not match"):
            self.store.write_every_code_feedback_resume_operation_record(mismatched)

    def test_operation_requires_advanced_fence_and_unexpired_commit(self) -> None:
        accepted = _acceptance()
        intent = _intent(accepted)
        operation = _operation(accepted, intent)
        self.store.write_every_code_feedback_acceptance_record(accepted)
        self.store._write_every_code_feedback_resume_intent_fixture_record(intent)
        stale_fence = operation.model_copy(
            update={
                "binding": operation.binding.model_copy(
                    update={"fencing_token": intent.expected_fencing_token}
                )
            }
        )
        with self.assertRaisesRegex(ValueError, "must advance"):
            self.store.write_every_code_feedback_resume_operation_record(stale_fence)
        expired = operation.model_copy(
            update={"committed_at": intent.eligible_until, "updated_at": intent.eligible_until}
        )
        with self.assertRaisesRegex(ValueError, "before intent eligibility expires"):
            self.store.write_every_code_feedback_resume_operation_record(expired)

    def test_receipts_and_unknown_recovery_are_separate_queryable_evidence(self) -> None:
        accepted = _acceptance()
        intent = _intent(accepted)
        operation = _operation(accepted, intent)
        self.store.write_every_code_feedback_acceptance_record(accepted)
        self.store._write_every_code_feedback_resume_intent_fixture_record(intent)
        self.store.write_every_code_feedback_resume_operation_record(operation)
        startup = EveryCodeFeedbackStartupReceiptRecord(
            receipt_id="receipt-1",
            operation_id=operation.operation_id,
            binding=operation.binding,
            recorded_at=T0,
            evidence_sha256=SHA,
            process_binding_sha256="b" * 64,
        )
        handoff = EveryCodeFeedbackHandoffReceiptRecord(
            receipt_id="receipt-2",
            operation_id=operation.operation_id,
            binding=operation.binding,
            recorded_at=T1,
            evidence_sha256="c" * 64,
            handoff_sha256="d" * 64,
        )
        recovery = EveryCodeFeedbackRecoveryDispositionRecord(
            recovery_id="recovery-1",
            operation_id=operation.operation_id,
            binding=operation.binding,
            recovery_attempt=1,
            disposition="reconcile_required",
            evidence_status="unavailable",
            evidence_sha256="e" * 64,
            recorded_at=T1,
            reason_code="inspection-unavailable",
        )
        self.store.write_every_code_feedback_resume_receipt_record(startup)
        self.store.write_every_code_feedback_resume_receipt_record(handoff)
        self.store.write_every_code_feedback_recovery_disposition_record(recovery)
        self.assertEqual(
            self.store.list_every_code_feedback_resume_receipt_records(
                operation_id=operation.operation_id
            ),
            (startup, handoff),
        )
        self.assertEqual(
            self.store.list_every_code_feedback_recovery_disposition_records(
                operation_id=operation.operation_id
            ),
            (recovery,),
        )
        self.assertEqual(self.store.list_every_code_pr_feedback_records(status="pending"), ())

    def test_closure_is_append_only_and_checks_known_feedback_identity(self) -> None:
        accepted = _acceptance()
        self.store.write_every_code_feedback_acceptance_record(accepted)
        closure = EveryCodeLinkedPullRequestClosureRecord(
            closure_id="closure-1",
            request_id=accepted.request_id,
            repository_id=accepted.revision.repository_id,
            pull_request_number=accepted.revision.pull_request_number,
            pull_request_node_id=accepted.revision.pull_request_node_id,
            merged=False,
            closed_at=T1,
            github_delivery_id="delivery-close-1",
        )
        self.store.write_every_code_linked_pull_request_closure_record(closure)
        self.assertEqual(
            self.store.list_every_code_linked_pull_request_closure_records(
                request_id=accepted.request_id
            ),
            (closure,),
        )
        changed_closure = EveryCodeLinkedPullRequestClosureRecord.model_validate(
            {**closure.model_dump(), "merged": True, "closure_digest": ""}
        )
        with self.assertRaises(EveryCodeFeedbackResumeStorageConflictError):
            self.store.write_every_code_linked_pull_request_closure_record(changed_closure)


if __name__ == "__main__":
    unittest.main()
