import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from control_plane.contracts.merge_train_pr_feedback_record import MergeTrainPrFeedbackRecord
from control_plane.contracts.merge_train_stack_collapse import (
    MergeTrainStackCollapsePlan,
    MergeTrainStackCollapsePlanRecord,
)
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.ordinary_agent_session_lifecycle import OrdinaryAgentSessionAdmissionDenied
from control_plane.storage.postgres import (
    LaunchplaneMergeTrainPrFeedbackRow,
    LaunchplaneMergeTrainStackCollapsePlanRow,
    PostgresRecordStore,
)
from tests.test_merge_train_controller import _stack_collapse_record


REPOSITORY = "Example/Merge-Train-Repo"


def _feedback() -> MergeTrainPrFeedbackRecord:
    return MergeTrainPrFeedbackRecord(
        feedback_id="legacy-feedback",
        repository=REPOSITORY,
        base_branch="main",
        pull_request_number=1,
        pull_request_url=f"https://github.com/{REPOSITORY}/pull/1",
        event="completed",
        marker="<!-- retained-feedback-marker -->",
        comment_markdown="Retained completion evidence.",
        source="test",
        recorded_at="2026-10-01T00:00:00Z",
        delivery_status="failed",
    )


def _collapse() -> MergeTrainStackCollapsePlanRecord:
    record = _stack_collapse_record(status="ready_for_train")
    return record.model_copy(
        update={
            "plan": MergeTrainStackCollapsePlan.model_validate(
                record.plan.model_dump() | {"repository": REPOSITORY}
            )
        }
    )


class RepositoryNameStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = Path(self.enterContext(TemporaryDirectory()))
        self.store = self.create_store()

    def create_store(self) -> FilesystemRecordStore | PostgresRecordStore:
        return FilesystemRecordStore(self.directory)

    def test_mixed_case_writes_resolve_through_each_repository_spelling(self) -> None:
        feedback, collapse = _feedback(), _collapse()
        self.store.write_merge_train_pr_feedback_record(feedback)
        self.store.write_merge_train_stack_collapse_plan_record(collapse)
        self.assertEqual(feedback.repository, REPOSITORY.lower())
        self.assertEqual(collapse.plan.repository, REPOSITORY.lower())
        for repository in (REPOSITORY, REPOSITORY.lower(), REPOSITORY.swapcase()):
            with self.subTest(repository=repository):
                self.assertEqual(
                    self.store.list_merge_train_pr_feedback_records(repository=repository),
                    (feedback,),
                )
                self.assertEqual(
                    self.store.list_merge_train_stack_collapse_plan_records(repository=repository),
                    (collapse,),
                )
        self.assertEqual(
            self.store.list_merge_train_pr_feedback_records(repository="example/elsewhere"), ()
        )
        self.assertEqual(
            self.store.list_merge_train_stack_collapse_plan_records(
                repository=REPOSITORY, base_branch="Main"
            ),
            (),
        )

    def test_legacy_repository_lookup_preserves_stored_payloads_and_identities(self) -> None:
        feedback, collapse = self._seed_legacy()
        before = self._legacy_payloads()
        found_feedback = self.store.list_merge_train_pr_feedback_records(
            repository=REPOSITORY.lower(),
            base_branch=feedback.base_branch,
            pr_number=feedback.pull_request_number,
        )
        found_collapse = self.store.list_merge_train_stack_collapse_plan_records(
            repository=REPOSITORY.lower(),
            base_branch=collapse.plan.base_branch,
            root_pull_request_number=collapse.plan.root_pull_request_number,
            status=collapse.status,
        )
        self.assertEqual([record.feedback_id for record in found_feedback], [feedback.feedback_id])
        self.assertEqual([record.record_id for record in found_collapse], [collapse.record_id])
        self.assertEqual(found_feedback[0].marker, feedback.marker)
        self.assertEqual(found_collapse[0].plan.collapse_id, collapse.plan.collapse_id)
        self.assertEqual(self._legacy_payloads(), before)

    def test_new_status_suppresses_legacy_retry_across_repository_case(self) -> None:
        feedback, _ = self._seed_legacy()
        newer = MergeTrainPrFeedbackRecord.model_validate(
            feedback.model_dump()
            | {
                "feedback_id": "new-status",
                "repository": REPOSITORY.lower(),
                "recorded_at": "2026-10-01T00:01:00Z",
                "event": "waiting",
                "delivery_status": "delivered",
            }
        )
        other = newer.model_copy(
            update={
                "feedback_id": "independent-retry",
                "pull_request_number": 2,
                "event": "completed",
                "delivery_status": "failed",
            }
        )
        self.store.write_merge_train_pr_feedback_record(newer)
        self.store.write_merge_train_pr_feedback_record(other)
        latest = self.store.list_merge_train_pr_feedback_records(latest_per_pr=True)
        self.assertEqual(
            {record.feedback_id for record in latest}, {newer.feedback_id, other.feedback_id}
        )
        retries = self.store.list_merge_train_pr_feedback_records(
            latest_per_pr=True,
            terminal_retry_candidates=True,
            delivery_status="failed",
        )
        self.assertEqual(tuple(record.feedback_id for record in retries), (other.feedback_id,))

    def _seed_legacy(self) -> tuple[MergeTrainPrFeedbackRecord, MergeTrainStackCollapsePlanRecord]:
        feedback = _feedback().model_copy(update={"repository": REPOSITORY})
        collapse = _collapse()
        collapse = collapse.model_copy(
            update={"plan": collapse.plan.model_copy(update={"repository": REPOSITORY})}
        )
        if isinstance(self.store, FilesystemRecordStore):
            self.legacy_paths = (
                self.store._write_model(
                    "launchplane_merge_train_pr_feedback", feedback.feedback_id, feedback
                ),
                self.store._write_model(
                    "launchplane_merge_train_stack_collapse_plans", collapse.record_id, collapse
                ),
            )
        else:
            with self.store._session_factory() as session, session.begin():
                session.add(
                    LaunchplaneMergeTrainPrFeedbackRow(
                        **{
                            name: getattr(feedback, name)
                            for name in (
                                "feedback_id",
                                "repository",
                                "base_branch",
                                "pull_request_number",
                                "event",
                                "delivery_status",
                                "recorded_at",
                            )
                        },
                        payload=feedback.model_dump(mode="json"),
                    )
                )
                session.add(
                    LaunchplaneMergeTrainStackCollapsePlanRow(
                        record_id=collapse.record_id,
                        status=collapse.status,
                        source=collapse.source,
                        updated_at=collapse.updated_at,
                        repository=collapse.plan.repository,
                        base_branch=collapse.plan.base_branch,
                        collapse_id=collapse.plan.collapse_id,
                        root_pull_request_number=collapse.plan.root_pull_request_number,
                        plan_status=collapse.plan.status,
                        payload=collapse.model_dump(mode="json"),
                    )
                )
        return feedback, collapse

    def _legacy_payloads(self) -> tuple[object, object]:
        if isinstance(self.store, FilesystemRecordStore):
            return json.loads(self.legacy_paths[0].read_text()), json.loads(
                self.legacy_paths[1].read_text()
            )
        with self.store._session_factory() as session:
            feedback = session.get(LaunchplaneMergeTrainPrFeedbackRow, _feedback().feedback_id)
            collapse = session.get(LaunchplaneMergeTrainStackCollapsePlanRow, _collapse().record_id)
            assert feedback is not None and collapse is not None
            return feedback.payload, collapse.payload


