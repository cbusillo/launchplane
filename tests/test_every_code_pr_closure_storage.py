from __future__ import annotations

import unittest
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

from control_plane.contracts.every_code_work_request import (
    EveryCodeWorkRequestRecord,
    requeue_every_code_work_request,
)
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.storage.postgres import PostgresRecordStore


PR_URL = "https://github.com/example/widgets/pull/42"
OTHER_PR_URL = "https://github.com/example/widgets/pull/99"


def _running_work_request(*, request_id: str, pr_url: str = PR_URL) -> EveryCodeWorkRequestRecord:
    return EveryCodeWorkRequestRecord(
        request_id=request_id,
        source="github_issue_label",
        state="running",
        repository="example/widgets",
        issue_number=17,
        issue_url="https://github.com/example/widgets/issues/17",
        trigger_label="every-code",
        queued_at="2026-09-05T12:00:00Z",
        updated_at="2026-09-05T12:02:00Z",
        claimed_at="2026-09-05T12:01:00Z",
        claimed_by_host="worker-old",
        lease_expires_at="2026-09-05T12:32:00Z",
        fencing_token=1,
        attempt=1,
        started_at="2026-09-05T12:02:00Z",
        result_pr_url=pr_url,
    )


Store = FilesystemRecordStore | PostgresRecordStore


@contextmanager
def _stores(root: Path) -> Iterator[tuple[tuple[str, Store], ...]]:
    postgres_store = PostgresRecordStore(
        database_url=f"sqlite+pysqlite:///{root / 'launchplane.sqlite3'}"
    )
    postgres_store.ensure_schema()
    try:
        yield (
            ("filesystem", FilesystemRecordStore(state_dir=root / "state")),
            ("sqlite-orm", postgres_store),
        )
    finally:
        postgres_store.close()


