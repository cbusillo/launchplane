import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from control_plane.merge_train_admission import build_merge_train_controller_status_read_model
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.storage.postgres import PostgresRecordStore
from tests.test_merge_train_admission import _candidate_record, _run_record


class MergeTrainRunHistoryTests(unittest.TestCase):
    def test_latest_level1_run_is_ordered_with_independent_controller_history(self) -> None:
        older = _run_record(recorded_at="2026-09-28T21:53:12Z", mutation="wait")
        newer = _run_record(recorded_at="2026-09-30T21:53:12Z")
        candidate = _candidate_record(status="planned").model_copy(
            update={"updated_at": "2026-10-04T09:10:59Z"}
        )
        other_repo = newer.model_copy(
            update={
                "run_id": "other-repository",
                "repository": "example/other",
                "recorded_at": candidate.updated_at,
            }
        )
        other_branch = newer.model_copy(
            update={
                "run_id": "other-branch",
                "base_branch": "other",
                "recorded_at": candidate.updated_at,
            }
        )
        for backend in ("filesystem", "database"):
            for history in ((older, newer), (newer, older)):
                with (
                    self.subTest(backend=backend, first=history[0].run_id),
                    TemporaryDirectory() as directory,
                ):
                    root = Path(directory)
                    store: FilesystemRecordStore | PostgresRecordStore
                    if backend == "filesystem":
                        store = FilesystemRecordStore(state_dir=root / "state")
                    else:
                        store = PostgresRecordStore(
                            database_url=f"sqlite+pysqlite:///{root / 'records.sqlite3'}"
                        )
                        store.ensure_schema()
                    try:
                        for record in (*history, other_repo, other_branch):
                            store.write_merge_train_run_record(record)
                        store.write_merge_train_batch_candidate_record(candidate)
                        model = build_merge_train_controller_status_read_model(
                            store=store,
                            repository=newer.repository,
                            base_branch=newer.base_branch,
                            generated_at=candidate.updated_at,
                            current_policy_key=candidate.candidate.policy_key,
                            current_policy_sha256=candidate.candidate.policy_sha256,
                        )
                        self.assertEqual(model.latest_run, newer)
                        self.assertEqual(model.latest_run_source, "level1")
                        self.assertEqual(model.latest_run_age_seconds, 299867)
                        self.assertEqual(
                            model.controller_records[0].updated_at, candidate.updated_at
                        )
                        self.assertEqual(model.admission.controller_action, "build_candidate")
                        self.assertEqual(
                            store.list_merge_train_run_records(
                                repository=newer.repository, base_branch=newer.base_branch
                            ),
                            (newer, older),
                        )
                    finally:
                        if isinstance(store, PostgresRecordStore):
                            store.close()
