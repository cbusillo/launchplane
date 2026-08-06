"""Tests for engineering review run contract, domain logic, and storage."""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from control_plane.contracts.engineering_review_run import (
    EngineeringReviewFinding,
    EngineeringReviewRunRecord,
    EngineeringReviewRunSubmission,
    ENGINEERING_REVIEW_RUN_MAX_FINDINGS,
    ENGINEERING_REVIEW_RUN_MAX_SUMMARY_LENGTH,
    build_engineering_review_evidence_digest,
    build_engineering_review_run_id,
    cancel_engineering_review_run,
    dispatch_engineering_review_run,
    expire_engineering_review_run,
    generate_engineering_review_run_credential,
    submit_engineering_review_run,
    verify_engineering_review_run_credential,
)
from control_plane.storage.postgres import PostgresRecordStore


def _sqlite_url(path: Path) -> str:
    return f"sqlite+pysqlite:///{path}"


def _pending_record(
    *,
    run_id: str = "eng-review-run-cbusillo-code-42-s1-abc123",
    review_slot: int = 1,
    head_sha: str = "abc123def456",
    policy_revision: str = "rev-001",
) -> EngineeringReviewRunRecord:
    _, credential_hash = generate_engineering_review_run_credential()
    return EngineeringReviewRunRecord(
        run_id=run_id,
        review_slot=review_slot,
        state="pending",
        repository="cbusillo/code",
        pr_number=42,
        head_sha=head_sha,
        tree_sha="tree456abc123",
        policy_revision=policy_revision,
        work_request_id="every-code-cbusillo-code-42-abc",
        model_id="claude-sonnet-4-6",
        model_family="claude",
        binary_digest="sha256:deadbeef",
        run_credential_hash=credential_hash,
        created_at="2026-08-05T10:00:00Z",
        updated_at="2026-08-05T10:00:00Z",
        lease_expires_at="2026-08-05T11:00:00Z",
    )


class BuildEngineeringReviewRunIdTests(unittest.TestCase):
    def test_id_is_deterministic(self) -> None:
        first = build_engineering_review_run_id(
            repository="cbusillo/code",
            pr_number=42,
            head_sha="abc123",
            review_slot=1,
            policy_revision="rev-001",
        )
        second = build_engineering_review_run_id(
            repository="cbusillo/code",
            pr_number=42,
            head_sha="abc123",
            review_slot=1,
            policy_revision="rev-001",
        )
        self.assertEqual(first, second)
        self.assertTrue(first.startswith("eng-review-run-cbusillo-code-42-s1-"))

    def test_different_slots_produce_different_ids(self) -> None:
        slot1 = build_engineering_review_run_id(
            repository="cbusillo/code",
            pr_number=42,
            head_sha="abc123",
            review_slot=1,
            policy_revision="rev-001",
        )
        slot2 = build_engineering_review_run_id(
            repository="cbusillo/code",
            pr_number=42,
            head_sha="abc123",
            review_slot=2,
            policy_revision="rev-001",
        )
        self.assertNotEqual(slot1, slot2)

    def test_different_head_shas_produce_different_ids(self) -> None:
        id_a = build_engineering_review_run_id(
            repository="cbusillo/code",
            pr_number=42,
            head_sha="abc",
            review_slot=1,
            policy_revision="rev-001",
        )
        id_b = build_engineering_review_run_id(
            repository="cbusillo/code",
            pr_number=42,
            head_sha="def",
            review_slot=1,
            policy_revision="rev-001",
        )
        self.assertNotEqual(id_a, id_b)

    def test_requires_all_fields(self) -> None:
        with self.assertRaises(ValueError):
            build_engineering_review_run_id(
                repository="",
                pr_number=42,
                head_sha="abc",
                review_slot=1,
                policy_revision="rev-001",
            )


