from __future__ import annotations

from datetime import UTC, datetime, timedelta
import unittest

from pydantic import ValidationError

from control_plane.contracts.every_code_feedback_resume import (
    EveryCodeFeedbackAcceptanceRecord,
    EveryCodeFeedbackHandoffReceiptRecord,
    EveryCodeFeedbackLaunchBinding,
    EveryCodeFeedbackPolicyDecisionProvenance,
    EveryCodeFeedbackProcessBindingRecord,
    EveryCodeFeedbackLaunchObservationRecord,
    EveryCodeFeedbackRecoveryDispositionRecord,
    EveryCodeFeedbackResumeIntentRecord,
    EveryCodeFeedbackStartupReceiptRecord,
    EveryCodeVerifiedFeedbackRevision,
    every_code_feedback_eligible_until,
    validate_every_code_feedback_revision_time,
)


T0 = "2026-09-07T12:00:00.000000Z"
SHA = "a" * 64


def revision(**updates: object) -> EveryCodeVerifiedFeedbackRevision:
    values: dict[str, object] = {
        "repository_owner_id": 12,
        "repository_id": 34,
        "repository": "cbusillo/launchplane",
        "pull_request_number": 56,
        "pull_request_node_id": "PR_node",
        "feedback_id": "feedback-1",
        "feedback_kind": "issue_comment",
        "object_node_id": "IC_node",
        "object_id": 78,
        "actor_github_id": 90,
        "actor_login": "display-name",
        "provider_updated_at": T0,
        "body_sha256": SHA,
    }
    values.update(updates)
    return EveryCodeVerifiedFeedbackRevision.model_validate(values)


def policy(
    action: str = "every_code_feedback_resume.request",
) -> EveryCodeFeedbackPolicyDecisionProvenance:
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


def binding() -> EveryCodeFeedbackLaunchBinding:
    return EveryCodeFeedbackLaunchBinding(
        request_id="request-1",
        lifecycle_id="5f901440-1c5b-4fde-83db-58b3085eb465",
        fencing_token=3,
        host="worker-1",
        launch_nonce="nonce-1",
        launch_attempt=1,
    )


