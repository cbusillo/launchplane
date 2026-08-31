from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast
import threading
import unittest

from control_plane.contracts.administrator_enrollment import (
    ADMINISTRATOR_ENROLLMENT_AUTHORITY_STATE,
    AdministratorEnrollmentConflictError,
    AdministratorEnrollmentRecord,
    administrator_enrollment_challenge_sha256,
)
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.storage.postgres import PostgresRecordStore


_POLICY_SHA256 = "b" * 64
_POLICY_RECORD_ID = "launchplane-authz-policy-r00000000000000000368-bbbbbbbbbbbb"
_PLAN_SHA256 = "c" * 64
_IDEMPOTENCY_SHA256 = "d" * 64


def _record(
    *,
    enrollment_id: str = "administrator-enrollment-test0001",
    challenge: str = "opaque-challenge",
    reason: str = "Second human control proof.",
) -> AdministratorEnrollmentRecord:
    return AdministratorEnrollmentRecord(
        enrollment_id=enrollment_id,
        proposer_github_id=101,
        challenge_sha256=administrator_enrollment_challenge_sha256(challenge),
        reason=reason,
        provenance_sha256="a" * 64,
        created_at="2026-08-31T12:00:00+00:00",
        expires_at="2026-08-31T12:30:00+00:00",
    )


def _complete(store: Any, enrollment_id: str) -> AdministratorEnrollmentRecord:
    return cast(
        AdministratorEnrollmentRecord,
        store.complete_administrator_enrollment(
            enrollment_id=enrollment_id,
            server_derived_candidate_github_id=202,
            enrolled_at="2026-08-31T12:06:00+00:00",
            enrolled_policy_record_id=_POLICY_RECORD_ID,
            enrolled_policy_revision=368,
            enrolled_policy_sha256=_POLICY_SHA256,
            reviewed_plan_sha256=_PLAN_SHA256,
            bridge_idempotency_key_sha256=_IDEMPOTENCY_SHA256,
        ),
    )


class AdministratorEnrollmentContractTests(unittest.TestCase):
    def test_lifecycle_is_inert_and_validates_terminal_states(self) -> None:
        record = _record()
        self.assertEqual(record.authority_state, ADMINISTRATOR_ENROLLMENT_AUTHORITY_STATE)
        self.assertFalse(record.authorizes_policy)
        self.assertEqual(record.policy_bridge_state, "not_applied")
        with self.assertRaises(ValueError):
            AdministratorEnrollmentRecord.model_validate(
                record.model_dump() | {"authorizes_policy": True}
            )
        with self.assertRaises(ValueError):
            AdministratorEnrollmentRecord.model_validate(
                record.model_dump()
                | {
                    "candidate_github_id": record.proposer_github_id,
                    "control_proven_at": "2026-08-31T12:05:00+00:00",
                    "state": "control_proven",
                }
            )
        with self.assertRaises(ValueError):
            AdministratorEnrollmentRecord.model_validate(
                record.model_dump()
                | {
                    "candidate_github_id": 202,
                    "control_proven_at": "2026-08-31T12:31:00+00:00",
                    "state": "control_proven",
                }
            )

    def test_enrolled_state_requires_exact_complete_policy_evidence(self) -> None:
        record = _record()
        base = record.model_dump() | {
            "state": "enrolled",
            "candidate_github_id": 202,
            "control_proven_at": "2026-08-31T12:05:00+00:00",
            "enrolled_at": "2026-08-31T12:06:00+00:00",
            "enrolled_policy_record_id": _POLICY_RECORD_ID,
            "enrolled_policy_revision": 368,
            "enrolled_policy_sha256": _POLICY_SHA256,
            "reviewed_plan_sha256": _PLAN_SHA256,
            "bridge_idempotency_key_sha256": _IDEMPOTENCY_SHA256,
            "policy_bridge_state": "applied",
        }
        enrolled = AdministratorEnrollmentRecord.model_validate(base)
        self.assertEqual(enrolled.state, "enrolled")
        with self.assertRaises(ValueError):
            AdministratorEnrollmentRecord.model_validate(base | {"reviewed_plan_sha256": None})
        with self.assertRaises(ValueError):
            AdministratorEnrollmentRecord.model_validate(
                base | {"policy_bridge_state": "not_applied"}
            )
        with self.assertRaises(ValueError):
            AdministratorEnrollmentRecord.model_validate(
                base
                | {
                    "enrolled_policy_record_id": (
                        "launchplane-authz-policy-r00000000000000000368-aaaaaaaaaaaa"
                    )
                }
            )

    def test_timestamps_are_normalized_to_utc_whole_seconds(self) -> None:
        record = AdministratorEnrollmentRecord(
            enrollment_id="administrator-enrollment-test0002",
            proposer_github_id=101,
            challenge_sha256="a" * 64,
            reason="Normalize timestamps.",
            provenance_sha256="b" * 64,
            created_at="2026-08-31T08:00:00-04:00",
            expires_at="2026-08-31T08:30:00-04:00",
        )
        self.assertEqual(record.created_at, "2026-08-31T12:00:00+00:00")
        self.assertEqual(record.expires_at, "2026-08-31T12:30:00+00:00")
        with self.assertRaises(ValueError):
            AdministratorEnrollmentRecord.model_validate(
                _record().model_dump() | {"created_at": "2026-08-31T12:00:00.1+00:00"}
            )