class CredentialTests(unittest.TestCase):
    def test_generate_returns_plaintext_and_hash(self) -> None:
        plaintext, credential_hash = generate_engineering_review_run_credential()
        self.assertTrue(len(plaintext) > 0)
        self.assertTrue(len(credential_hash) > 0)
        self.assertNotEqual(plaintext, credential_hash)

    def test_verify_correct_credential(self) -> None:
        plaintext, credential_hash = generate_engineering_review_run_credential()
        self.assertTrue(verify_engineering_review_run_credential(plaintext, credential_hash))

    def test_reject_wrong_credential(self) -> None:
        _, credential_hash = generate_engineering_review_run_credential()
        self.assertFalse(verify_engineering_review_run_credential("wrong", credential_hash))

    def test_each_generation_is_unique(self) -> None:
        first, _ = generate_engineering_review_run_credential()
        second, _ = generate_engineering_review_run_credential()
        self.assertNotEqual(first, second)


class EngineeringReviewRunRecordValidationTests(unittest.TestCase):
    def test_pending_record_valid(self) -> None:
        record = _pending_record()
        self.assertEqual(record.state, "pending")
        self.assertIsNone(record.decision)

    def test_requires_run_id(self) -> None:
        with self.assertRaises(ValueError):
            EngineeringReviewRunRecord(
                run_id="",
                review_slot=1,
                state="pending",
                repository="cbusillo/code",
                pr_number=42,
                head_sha="abc",
                tree_sha="def",
                policy_revision="rev",
                work_request_id="req-1",
                model_id="claude",
                model_family="claude",
                binary_digest="sha256:abc",
                run_credential_hash="hash",
                created_at="2026-08-05T10:00:00Z",
                updated_at="2026-08-05T10:00:00Z",
                lease_expires_at="2026-08-05T11:00:00Z",
            )

    def test_completed_requires_decision(self) -> None:
        base = _pending_record().model_dump()
        base.update(
            {
                "state": "completed",
                "dispatched_at": "2026-08-05T10:01:00Z",
                "started_at": "2026-08-05T10:02:00Z",
                "completed_at": "2026-08-05T10:10:00Z",
                "evidence_digest": "abc123",
            }
        )
        with self.assertRaises(ValueError):
            EngineeringReviewRunRecord.model_validate(base)

    def test_nonterminal_must_not_have_decision(self) -> None:
        base = _pending_record().model_dump()
        base["decision"] = "approved"
        with self.assertRaises(ValueError):
            EngineeringReviewRunRecord.model_validate(base)

    def test_findings_bounded(self) -> None:
        base = _pending_record().model_dump()
        base["findings"] = [
            {"code": "c", "message": "m"} for _ in range(ENGINEERING_REVIEW_RUN_MAX_FINDINGS + 1)
        ]
        with self.assertRaises(ValueError):
            EngineeringReviewRunRecord.model_validate(base)

    def test_summary_length_bounded(self) -> None:
        base = _pending_record().model_dump()
        base["summary"] = "x" * (ENGINEERING_REVIEW_RUN_MAX_SUMMARY_LENGTH + 1)
        with self.assertRaises(ValueError):
            EngineeringReviewRunRecord.model_validate(base)


class DispatchEngineeringReviewRunTests(unittest.TestCase):
    def test_dispatch_transitions_to_dispatched(self) -> None:
        record = _pending_record()
        dispatched = dispatch_engineering_review_run(
            record,
            dispatched_at="2026-08-05T10:01:00Z",
            dispatched_by_host="launchplane-worker",
        )
        self.assertEqual(dispatched.state, "dispatched")
        self.assertEqual(dispatched.dispatched_by_host, "launchplane-worker")
        self.assertEqual(dispatched.dispatched_at, "2026-08-05T10:01:00Z")
        self.assertNotEqual(dispatched.lease_expires_at, record.lease_expires_at)

    def test_dispatch_requires_pending_state(self) -> None:
        record = _pending_record()
        dispatched = dispatch_engineering_review_run(
            record,
            dispatched_at="2026-08-05T10:01:00Z",
            dispatched_by_host="host",
        )
        with self.assertRaises(ValueError):
            dispatch_engineering_review_run(
                dispatched,
                dispatched_at="2026-08-05T10:02:00Z",
                dispatched_by_host="host",
            )

    def test_dispatch_requires_host(self) -> None:
        record = _pending_record()
        with self.assertRaises(ValueError):
            dispatch_engineering_review_run(
                record,
                dispatched_at="2026-08-05T10:01:00Z",
                dispatched_by_host="",
            )


