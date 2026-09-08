from __future__ import annotations

import unittest

from control_plane.contracts.every_code_feedback_resume import (
    EveryCodeFeedbackHandoffReceiptRecord,
    EveryCodeFeedbackLaunchBinding,
    EveryCodeFeedbackLaunchObservationRecord,
    EveryCodeFeedbackProcessBindingRecord,
    EveryCodeFeedbackStartupReceiptRecord,
)
from control_plane.every_code_feedback_launch import (
    UnavailableEveryCodeFeedbackLaunchAdapter,
    cancellation_matches_process,
    decide_binding_registration,
    decide_closure,
    decide_gate_release,
    decide_handoff_receipt,
    decide_reconciliation,
    decide_startup_receipt,
    deterministic_every_code_feedback_session_name,
)

_TIME = "2026-09-08T01:00:00.000000Z"
_DIGEST = "a" * 64


class EveryCodeFeedbackLaunchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.binding = EveryCodeFeedbackLaunchBinding(
            request_id="request-1",
            lifecycle_id="5f901440-1c5b-4fde-83db-58b3085eb465",
            fencing_token=4,
            host="worker.example.test",
            launch_nonce="nonce-1",
            launch_attempt=1,
        )
        self.process = self._process(self.binding)

    def _process(
        self, binding: EveryCodeFeedbackLaunchBinding, *, process_id: int = 101
    ) -> EveryCodeFeedbackProcessBindingRecord:
        return EveryCodeFeedbackProcessBindingRecord(
            binding=binding,
            session_name=deterministic_every_code_feedback_session_name(binding),
            process_id=process_id,
            process_group_id=process_id,
            process_start_marker=f"darwin-proc-start:{process_id}:1000",
            registered_at=_TIME,
        )

    def test_session_name_binds_all_launch_fields(self) -> None:
        original = deterministic_every_code_feedback_session_name(self.binding)
        for field, value in (
            ("request_id", "request-2"),
            ("lifecycle_id", "325a5210-f018-4f01-b28d-175757f1674a"),
            ("fencing_token", 5),
            ("host", "other.example.test"),
            ("launch_nonce", "nonce-2"),
            ("launch_attempt", 2),
        ):
            changed = self.binding.model_copy(update={field: value})
            self.assertNotEqual(original, deterministic_every_code_feedback_session_name(changed))

    def test_session_name_removes_tmux_delimiters_and_other_punctuation(self) -> None:
        binding = self.binding.model_copy(update={"request_id": "repo.name:pr/one two"})
        session_name = deterministic_every_code_feedback_session_name(binding)
        self.assertRegex(session_name, r"^[A-Za-z0-9_-]+$")
        self.assertNotIn(".", session_name)
        self.assertNotIn(":", session_name)

    def test_registration_replays_identical_and_conflicts_on_process_creation(self) -> None:
        registered = decide_binding_registration(
            expected=self.binding, existing=None, proposed=self.process
        )
        replay = decide_binding_registration(
            expected=self.binding, existing=self.process, proposed=self.process
        )
        conflict = decide_binding_registration(
            expected=self.binding,
            existing=self.process,
            proposed=self._process(self.binding, process_id=102),
        )
        self.assertEqual(registered.action, "register")
        self.assertEqual(replay.reason, "binding_registration_replayed")
        self.assertEqual(conflict.phase, "reconcile_required")

    def test_registration_rejects_launch_and_session_mismatch(self) -> None:
        other_binding = self.binding.model_copy(update={"launch_nonce": "nonce-2"})
        launch_mismatch = decide_binding_registration(
            expected=self.binding, existing=None, proposed=self._process(other_binding)
        )
        wrong_session = self.process.model_copy(update={"session_name": "wrong-session"})
        session_mismatch = decide_binding_registration(
            expected=self.binding, existing=None, proposed=wrong_session
        )
        self.assertEqual(launch_mismatch.reason, "launch_binding_mismatch")
        self.assertEqual(session_mismatch.reason, "session_name_mismatch")

    def test_release_requires_open_request_exact_binding_lease_and_authority(self) -> None:
        released = decide_gate_release(
            expected=self.binding,
            process_binding=self.process,
            pull_request_open=True,
            lease_current=True,
            authorized=True,
        )
        closed = decide_gate_release(
            expected=self.binding,
            process_binding=self.process,
            pull_request_open=False,
            lease_current=True,
            authorized=True,
        )
        denied = decide_gate_release(
            expected=self.binding,
            process_binding=self.process,
            pull_request_open=True,
            lease_current=True,
            authorized=False,
        )
        self.assertEqual((released.action, released.phase), ("release", "released"))
        self.assertEqual((closed.action, closed.phase), ("cancel_gate", "cancelled"))
        self.assertEqual(denied.phase, "reconcile_required")

    def test_release_rejects_stale_lease_missing_registration_and_wrong_session(self) -> None:
        stale = decide_gate_release(
            expected=self.binding,
            process_binding=self.process,
            pull_request_open=True,
            lease_current=False,
            authorized=True,
        )
        missing = decide_gate_release(
            expected=self.binding,
            process_binding=None,
            pull_request_open=True,
            lease_current=True,
            authorized=True,
        )
        wrong_session = decide_gate_release(
            expected=self.binding,
            process_binding=self.process.model_copy(update={"session_name": "wrong-session"}),
            pull_request_open=True,
            lease_current=True,
            authorized=True,
        )
        self.assertEqual(stale.reason, "lease_not_current")
        self.assertEqual(missing.reason, "exact_binding_not_registered")
        self.assertEqual(wrong_session.reason, "registered_session_name_mismatch")

    def test_startup_receipt_never_means_handoff_accepted(self) -> None:
        receipt = EveryCodeFeedbackStartupReceiptRecord(
            receipt_id="startup-1",
            operation_id="operation-1",
            binding=self.binding,
            recorded_at=_TIME,
            evidence_sha256=_DIGEST,
            process_binding_sha256=self.process.process_binding_sha256,
        )
        decision = decide_startup_receipt(
            expected_operation_id="operation-1", expected_process=self.process, receipt=receipt
        )
        self.assertEqual((decision.action, decision.phase), ("record_started", "started"))

    def test_startup_receipt_rejects_operation_binding_process_and_time_mismatch(self) -> None:
        receipt = EveryCodeFeedbackStartupReceiptRecord(
            receipt_id="startup-1",
            operation_id="operation-1",
            binding=self.binding,
            recorded_at=_TIME,
            evidence_sha256=_DIGEST,
            process_binding_sha256=self.process.process_binding_sha256,
        )
        other_binding = self.binding.model_copy(update={"launch_nonce": "nonce-2"})
        variants = (
            receipt.model_copy(update={"operation_id": "operation-2"}),
            receipt.model_copy(update={"binding": other_binding}),
            receipt.model_copy(update={"process_binding_sha256": "b" * 64}),
            receipt.model_copy(update={"recorded_at": "2026-09-08T00:59:59.000000Z"}),
        )
        for variant in variants:
            with self.subTest(variant=variant):
                decision = decide_startup_receipt(
                    expected_operation_id="operation-1",
                    expected_process=self.process,
                    receipt=variant,
                )
                self.assertEqual(
                    (decision.action, decision.phase), ("no_action", "delivery_unknown")
                )

    def test_only_exact_session_receipt_accepts_handoff(self) -> None:
        receipt = EveryCodeFeedbackHandoffReceiptRecord(
            receipt_id="handoff-1",
            operation_id="operation-1",
            binding=self.binding,
            recorded_at=_TIME,
            evidence_sha256=_DIGEST,
            handoff_sha256="b" * 64,
        )
        accepted = decide_handoff_receipt(
            expected_operation_id="operation-1",
            expected_process=self.process,
            expected_handoff_sha256="b" * 64,
            receipt=receipt,
        )
        unknown = decide_handoff_receipt(
            expected_operation_id="operation-1",
            expected_process=self.process,
            expected_handoff_sha256="c" * 64,
            receipt=receipt,
        )
        self.assertEqual(accepted.phase, "handoff_accepted")
        self.assertEqual(unknown.phase, "delivery_unknown")

        wrong_operation = receipt.model_copy(update={"operation_id": "operation-2"})
        rejected = decide_handoff_receipt(
            expected_operation_id="operation-1",
            expected_process=self.process,
            expected_handoff_sha256="b" * 64,
            receipt=wrong_operation,
        )
        self.assertEqual(rejected.phase, "delivery_unknown")

    def test_reconciliation_does_not_adopt_reused_process_identifiers(self) -> None:
        different_process = self._process(self.binding, process_id=102)
        observation = EveryCodeFeedbackLaunchObservationRecord(
            binding=self.binding,
            evidence_status="exact_match",
            observed_process_binding=different_process,
            inspected_at=_TIME,
            evidence_sha256=_DIGEST,
        )
        decision = decide_reconciliation(expected_process=self.process, observation=observation)
        self.assertEqual((decision.action, decision.phase), ("no_action", "reconcile_required"))

    def test_reconciliation_records_absence_without_authorizing_launch(self) -> None:
        observation = EveryCodeFeedbackLaunchObservationRecord(
            binding=self.binding,
            evidence_status="proven_absent",
            inspected_at=_TIME,
            evidence_sha256=_DIGEST,
        )
        decision = decide_reconciliation(expected_process=self.process, observation=observation)
        self.assertEqual(
            (decision.action, decision.phase), ("record_proven_absence", "reconcile_required")
        )

    def test_mismatch_and_unavailable_never_retry(self) -> None:
        for status in ("mismatch", "unavailable"):
            observation = EveryCodeFeedbackLaunchObservationRecord(
                binding=self.binding,
                evidence_status=status,
                inspected_at=_TIME,
                evidence_sha256=_DIGEST,
            )
            self.assertEqual(
                decide_reconciliation(expected_process=self.process, observation=observation).phase,
                "reconcile_required",
            )
        adapter = UnavailableEveryCodeFeedbackLaunchAdapter()
        self.assertIsNone(adapter.inspect(self.binding))
        self.assertEqual(
            decide_reconciliation(expected_process=self.process, observation=None).phase,
            "reconcile_required",
        )

    def test_closure_after_release_targets_only_exact_process_creation(self) -> None:
        decision = decide_closure(
            operation_id="operation-1",
            process_binding=self.process,
            gate_released=True,
            requested_at=_TIME,
        )
        assert decision.cancellation is not None
        self.assertTrue(cancellation_matches_process(decision.cancellation, self.process))
        self.assertFalse(
            cancellation_matches_process(
                decision.cancellation, self._process(self.binding, process_id=102)
            )
        )

    def test_closure_without_binding_is_unknown_after_release_and_safe_before_release(self) -> None:
        before = decide_closure(
            operation_id="operation-1",
            process_binding=None,
            gate_released=False,
            requested_at=_TIME,
        )
        after = decide_closure(
            operation_id="operation-1",
            process_binding=None,
            gate_released=True,
            requested_at=_TIME,
        )
        self.assertEqual((before.action, before.phase), ("cancel_gate", "cancelled"))
        self.assertEqual((after.action, after.phase), ("no_action", "cancellation_unknown"))


if __name__ == "__main__":
    unittest.main()
