from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from pydantic import ValidationError

from control_plane.contracts.engineering_review_run import (
    EngineeringReviewAuthorityRecord,
    EngineeringReviewModelSlot,
    EngineeringReviewPullRequestTarget,
    EngineeringReviewRunRecord,
    EngineeringReviewRunSubmission,
    build_engineering_review_assignment_fingerprint,
    build_engineering_review_run_id,
    claim_engineering_review_run,
    engineering_review_run_view,
    generate_engineering_review_run_credential,
    start_engineering_review_run,
    submit_engineering_review_run,
)
from control_plane.contracts.every_code_work_request import EveryCodeWorkRequestRecord
from control_plane.storage.postgres import PostgresRecordStore


HEAD_SHA = "a" * 40
TREE_SHA = "b" * 40
BINARY_SHA256 = "c" * 64


def _authority() -> EngineeringReviewAuthorityRecord:
    return EngineeringReviewAuthorityRecord(
        repository="example/repository",
        status="active",
        policy_revision=1,
        model_slots=(
            EngineeringReviewModelSlot(
                review_slot=1,
                model_id="code-gpt-5.6-sol",
                model_family="openai",
            ),
            EngineeringReviewModelSlot(
                review_slot=2,
                model_id="claude-sonnet-4.6",
                model_family="anthropic",
            ),
        ),
        worker_runtime_id="controlled-worker-1",
        worker_host="controlled-worker.example.test",
        executable_path="/opt/every-code/bin/code",
        binary_sha256=BINARY_SHA256,
        lease_seconds=1800,
        recorded_at="2026-08-06T00:00:00Z",
        source="operator-api",
        reason="Enable controlled shadow review runs.",
    )


def _target() -> EngineeringReviewPullRequestTarget:
    return EngineeringReviewPullRequestTarget(
        repository="example/repository",
        pr_number=42,
        pr_url="https://github.com/example/repository/pull/42",
        head_sha=HEAD_SHA,
        tree_sha=TREE_SHA,
    )


def _pending_record() -> tuple[EngineeringReviewRunRecord, str]:
    authority = _authority()
    slot = authority.model_slots[0]
    fingerprint = build_engineering_review_assignment_fingerprint(
        target=_target(),
        authority=authority,
        work_request_id="every-code-request-42",
        work_request_lifecycle_id="lifecycle-42",
        model_slot=slot,
    )
    credential, credential_hash = generate_engineering_review_run_credential()
    return (
        EngineeringReviewRunRecord(
            run_id=build_engineering_review_run_id(assignment_fingerprint=fingerprint),
            assignment_fingerprint=fingerprint,
            review_slot=slot.review_slot,
            state="pending",
            repository="example/repository",
            pr_number=42,
            pr_url="https://github.com/example/repository/pull/42",
            head_sha=HEAD_SHA,
            tree_sha=TREE_SHA,
            authority_id=authority.authority_id,
            authority_digest=authority.authority_digest,
            policy_revision=authority.policy_revision,
            work_request_id="every-code-request-42",
            work_request_lifecycle_id="lifecycle-42",
            model_id=slot.model_id,
            model_family=slot.model_family,
            worker_runtime_id=authority.worker_runtime_id,
            worker_host=authority.worker_host,
            executable_path=authority.executable_path,
            binary_sha256=authority.binary_sha256,
            lease_seconds=authority.lease_seconds,
            created_at="2026-08-06T00:00:00Z",
            updated_at="2026-08-06T00:00:00Z",
            credential_hash=credential_hash,
            credential_ciphertext="encrypted-review-credential",
            credential_key_id="test-key",
        ),
        credential,
    )


class EngineeringReviewAuthorityTests(unittest.TestCase):
    def test_authority_is_explicitly_shadow_only(self) -> None:
        authority = _authority()

        self.assertEqual(authority.rollout_mode, "shadow")
        self.assertFalse(authority.authoritative)
        self.assertEqual(authority.enforcement_effect, "none")

    def test_authority_rejects_noncanonical_binary_digest(self) -> None:
        with self.assertRaises(ValidationError):
            EngineeringReviewAuthorityRecord.model_validate(
                {
                    **_authority().model_dump(mode="json"),
                    "binary_sha256": "sha256:deadbeef",
                }
            )

    def test_authority_requires_model_family_diversity(self) -> None:
        with self.assertRaises(ValidationError):
            EngineeringReviewAuthorityRecord.model_validate(
                {
                    **_authority().model_dump(mode="json"),
                    "model_slots": [
                        {
                            "review_slot": 1,
                            "model_id": "code-gpt-5.6-sol",
                            "model_family": "openai",
                        },
                        {
                            "review_slot": 2,
                            "model_id": "code-gpt-5.6-terra",
                            "model_family": "openai",
                        },
                    ],
                }
            )