class EveryCodeFeedbackResumeContractTests(unittest.TestCase):
    def test_revision_digest_excludes_display_login(self) -> None:
        first = revision(actor_login="old-name")
        renamed = revision(actor_login="new-name")
        changed_actor = revision(actor_github_id=91)
        self.assertEqual(first.revision_digest, renamed.revision_digest)
        self.assertNotEqual(first.revision_digest, changed_actor.revision_digest)

    def test_numeric_id_rejects_boolean_and_timestamp_requires_canonical_aware_value(self) -> None:
        with self.assertRaises(ValidationError):
            revision(actor_github_id=True)
        with self.assertRaises(ValidationError):
            revision(provider_updated_at="2026-09-07T12:00:00")
        with self.assertRaises(ValidationError):
            revision(provider_updated_at="2026-09-07T08:00:00.000000-04:00")

    def test_revision_digest_mismatch_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValidationError, "revision_digest"):
            revision(revision_digest="b" * 64)

    def test_opaque_node_ids_allow_encoding_but_reject_whitespace_and_controls(self) -> None:
        self.assertEqual(revision(object_node_id="Q29tbWVudA==").object_node_id, "Q29tbWVudA==")
        for node_id in ("node\nother", "node\x00other", "node other"):
            with self.subTest(node_id=node_id), self.assertRaises(ValidationError):
                revision(object_node_id=node_id)

    def test_revision_age_and_future_skew_fail_closed(self) -> None:
        observed = datetime(2026, 9, 7, 12, tzinfo=UTC)
        for provider in (
            observed - timedelta(hours=24, microseconds=1),
            observed + timedelta(minutes=5, microseconds=1),
        ):
            with self.subTest(provider=provider), self.assertRaises(ValueError):
                validate_every_code_feedback_revision_time(
                    provider_updated_at=provider.isoformat(timespec="microseconds").replace(
                        "+00:00", "Z"
                    ),
                    observed_at=T0,
                )

    def test_expiry_is_earlier_receipt_or_revision_bound(self) -> None:
        self.assertEqual(
            every_code_feedback_eligible_until(
                first_received_at="2026-09-07T14:00:00.000000Z", provider_updated_at=T0
            ),
            "2026-09-08T12:00:00.000000Z",
        )

    def test_acceptance_binds_exact_policy_and_fixed_expiry(self) -> None:
        accepted = EveryCodeFeedbackAcceptanceRecord(
            acceptance_id="acceptance-1",
            request_id="request-1",
            issue_number=2328,
            issue_url="https://github.com/cbusillo/launchplane/issues/2328",
            retained_pull_request_url="https://github.com/cbusillo/launchplane/pull/1",
            revision=revision(),
            policy=policy(),
            github_delivery_id="delivery-1",
            received_at=T0,
            created_at=T0,
            eligible_until="2026-09-08T12:00:00.000000Z",
            status="accepted",
            reason_code="exact-policy-match",
        )
        renamed = accepted.model_copy(
            update={"revision": revision(actor_login="renamed"), "acceptance_digest": ""}
        )
        renamed = EveryCodeFeedbackAcceptanceRecord.model_validate(renamed.model_dump())
        self.assertEqual(accepted.acceptance_digest, renamed.acceptance_digest)
        with self.assertRaisesRegex(ValidationError, "fixed earliest"):
            EveryCodeFeedbackAcceptanceRecord.model_validate(
                {
                    **accepted.model_dump(),
                    "eligible_until": "2026-09-08T12:00:00.000001Z",
                    "acceptance_digest": "",
                }
            )

    def test_intent_is_terminal_and_expiry_is_bounded(self) -> None:
        values = {
            "intent_id": "intent-1",
            "request_id": "request-1",
            "acceptance_id": "acceptance-1",
            "acceptance_digest": SHA,
            "expected_lifecycle_id": "lifecycle-1",
            "expected_terminal_state": "done",
            "expected_fencing_token": 2,
            "retained_host": "worker-1",
            "retained_pull_request_url": "https://example.test/pr/1",
            "issued_at": T0,
            "eligible_until": "2026-09-08T12:00:00.000000Z",
            "worker_idempotency_key": "worker-key-1",
        }
        intent = EveryCodeFeedbackResumeIntentRecord.model_validate(values)
        self.assertTrue(intent.intent_digest)
        with self.assertRaises(ValidationError):
            EveryCodeFeedbackResumeIntentRecord.model_validate(
                {**values, "expected_fencing_token": True}
            )
        with self.assertRaisesRegex(ValidationError, "within 24 hours"):
            EveryCodeFeedbackResumeIntentRecord.model_validate(
                {**values, "eligible_until": "2026-09-08T12:00:00.000001Z"}
            )

    def test_startup_receipt_cannot_stand_in_for_handoff(self) -> None:
        startup = EveryCodeFeedbackStartupReceiptRecord(
            receipt_id="receipt-1",
            operation_id="operation-1",
            binding=binding(),
            recorded_at=T0,
            evidence_sha256=SHA,
            process_binding_sha256=SHA,
        )
        self.assertEqual(startup.receipt_kind, "startup")
        with self.assertRaises(ValidationError):
            EveryCodeFeedbackHandoffReceiptRecord.model_validate(startup.model_dump())

    def test_recovery_is_observation_only_and_requires_exact_evidence(self) -> None:
        common = {
            "recovery_id": "recovery-1",
            "operation_id": "operation-1",
            "binding": binding(),
            "recovery_attempt": 1,
            "evidence_sha256": SHA,
            "recorded_at": T0,
            "reason_code": "worker-crash",
        }
        with self.assertRaisesRegex(ValidationError, "exact matching"):
            EveryCodeFeedbackRecoveryDispositionRecord.model_validate(
                {**common, "disposition": "adopted", "evidence_status": "unavailable"}
            )
        with self.assertRaises(ValidationError):
            EveryCodeFeedbackRecoveryDispositionRecord.model_validate(
                {
                    **common,
                    "disposition": "observed_absent",
                    "evidence_status": "unavailable",
                }
            )
        with self.assertRaises(ValidationError):
            EveryCodeFeedbackRecoveryDispositionRecord.model_validate(
                {
                    **common,
                    "disposition": "reconcile_required",
                    "evidence_status": "unavailable",
                    "operator_subject": "operator-1",
                }
            )

    def test_process_identity_needs_start_marker_and_observation_is_exact(self) -> None:
        process = EveryCodeFeedbackProcessBindingRecord(
            binding=binding(),
            session_name="session-1",
            process_id=123,
            process_group_id=122,
            process_start_marker="start-456",
            registered_at=T0,
        )
        self.assertTrue(process.process_binding_sha256)
        with self.assertRaises(ValidationError):
            EveryCodeFeedbackProcessBindingRecord.model_validate(
                {**process.model_dump(), "process_id": True, "process_binding_sha256": ""}
            )
        with self.assertRaisesRegex(ValidationError, "requires a process binding"):
            EveryCodeFeedbackLaunchObservationRecord(
                binding=binding(),
                evidence_status="exact_match",
                inspected_at=T0,
                evidence_sha256=SHA,
            )

    def test_new_launch_lifecycle_requires_canonical_uuid4(self) -> None:
        for lifecycle_id in (
            "lifecycle-2",
            "5F901440-1C5B-4FDE-83DB-58B3085EB465",
            "5f901440-1c5b-1fde-83db-58b3085eb465",
        ):
            with self.subTest(lifecycle_id=lifecycle_id), self.assertRaises(ValidationError):
                EveryCodeFeedbackLaunchBinding.model_validate(
                    {**binding().model_dump(), "lifecycle_id": lifecycle_id}
                )


if __name__ == "__main__":
    unittest.main()
