"""Receipt persistence and cross-base reservation against the real storage APIs."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from sqlalchemy import select

from control_plane.merge_train_github import MergeTrainGitHubError
from control_plane.merge_train_setup_recovery_receipt import (
    read_setup_recovery_receipt,
    receipt_id,
    reserve_setup_recovery,
)
from control_plane.storage.postgres import PostgresRecordStore, LaunchplaneMergeTrainPrFeedbackRow
from tests import test_merge_train_block_recovery as recovery_tests


def assert_committed_reservation_survives_unknown_delete(
    case: unittest.TestCase, store: PostgresRecordStore
) -> None:
    fixture = recovery_tests.CandidateBlockRecoveryTests()
    fixture.setUp()
    try:
        fixture.setup_missing_store_failure()
        for policy in fixture.store.list_merge_train_policy_records():
            store.write_merge_train_policy_record(policy)
        for candidate in fixture.store.list_merge_train_batch_candidate_records():
            store.write_merge_train_batch_candidate_record(candidate)
        for feedback in fixture.store.list_merge_train_pr_feedback_records():
            store.write_merge_train_pr_feedback_record(feedback)
        fixture.store = store
        original_request = fixture.request
        key = receipt_id(
            fixture.target.repository, fixture.current.number, fixture.current.head_sha
        )

        def lost_reply(*, method: str, path: str, body: object = None) -> object:
            if method == "DELETE":
                # A separate DB connection must observe the committed budget
                # before the provider effect, even if its reply is lost.
                with store._engine.connect() as connection:
                    payload = connection.scalar(
                        select(LaunchplaneMergeTrainPrFeedbackRow.payload).where(
                            LaunchplaneMergeTrainPrFeedbackRow.feedback_id == key
                        )
                    )
                case.assertIsNotNone(payload)
                original_request(method=method, path=path, body=body)
                raise MergeTrainGitHubError("Provider reply lost after DELETE")
            return original_request(method=method, path=path, body=body)

        fixture.transport.request.side_effect = lost_reply
        with case.assertRaises(MergeTrainGitHubError):
            fixture.advance(mutate=True)
        saved = read_setup_recovery_receipt(
            store, fixture.target.repository, fixture.current.number, fixture.current.head_sha
        )
        case.assertIsNotNone(saved)
        assert saved is not None
        # A later block on another base cannot overwrite this head's budget.
        with case.assertRaises(ValueError):
            reserve_setup_recovery(
                store=store,
                candidate=fixture.record.model_copy(
                    update={"record_id": "different-failed-record"}
                ),
                policy=fixture.target.model_copy(update={"base_branch": "another-base"}),
                evidence=saved.evidence,
                recorded_at="2026-10-18T00:06:00Z",
            )
        case.assertEqual(
            read_setup_recovery_receipt(
                store, fixture.target.repository, fixture.current.number, fixture.current.head_sha
            ),
            saved,
        )
    finally:
        fixture.doCleanups()


class SetupRecoveryReceiptStorageTests(unittest.TestCase):
    def test_committed_receipt_survives_unknown_delete_and_cannot_be_replaced(self) -> None:
        with TemporaryDirectory() as directory:
            store = PostgresRecordStore(
                database_url=f"sqlite+pysqlite:///{Path(directory) / 'records.sqlite'}"
            )
            self.addCleanup(store.close)
            store.ensure_schema()
            assert_committed_reservation_survives_unknown_delete(self, store)