class EngineeringReviewRunTests(unittest.TestCase):
    def test_public_view_redacts_all_credential_and_runtime_handoff_material(self) -> None:
        record, _credential = _pending_record()

        payload = engineering_review_run_view(record).model_dump(mode="json")

        self.assertNotIn("credential_hash", payload)
        self.assertNotIn("credential_ciphertext", payload)
        self.assertNotIn("credential_key_id", payload)
        self.assertNotIn("executable_path", payload)
        self.assertNotIn("worker_host", payload)

    def test_submission_contract_rejects_target_or_identity_fields(self) -> None:
        with self.assertRaises(ValidationError):
            EngineeringReviewRunSubmission.model_validate(
                {
                    "credential": "credential",
                    "decision": "approved",
                    "repository": "example/repository",
                }
            )

    def test_submit_rejects_expired_lease(self) -> None:
        pending, credential = _pending_record()
        claimed = claim_engineering_review_run(
            pending,
            claimed_at="2026-08-06T00:00:00Z",
        )
        running = start_engineering_review_run(
            claimed,
            started_at="2026-08-06T00:01:00Z",
        )

        with self.assertRaisesRegex(ValueError, "lease expired"):
            submit_engineering_review_run(
                running,
                EngineeringReviewRunSubmission(
                    credential=credential,
                    decision="approved",
                    summary="No blocking findings.",
                ),
                completed_at="2026-08-06T01:00:00Z",
            )

    def test_identical_completed_submission_is_replay_safe(self) -> None:
        pending, credential = _pending_record()
        running = start_engineering_review_run(
            claim_engineering_review_run(pending, claimed_at="2026-08-06T00:00:00Z"),
            started_at="2026-08-06T00:01:00Z",
        )
        submission = EngineeringReviewRunSubmission(
            credential=credential,
            decision="approved",
            summary="No blocking findings.",
        )
        completed = submit_engineering_review_run(
            running,
            submission,
            completed_at="2026-08-06T00:10:00Z",
        )

        replay = submit_engineering_review_run(
            completed,
            submission,
            completed_at="2026-08-06T00:11:00Z",
        )

        self.assertEqual(replay, completed)


class EngineeringReviewRunStorageTests(unittest.TestCase):
    def test_record_round_trip_preserves_internal_authority(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_path = Path(temporary_directory_name) / "records.sqlite3"
            store = PostgresRecordStore(database_url=f"sqlite+pysqlite:///{database_path}")
            store.ensure_schema()
            record, _credential = _pending_record()
            authority = _authority()
            store.compare_and_write_engineering_review_authority_record(
                authority,
                expected_current_authority_id="",
                expected_current_authority_digest="",
            )
            store.write_every_code_work_request_record(
                EveryCodeWorkRequestRecord(
                    request_id=record.work_request_id,
                    lifecycle_id=record.work_request_lifecycle_id,
                    source="manual",
                    state="done",
                    repository=record.repository,
                    issue_number=42,
                    issue_url="https://github.com/example/repository/issues/42",
                    trigger_label="every-code",
                    queued_at=record.created_at,
                    updated_at=record.created_at,
                    claimed_at=record.created_at,
                    claimed_by_host="worker",
                    started_at=record.created_at,
                    finished_at=record.created_at,
                    result_pr_url=record.pr_url,
                )
            )

            stored, created = store.create_engineering_review_run_record_if_absent(record)
            loaded = store.read_engineering_review_run_record(record.run_id)
            listed = store.list_engineering_review_run_records(
                repository=record.repository,
                work_request_id=record.work_request_id,
            )
            assigned = store.list_engineering_review_run_records(
                worker_runtime_id=record.worker_runtime_id,
                worker_host=record.worker_host,
                state="pending",
            )
            other_worker = store.list_engineering_review_run_records(
                worker_runtime_id="another-worker",
                worker_host=record.worker_host,
                state="pending",
            )

        self.assertTrue(created)
        self.assertEqual(stored, record)
        self.assertEqual(loaded, record)
        self.assertEqual(listed, (record,))
        self.assertEqual(assigned, (record,))
        self.assertEqual(other_worker, ())


if __name__ == "__main__":
    unittest.main()