class SubmitEngineeringReviewRunTests(unittest.TestCase):
    def _dispatched_record(self) -> tuple[EngineeringReviewRunRecord, str]:
        plaintext, credential_hash = generate_engineering_review_run_credential()
        record = _pending_record().model_copy(update={"run_credential_hash": credential_hash})
        dispatched = dispatch_engineering_review_run(
            record,
            dispatched_at="2026-08-05T10:01:00Z",
            dispatched_by_host="launchplane-worker",
        )
        return dispatched, plaintext

    def test_submit_with_valid_credential_completes_run(self) -> None:
        dispatched, plaintext = self._dispatched_record()
        submission = EngineeringReviewRunSubmission(
            run_credential=plaintext,
            decision="approved",
            summary="LGTM",
        )
        completed = submit_engineering_review_run(
            dispatched, submission, completed_at="2026-08-05T10:10:00Z"
        )
        self.assertEqual(completed.state, "completed")
        self.assertEqual(completed.decision, "approved")
        self.assertEqual(completed.summary, "LGTM")
        self.assertTrue(len(completed.evidence_digest) > 0)

    def test_submit_fails_closed_with_wrong_credential(self) -> None:
        dispatched, _ = self._dispatched_record()
        submission = EngineeringReviewRunSubmission(
            run_credential="wrong-credential",
            decision="approved",
        )
        with self.assertRaises(ValueError) as ctx:
            submit_engineering_review_run(
                dispatched, submission, completed_at="2026-08-05T10:10:00Z"
            )
        self.assertIn("fail closed", str(ctx.exception).lower())

    def test_submit_fails_closed_on_already_completed(self) -> None:
        dispatched, plaintext = self._dispatched_record()
        submission = EngineeringReviewRunSubmission(
            run_credential=plaintext,
            decision="approved",
        )
        completed = submit_engineering_review_run(
            dispatched, submission, completed_at="2026-08-05T10:10:00Z"
        )
        with self.assertRaises(ValueError):
            submit_engineering_review_run(
                completed, submission, completed_at="2026-08-05T10:11:00Z"
            )

    def test_submit_cannot_supply_reviewer_identity(self) -> None:
        submission_fields = EngineeringReviewRunSubmission.model_fields.keys()
        identity_fields = {
            "repository",
            "target",
            "implementer",
            "reviewer",
            "model",
            "model_family",
            "sensitive_areas",
            "evidence_digest",
        }
        disallowed = set(submission_fields) & identity_fields
        self.assertEqual(
            disallowed, set(), f"Submission must not accept identity fields: {disallowed}"
        )

    def test_evidence_digest_is_server_derived(self) -> None:
        dispatched, plaintext = self._dispatched_record()
        submission = EngineeringReviewRunSubmission(
            run_credential=plaintext,
            decision="changes_requested",
            findings=(EngineeringReviewFinding(code="style", message="Fix formatting"),),
            summary="Needs work",
        )
        completed = submit_engineering_review_run(
            dispatched, submission, completed_at="2026-08-05T10:10:00Z"
        )
        expected_digest = build_engineering_review_evidence_digest(
            dispatched,
            decision="changes_requested",
            findings=submission.findings,
            summary="Needs work",
        )
        self.assertEqual(completed.evidence_digest, expected_digest)