class RepositoryNameSQLStorageTests(RepositoryNameStorageTests):
    def test_native_write_refuses_legacy_bound_stack_across_repository_case(self) -> None:
        _, collapse = self._seed_legacy()
        assert isinstance(self.store, PostgresRecordStore)
        with self.store._session_factory() as session, session.begin():
            row = session.get(LaunchplaneMergeTrainStackCollapsePlanRow, collapse.record_id)
            assert row is not None
            row.payload = row.payload | {
                "ordinary_job_binding": {
                    "request_id": "retained-request",
                    "scope_sha256": "a" * 64,
                    "binding_revision": 1,
                }
            }
        before = self._legacy_payloads()
        for repository in (REPOSITORY.lower(), REPOSITORY.swapcase()):
            self.assertTrue(
                self.store.has_ordinary_merge_train_target_fence(
                    repository=repository,
                    base_branch=collapse.plan.base_branch,
                )
            )
        self.assertFalse(
            self.store.has_ordinary_merge_train_target_fence(
                repository=REPOSITORY,
                base_branch=collapse.plan.base_branch.swapcase(),
            )
        )
        with self.assertRaisesRegex(
            OrdinaryAgentSessionAdmissionDenied, "ordinary_progress_requires_joined_write"
        ):
            self.store.write_merge_train_stack_collapse_plan_record(
                _collapse().model_copy(update={"record_id": "native-successor"})
            )
        self.assertEqual(self._legacy_payloads(), before)
        self.assertEqual(
            [
                record.record_id
                for record in self.store.list_merge_train_stack_collapse_plan_records()
            ],
            [collapse.record_id],
        )

    def database_url(self) -> str:
        return f"sqlite+pysqlite:///{self.directory / 'records.sqlite3'}"

    def create_store(self) -> PostgresRecordStore:
        store = PostgresRecordStore(database_url=self.database_url())
        self.addCleanup(store.close)
        store.ensure_schema()
        return store