class AdministratorEnrollmentStorageTests(unittest.TestCase):
    def _exercise_store(self, store: Any) -> None:
        record, created = store.create_administrator_enrollment_if_absent(_record())
        self.assertTrue(created)
        replay, created = store.create_administrator_enrollment_if_absent(record)
        self.assertFalse(created)
        self.assertEqual(replay, record)

        proven = store.prove_administrator_enrollment_control(
            enrollment_id=record.enrollment_id,
            challenge="opaque-challenge",
            server_derived_candidate_github_id=202,
            control_proven_at="2026-08-31T12:05:00+00:00",
        )
        self.assertEqual(proven.state, "control_proven")
        self.assertEqual(proven.candidate_github_id, 202)
        self.assertEqual(
            store.prove_administrator_enrollment_control(
                enrollment_id=record.enrollment_id,
                challenge="opaque-challenge",
                server_derived_candidate_github_id=202,
                control_proven_at="2026-08-31T12:05:00+00:00",
            ),
            proven,
        )
        with self.assertRaises(AdministratorEnrollmentConflictError):
            store.prove_administrator_enrollment_control(
                enrollment_id=record.enrollment_id,
                challenge="opaque-challenge",
                server_derived_candidate_github_id=202,
                control_proven_at="2026-08-31T12:06:00+00:00",
            )

        enrolled = _complete(store, record.enrollment_id)
        self.assertEqual(enrolled.state, "enrolled")
        self.assertEqual(enrolled.policy_bridge_state, "applied")
        self.assertEqual(store.read_administrator_enrollment(record.enrollment_id), enrolled)
        self.assertEqual(_complete(store, record.enrollment_id), enrolled)
        with self.assertRaises(AdministratorEnrollmentConflictError):
            store.complete_administrator_enrollment(
                enrollment_id=record.enrollment_id,
                server_derived_candidate_github_id=202,
                enrolled_at="2026-08-31T12:06:00+00:00",
                enrolled_policy_record_id=_POLICY_RECORD_ID,
                enrolled_policy_revision=368,
                enrolled_policy_sha256=_POLICY_SHA256,
                reviewed_plan_sha256="e" * 64,
                bridge_idempotency_key_sha256=_IDEMPOTENCY_SHA256,
            )

    def _exercise_duplicate_challenge(self, store: Any) -> None:
        store.create_administrator_enrollment_if_absent(_record())
        with self.assertRaises(AdministratorEnrollmentConflictError):
            store.create_administrator_enrollment_if_absent(
                _record(enrollment_id="administrator-enrollment-test0003")
            )

    def test_filesystem_round_trip_and_lifecycle(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            store = FilesystemRecordStore(Path(temporary_directory))
            self._exercise_store(store)
            record_path = (
                Path(temporary_directory)
                / "launchplane_administrator_enrollments"
                / "administrator-enrollment-test0001.json"
            )
            self.assertNotIn("opaque-challenge", record_path.read_text(encoding="utf-8"))

    def test_sqlite_postgres_store_round_trip_and_lifecycle(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            store = PostgresRecordStore(
                database_url=f"sqlite+pysqlite:///{Path(temporary_directory) / 'records.sqlite3'}"
            )
            try:
                store.ensure_schema()
                self._exercise_store(store)
            finally:
                store.close()

    def test_challenge_digest_is_globally_unique_in_both_backends(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            self._exercise_duplicate_challenge(
                FilesystemRecordStore(Path(temporary_directory) / "filesystem")
            )
            store = PostgresRecordStore(
                database_url=(f"sqlite+pysqlite:///{Path(temporary_directory) / 'records.sqlite3'}")
            )
            try:
                store.ensure_schema()
                self._exercise_duplicate_challenge(store)
            finally:
                store.close()

    def test_filesystem_challenge_digest_reservation_is_atomic(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            state_dir = Path(temporary_directory)
            barrier = threading.Barrier(2)

            def create(enrollment_id: str) -> AdministratorEnrollmentRecord | BaseException:
                store = FilesystemRecordStore(state_dir)
                try:
                    barrier.wait(timeout=5)
                    record, _created = store.create_administrator_enrollment_if_absent(
                        _record(enrollment_id=enrollment_id)
                    )
                    return record
                except BaseException as error:  # noqa: BLE001
                    return error

            with ThreadPoolExecutor(max_workers=2) as executor:
                results = tuple(
                    executor.map(
                        create,
                        (
                            "administrator-enrollment-test0004",
                            "administrator-enrollment-test0005",
                        ),
                    )
                )

        self.assertEqual(
            sum(isinstance(result, AdministratorEnrollmentRecord) for result in results),
            1,
        )
        self.assertEqual(
            sum(isinstance(result, AdministratorEnrollmentConflictError) for result in results),
            1,
        )

    def test_sqlite_terminal_transition_is_atomic_across_store_instances(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            database_url = f"sqlite+pysqlite:///{Path(temporary_directory) / 'records.sqlite3'}"
            setup_store = PostgresRecordStore(database_url=database_url)
            try:
                setup_store.ensure_schema()
                record, _ = setup_store.create_administrator_enrollment_if_absent(_record())
            finally:
                setup_store.close()
            barrier = threading.Barrier(2)

            def transition(kind: str) -> AdministratorEnrollmentRecord | BaseException:
                store = PostgresRecordStore(database_url=database_url)
                try:
                    barrier.wait(timeout=5)
                    if kind == "withdraw":
                        return store.withdraw_administrator_enrollment(
                            enrollment_id=record.enrollment_id,
                            proposer_github_id=101,
                            withdrawn_at="2026-08-31T12:05:00+00:00",
                        )
                    return store.expire_administrator_enrollment(
                        enrollment_id=record.enrollment_id,
                        expired_at="2026-08-31T12:30:00+00:00",
                    )
                except BaseException as error:  # noqa: BLE001
                    return error
                finally:
                    store.close()

            with ThreadPoolExecutor(max_workers=2) as executor:
                results = tuple(executor.map(transition, ("withdraw", "expire")))

        self.assertEqual(
            sum(isinstance(result, AdministratorEnrollmentRecord) for result in results),
            1,
        )
        self.assertEqual(
            sum(isinstance(result, AdministratorEnrollmentConflictError) for result in results),
            1,
        )

    def test_expiry_precedes_replay_and_preserves_control_proof(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            store = FilesystemRecordStore(Path(temporary_directory))
            record, _ = store.create_administrator_enrollment_if_absent(_record())
            with self.assertRaises(AdministratorEnrollmentConflictError):
                store.prove_administrator_enrollment_control(
                    enrollment_id=record.enrollment_id,
                    challenge="opaque-challenge",
                    server_derived_candidate_github_id=202,
                    control_proven_at="2026-08-31T12:30:00+00:00",
                )
            proven = store.prove_administrator_enrollment_control(
                enrollment_id=record.enrollment_id,
                challenge="opaque-challenge",
                server_derived_candidate_github_id=202,
                control_proven_at="2026-08-31T12:05:00+00:00",
            )
            expired = store.expire_administrator_enrollment(
                enrollment_id=record.enrollment_id,
                expired_at="2026-08-31T12:30:00+00:00",
            )
            self.assertEqual(expired.state, "expired")
            self.assertEqual(expired.candidate_github_id, proven.candidate_github_id)
            self.assertEqual(
                store.expire_administrator_enrollment(
                    enrollment_id=record.enrollment_id,
                    expired_at="2026-08-31T12:30:00+00:00",
                ),
                expired,
            )
            with self.assertRaises(AdministratorEnrollmentConflictError):
                store.expire_administrator_enrollment(
                    enrollment_id=record.enrollment_id,
                    expired_at="2026-08-31T12:31:00+00:00",
                )
            with self.assertRaises(AdministratorEnrollmentConflictError):
                _complete(store, record.enrollment_id)

    def test_withdrawal_is_proposer_only_and_exactly_idempotent(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            store = FilesystemRecordStore(Path(temporary_directory))
            record, _ = store.create_administrator_enrollment_if_absent(_record())
            with self.assertRaises(AdministratorEnrollmentConflictError):
                store.withdraw_administrator_enrollment(
                    enrollment_id=record.enrollment_id,
                    proposer_github_id=999,
                    withdrawn_at="2026-08-31T12:05:00+00:00",
                )
            withdrawn = store.withdraw_administrator_enrollment(
                enrollment_id=record.enrollment_id,
                proposer_github_id=101,
                withdrawn_at="2026-08-31T12:05:00+00:00",
            )
            self.assertEqual(withdrawn.state, "withdrawn")
            self.assertEqual(
                store.withdraw_administrator_enrollment(
                    enrollment_id=record.enrollment_id,
                    proposer_github_id=101,
                    withdrawn_at="2026-08-31T12:05:00+00:00",
                ),
                withdrawn,
            )
            with self.assertRaises(AdministratorEnrollmentConflictError):
                store.withdraw_administrator_enrollment(
                    enrollment_id=record.enrollment_id,
                    proposer_github_id=101,
                    withdrawn_at="2026-08-31T12:06:00+00:00",
                )


if __name__ == "__main__":
    unittest.main()