class ExpireEngineeringReviewRunTests(unittest.TestCase):
    def test_expire_transitions_to_expired(self) -> None:
        record = _pending_record()
        expired = expire_engineering_review_run(record, expired_at="2026-08-05T12:00:00Z")
        self.assertEqual(expired.state, "expired")
        self.assertIn("expired", expired.error_message.lower())

    def test_expire_requires_nonterminal_state(self) -> None:
        pending = _pending_record()
        expired_from_pending = expire_engineering_review_run(
            pending, expired_at="2026-08-05T12:00:00Z"
        )
        self.assertEqual(expired_from_pending.state, "expired")

        dispatched = dispatch_engineering_review_run(
            pending,
            dispatched_at="2026-08-05T10:01:00Z",
            dispatched_by_host="launchplane-worker",
        )
        expired_from_dispatched = expire_engineering_review_run(
            dispatched, expired_at="2026-08-05T12:00:00Z"
        )
        self.assertEqual(expired_from_dispatched.state, "expired")

    def test_expire_rejected_for_terminal_states(self) -> None:
        record = _pending_record()
        cancelled = cancel_engineering_review_run(record, cancelled_at="2026-08-05T11:00:00Z")
        with self.assertRaises(ValueError):
            expire_engineering_review_run(cancelled, expired_at="2026-08-05T12:00:00Z")


class CancelEngineeringReviewRunTests(unittest.TestCase):
    def test_cancel_transitions_to_cancelled(self) -> None:
        record = _pending_record()
        cancelled = cancel_engineering_review_run(
            record, cancelled_at="2026-08-05T10:30:00Z", reason="superseded"
        )
        self.assertEqual(cancelled.state, "cancelled")
        self.assertEqual(cancelled.error_message, "superseded")

    def test_cancel_terminal_state_fails(self) -> None:
        record = _pending_record()
        cancelled = cancel_engineering_review_run(record, cancelled_at="2026-08-05T10:30:00Z")
        with self.assertRaises(ValueError):
            cancel_engineering_review_run(cancelled, cancelled_at="2026-08-05T10:31:00Z")


class EvidenceDigestTests(unittest.TestCase):
    def test_digest_is_deterministic(self) -> None:
        record = _pending_record()
        finding = EngineeringReviewFinding(code="style", message="Fix it")
        digest1 = build_engineering_review_evidence_digest(
            record, decision="approved", findings=(finding,), summary="ok"
        )
        digest2 = build_engineering_review_evidence_digest(
            record, decision="approved", findings=(finding,), summary="ok"
        )
        self.assertEqual(digest1, digest2)

    def test_different_decisions_produce_different_digests(self) -> None:
        record = _pending_record()
        d_approved = build_engineering_review_evidence_digest(
            record, decision="approved", findings=(), summary=""
        )
        d_changes = build_engineering_review_evidence_digest(
            record, decision="changes_requested", findings=(), summary=""
        )
        self.assertNotEqual(d_approved, d_changes)

    def test_different_runs_produce_different_digests(self) -> None:
        record_a = _pending_record(run_id="eng-review-run-cbusillo-code-42-s1-aaa")
        record_b = _pending_record(run_id="eng-review-run-cbusillo-code-42-s2-bbb", review_slot=2)
        d_a = build_engineering_review_evidence_digest(
            record_a, decision="approved", findings=(), summary=""
        )
        d_b = build_engineering_review_evidence_digest(
            record_b, decision="approved", findings=(), summary=""
        )
        self.assertNotEqual(d_a, d_b)

    def test_digest_binds_head_sha(self) -> None:
        record_a = _pending_record(head_sha="sha1")
        record_b = _pending_record(head_sha="sha2")
        d_a = build_engineering_review_evidence_digest(
            record_a, decision="approved", findings=(), summary=""
        )
        d_b = build_engineering_review_evidence_digest(
            record_b, decision="approved", findings=(), summary=""
        )
        self.assertNotEqual(d_a, d_b)

    def test_digest_binds_policy_revision(self) -> None:
        record_a = _pending_record(policy_revision="rev-001")
        record_b = _pending_record(policy_revision="rev-002")
        d_a = build_engineering_review_evidence_digest(
            record_a, decision="approved", findings=(), summary=""
        )
        d_b = build_engineering_review_evidence_digest(
            record_b, decision="approved", findings=(), summary=""
        )
        self.assertNotEqual(d_a, d_b)