class EveryCodePullRequestClosureStorageTests(unittest.TestCase):
    def test_close_persists_contract_result_in_each_store(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            with _stores(Path(temporary_directory_name)) as stores:
                for store_name, store in stores:
                    with self.subTest(store=store_name):
                        record = _running_work_request(request_id=f"every-code-close-{store_name}")
                        store.write_every_code_work_request_record(record)

                        closed = store.close_every_code_work_request_for_pull_request_record(
                            request_id=record.request_id,
                            expected_lifecycle_id=record.lifecycle_id,
                            pr_url=PR_URL,
                            merged=False,
                            closed_at="2026-09-05T12:05:00Z",
                        )

                        self.assertIsNotNone(closed)
                        assert closed is not None
                        self.assertEqual(closed.state, "blocked")
                        self.assertEqual(
                            closed.result_summary,
                            f"Linked pull request closed without merge: {PR_URL}",
                        )
                        self.assertEqual(closed.error_message, closed.result_summary)
                        self.assertEqual(closed.fencing_token, 1)
                        self.assertEqual(
                            store.read_every_code_work_request_record(record.request_id),
                            closed,
                        )

    def test_close_rereads_fresh_record_instead_of_overwriting_stale_snapshot(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            with _stores(Path(temporary_directory_name)) as stores:
                for store_name, store in stores:
                    with self.subTest(store=store_name):
                        record = _running_work_request(
                            request_id=f"every-code-fresh-fence-{store_name}"
                        )
                        store.write_every_code_work_request_record(record)
                        stale_caller_snapshot = store.read_every_code_work_request_record(
                            record.request_id
                        )
                        fresh_record = stale_caller_snapshot.model_copy(
                            update={
                                "state": "done",
                                "updated_at": "2026-09-05T12:06:00Z",
                                "claimed_by_host": "worker-new",
                                "lease_expires_at": "2026-09-05T12:36:00Z",
                                "fencing_token": 2,
                                "attempt": 2,
                                "finished_at": "2026-09-05T12:06:00Z",
                                "result_summary": "Worker completed from the newer claim.",
                            }
                        )
                        store.write_every_code_work_request_record(fresh_record)

                        closed = store.close_every_code_work_request_for_pull_request_record(
                            request_id=stale_caller_snapshot.request_id,
                            expected_lifecycle_id=stale_caller_snapshot.lifecycle_id,
                            pr_url=PR_URL,
                            merged=True,
                            closed_at="2026-09-05T12:07:00Z",
                        )

                        self.assertIsNotNone(closed)
                        assert closed is not None
                        expected = fresh_record.model_copy(
                            update={
                                "updated_at": "2026-09-05T12:07:00Z",
                                "result_summary": (
                                    f"Linked pull request merged: {PR_URL}\n"
                                    "Worker completed from the newer claim."
                                ),
                            }
                        )
                        self.assertEqual(closed, expected)
                        self.assertEqual(closed.fencing_token, 2)
                        self.assertEqual(closed.claimed_by_host, "worker-new")
                        self.assertEqual(stale_caller_snapshot.fencing_token, 1)
                        self.assertEqual(
                            store.read_every_code_work_request_record(record.request_id),
                            expected,
                        )

    def test_close_rejects_stale_candidate_after_rerun_changes_lifecycle(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            with _stores(Path(temporary_directory_name)) as stores:
                for store_name, store in stores:
                    with self.subTest(store=store_name):
                        terminal = _running_work_request(
                            request_id=f"every-code-rerun-{store_name}"
                        ).model_copy(
                            update={
                                "state": "done",
                                "updated_at": "2026-09-05T12:05:00Z",
                                "finished_at": "2026-09-05T12:05:00Z",
                                "result_summary": "Worker completed.",
                            }
                        )
                        store.write_every_code_work_request_record(terminal)
                        stale_webhook_candidate = store.read_every_code_work_request_record(
                            terminal.request_id
                        )
                        rerun = requeue_every_code_work_request(
                            stale_webhook_candidate,
                            queued_at="2026-09-05T12:06:00Z",
                            trigger_actor="operator",
                        )
                        store.write_every_code_work_request_record(rerun)

                        closed = store.close_every_code_work_request_for_pull_request_record(
                            request_id=stale_webhook_candidate.request_id,
                            expected_lifecycle_id=stale_webhook_candidate.lifecycle_id,
                            pr_url=PR_URL,
                            merged=True,
                            closed_at="2026-09-05T12:07:00Z",
                        )

                        self.assertNotEqual(
                            rerun.lifecycle_id, stale_webhook_candidate.lifecycle_id
                        )
                        self.assertIsNone(closed)
                        self.assertEqual(rerun.state, "queued")
                        self.assertEqual(rerun.result_pr_url, "")
                        self.assertEqual(
                            store.read_every_code_work_request_record(terminal.request_id),
                            rerun,
                        )

    def test_close_does_not_write_mismatch_or_repeat(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            with _stores(Path(temporary_directory_name)) as stores:
                for store_name, store in stores:
                    with self.subTest(store=store_name):
                        mismatched = _running_work_request(
                            request_id=f"every-code-mismatch-{store_name}",
                            pr_url=OTHER_PR_URL,
                        )
                        store.write_every_code_work_request_record(mismatched)

                        rejected = store.close_every_code_work_request_for_pull_request_record(
                            request_id=mismatched.request_id,
                            expected_lifecycle_id=mismatched.lifecycle_id,
                            pr_url=PR_URL,
                            merged=True,
                            closed_at="2026-09-05T12:05:00Z",
                        )

                        self.assertIsNone(rejected)
                        self.assertEqual(
                            store.read_every_code_work_request_record(mismatched.request_id),
                            mismatched,
                        )

                        matching = _running_work_request(
                            request_id=f"every-code-repeat-{store_name}"
                        )
                        store.write_every_code_work_request_record(matching)
                        first = store.close_every_code_work_request_for_pull_request_record(
                            request_id=matching.request_id,
                            expected_lifecycle_id=matching.lifecycle_id,
                            pr_url=PR_URL,
                            merged=True,
                            closed_at="2026-09-05T12:05:00Z",
                        )
                        repeated = store.close_every_code_work_request_for_pull_request_record(
                            request_id=matching.request_id,
                            expected_lifecycle_id=matching.lifecycle_id,
                            pr_url=PR_URL,
                            merged=True,
                            closed_at="2026-09-05T12:06:00Z",
                        )

                        self.assertIsNotNone(first)
                        self.assertIsNone(repeated)
                        self.assertEqual(
                            store.read_every_code_work_request_record(matching.request_id),
                            first,
                        )


if __name__ == "__main__":
    unittest.main()
