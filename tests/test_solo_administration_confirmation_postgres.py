from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import threading
import unittest

from control_plane.contracts.solo_administration_confirmation import (
    SoloAdministrationConfirmationConflictError,
    SoloAdministrationConfirmationRecord,
)
from control_plane.storage.postgres import PostgresRecordStore
from tests.test_postgres_integration import (
    _isolated_postgres_database,
    _upgrade_empty_database_to_head,
)
from tests.test_solo_administration_confirmation import _issue_record


def _consume_once(
    database_url: str,
    record: SoloAdministrationConfirmationRecord,
    barrier: threading.Barrier,
) -> str:
    store = PostgresRecordStore(database_url=database_url)
    try:
        barrier.wait(timeout=10)
        store.consume_solo_administration_confirmation(
            confirmation_id=record.confirmation_id,
            active_policy_record_id=record.active_policy_record_id,
            active_policy_revision=record.active_policy_revision,
            active_policy_sha256=record.active_policy_sha256,
            candidate_policy_sha256=record.candidate_policy_sha256,
            candidate_administrator_quorum=1,
            candidate_distinct_human_administrator_count=1,
            reviewed_plan_sha256=record.reviewed_plan_sha256,
            human_session_id_sha256=record.human_session_id_sha256,
            github_id=record.github_id,
            idempotency_scope_sha256=record.idempotency_scope_sha256,
            idempotency_key_sha256=record.idempotency_key_sha256,
            acknowledgement_sha256=record.acknowledgement_sha256,
            secret_sha256=record.secret_sha256,
            terminal_at="2026-08-31T12:01:00Z",
        )
        return "consumed"
    except SoloAdministrationConfirmationConflictError:
        return "conflict"
    finally:
        store.close()


class RealPostgresSoloAdministrationConfirmationConcurrencyTests(unittest.TestCase):
    def test_consumed_confirmation_backing_is_exact_digest_bounded(self) -> None:
        with _isolated_postgres_database() as database_url:
            _upgrade_empty_database_to_head(database_url)
            store = PostgresRecordStore(database_url=database_url)
            try:
                record = _issue_record()
                store.issue_solo_administration_confirmation(record)
                self.assertFalse(
                    store.has_consumed_solo_administration_confirmation(
                        candidate_policy_sha256=record.candidate_policy_sha256
                    )
                )
                store.consume_solo_administration_confirmation(
                    confirmation_id=record.confirmation_id,
                    active_policy_record_id=record.active_policy_record_id,
                    active_policy_revision=record.active_policy_revision,
                    active_policy_sha256=record.active_policy_sha256,
                    candidate_policy_sha256=record.candidate_policy_sha256,
                    candidate_administrator_quorum=1,
                    candidate_distinct_human_administrator_count=1,
                    reviewed_plan_sha256=record.reviewed_plan_sha256,
                    human_session_id_sha256=record.human_session_id_sha256,
                    github_id=record.github_id,
                    idempotency_scope_sha256=record.idempotency_scope_sha256,
                    idempotency_key_sha256=record.idempotency_key_sha256,
                    acknowledgement_sha256=record.acknowledgement_sha256,
                    secret_sha256=record.secret_sha256,
                    terminal_at="2026-08-31T12:01:00Z",
                )
                self.assertTrue(
                    store.has_consumed_solo_administration_confirmation(
                        candidate_policy_sha256=record.candidate_policy_sha256
                    )
                )
                self.assertFalse(
                    store.has_consumed_solo_administration_confirmation(
                        candidate_policy_sha256="0" * 64
                    )
                )
            finally:
                store.close()

    def test_concurrent_consumers_have_one_winner_and_retain_terminal_row(self) -> None:
        with _isolated_postgres_database() as database_url:
            _upgrade_empty_database_to_head(database_url)
            store = PostgresRecordStore(database_url=database_url)
            try:
                record = _issue_record()
                store.issue_solo_administration_confirmation(record)
                barrier = threading.Barrier(2)
                with ThreadPoolExecutor(max_workers=2) as executor:
                    results = list(
                        executor.map(
                            lambda _: _consume_once(
                                database_url,
                                record,
                                barrier,
                            ),
                            range(2),
                        )
                    )
                self.assertEqual(results.count("consumed"), 1)
                self.assertEqual(results.count("conflict"), 1)
                self.assertEqual(
                    store.read_solo_administration_confirmation(record.confirmation_id).state,
                    "consumed",
                )
            finally:
                store.close()