class EngineeringReviewRunStorageTests(unittest.TestCase):
    def _store(self, tmp_path: Path) -> PostgresRecordStore:
        store = PostgresRecordStore(database_url=_sqlite_url(tmp_path / "test.sqlite3"))
        store.ensure_schema()
        return store

    def test_create_and_read_pending_run(self) -> None:
        with TemporaryDirectory() as tmp:
            store = self._store(Path(tmp))
            record = _pending_record()
            store.create_engineering_review_run_record_if_absent(record)
            found = store.read_engineering_review_run_record(record.run_id)
            self.assertEqual(found.run_id, record.run_id)
            self.assertEqual(found.state, "pending")
            self.assertEqual(found.review_slot, 1)
            self.assertEqual(found.head_sha, record.head_sha)

    def test_create_if_absent_is_idempotent(self) -> None:
        with TemporaryDirectory() as tmp:
            store = self._store(Path(tmp))
            record = _pending_record()
            first, created = store.create_engineering_review_run_record_if_absent(record)
            second, created2 = store.create_engineering_review_run_record_if_absent(record)
            self.assertTrue(created)
            self.assertFalse(created2)
            self.assertEqual(first.run_id, second.run_id)

    def test_read_missing_run_raises_file_not_found(self) -> None:
        with TemporaryDirectory() as tmp:
            store = self._store(Path(tmp))
            with self.assertRaises(FileNotFoundError):
                store.read_engineering_review_run_record("no-such-run")

    def test_list_runs_by_repository(self) -> None:
        with TemporaryDirectory() as tmp:
            store = self._store(Path(tmp))
            record = _pending_record()
            store.create_engineering_review_run_record_if_absent(record)
            results = store.list_engineering_review_run_records(repository="cbusillo/code")
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0].run_id, record.run_id)

    def test_list_runs_by_work_request_id(self) -> None:
        with TemporaryDirectory() as tmp:
            store = self._store(Path(tmp))
            record = _pending_record()
            store.create_engineering_review_run_record_if_absent(record)
            results = store.list_engineering_review_run_records(
                work_request_id="every-code-cbusillo-code-42-abc"
            )
            self.assertEqual(len(results), 1)

    def test_dispatch_transitions_state(self) -> None:
        with TemporaryDirectory() as tmp:
            store = self._store(Path(tmp))
            record = _pending_record()
            store.create_engineering_review_run_record_if_absent(record)
            dispatched = store.dispatch_engineering_review_run_record(
                run_id=record.run_id,
                dispatched_at="2026-08-05T10:01:00Z",
                dispatched_by_host="launchplane-worker",
            )
            self.assertIsNotNone(dispatched)
            assert dispatched is not None
            self.assertEqual(dispatched.state, "dispatched")
            stored = store.read_engineering_review_run_record(record.run_id)
            self.assertEqual(stored.state, "dispatched")

    def test_dispatch_missing_run_raises_file_not_found(self) -> None:
        with TemporaryDirectory() as tmp:
            store = self._store(Path(tmp))
            with self.assertRaises(FileNotFoundError):
                store.dispatch_engineering_review_run_record(
                    run_id="no-such-run",
                    dispatched_at="2026-08-05T10:01:00Z",
                    dispatched_by_host="host",
                )

    def test_submit_completes_run(self) -> None:
        with TemporaryDirectory() as tmp:
            store = self._store(Path(tmp))
            plaintext, credential_hash = generate_engineering_review_run_credential()
            record = _pending_record().model_copy(update={"run_credential_hash": credential_hash})
            store.create_engineering_review_run_record_if_absent(record)
            store.dispatch_engineering_review_run_record(
                run_id=record.run_id,
                dispatched_at="2026-08-05T10:01:00Z",
                dispatched_by_host="launchplane-worker",
            )
            submission = EngineeringReviewRunSubmission(
                run_credential=plaintext,
                decision="approved",
                summary="All good",
            )
            completed = store.submit_engineering_review_run_record(
                run_id=record.run_id,
                submission=submission,
                completed_at="2026-08-05T10:10:00Z",
            )
            self.assertEqual(completed.state, "completed")
            self.assertEqual(completed.decision, "approved")
            self.assertTrue(len(completed.evidence_digest) > 0)

    def test_submit_wrong_credential_fails_closed(self) -> None:
        with TemporaryDirectory() as tmp:
            store = self._store(Path(tmp))
            _, credential_hash = generate_engineering_review_run_credential()
            record = _pending_record().model_copy(update={"run_credential_hash": credential_hash})
            store.create_engineering_review_run_record_if_absent(record)
            store.dispatch_engineering_review_run_record(
                run_id=record.run_id,
                dispatched_at="2026-08-05T10:01:00Z",
                dispatched_by_host="launchplane-worker",
            )
            submission = EngineeringReviewRunSubmission(
                run_credential="wrong",
                decision="approved",
            )
            with self.assertRaises(ValueError):
                store.submit_engineering_review_run_record(
                    run_id=record.run_id,
                    submission=submission,
                    completed_at="2026-08-05T10:10:00Z",
                )

    def test_list_stale_returns_expired_lease_records(self) -> None:
        with TemporaryDirectory() as tmp:
            store = self._store(Path(tmp))
            plaintext, credential_hash = generate_engineering_review_run_credential()
            record = _pending_record().model_copy(
                update={
                    "run_credential_hash": credential_hash,
                    "lease_expires_at": "2026-08-05T09:00:00Z",
                }
            )
            store.create_engineering_review_run_record_if_absent(record)
            store.dispatch_engineering_review_run_record(
                run_id=record.run_id,
                dispatched_at="2026-08-05T08:00:00Z",
                dispatched_by_host="launchplane-worker",
                lease_seconds=1,
            )
            stale = store.list_stale_engineering_review_run_records(as_of="2026-08-05T10:00:00Z")
            self.assertTrue(len(stale) >= 1)
            self.assertEqual(stale[0].run_id, record.run_id)

    def test_expire_stale_transitions_to_expired(self) -> None:
        with TemporaryDirectory() as tmp:
            store = self._store(Path(tmp))
            _, credential_hash = generate_engineering_review_run_credential()
            record = _pending_record().model_copy(update={"run_credential_hash": credential_hash})
            store.create_engineering_review_run_record_if_absent(record)
            dispatched = store.dispatch_engineering_review_run_record(
                run_id=record.run_id,
                dispatched_at="2026-08-05T08:00:00Z",
                dispatched_by_host="launchplane-worker",
                lease_seconds=1,
            )
            assert dispatched is not None
            result = store.expire_stale_engineering_review_run_record(
                expected_record=dispatched,
                expired_at="2026-08-05T10:00:00Z",
            )
            self.assertIsNotNone(result)
            assert result is not None
            self.assertEqual(result.state, "expired")

    def test_expire_stale_compare_and_swap_rejects_stale_expected_record(self) -> None:
        with TemporaryDirectory() as tmp:
            store = self._store(Path(tmp))
            _, credential_hash = generate_engineering_review_run_credential()
            record = _pending_record().model_copy(update={"run_credential_hash": credential_hash})
            store.create_engineering_review_run_record_if_absent(record)
            dispatched_v1 = store.dispatch_engineering_review_run_record(
                run_id=record.run_id,
                dispatched_at="2026-08-05T08:00:00Z",
                dispatched_by_host="launchplane-worker",
                lease_seconds=1,
            )
            assert dispatched_v1 is not None
            result = store.expire_stale_engineering_review_run_record(
                expected_record=dispatched_v1,
                expired_at="2026-08-05T10:00:00Z",
            )
            self.assertIsNotNone(result)
            result_again = store.expire_stale_engineering_review_run_record(
                expected_record=dispatched_v1,
                expired_at="2026-08-05T11:00:00Z",
            )
            self.assertIsNone(result_again)


if __name__ == "__main__":
    unittest.main()
