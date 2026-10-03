from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
import unittest
from unittest.mock import patch

from control_plane.contracts.merge_train_stack_collapse import (
    MergeTrainStackCollapsePlanRecord,
    build_merge_train_stack_collapse_plan_record,
)
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.merge_train import MergeTrainDryRunSnapshot
from control_plane.merge_train_github import MergeTrainGitHubError, MergeTrainGitHubStaleHeadError
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.workflows.merge_train_controller import (
    decide_merge_train_controller_record_action,
)
from tests.support.merge_train import (
    _FakeCollapsedRootStackedMergeTrainSnapshotReader,
    _FakeMergeTrainGitHubClient,
    _merge_train_service_identity,
    _merge_train_service_policy,
    _seed_executed_merge_train_stack_collapse_plan_record,
    _seed_merge_train_policy,
)

from tests.http_app_test_support import _post_merge_train_controller_run_once
from tests.support.auth import _StubVerifier


class CollapseReconciliationTests(unittest.IsolatedAsyncioTestCase):
    async def test_retires_changed_and_missing_roots_without_reviving_older_progress(self) -> None:
        for missing in (False, True):
            for mutate in (False, True):
                with self.subTest(missing=missing, mutate=mutate):

                    class Reader(_FakeCollapsedRootStackedMergeTrainSnapshotReader):
                        def read_merge_train_snapshot(
                            self, *, repository: str, base_branch: str
                        ) -> MergeTrainDryRunSnapshot:
                            snapshot = super().read_merge_train_snapshot(
                                repository=repository, base_branch=base_branch
                            )
                            root = snapshot.pull_requests[0].model_copy(
                                update={"head_sha": "new-head"}
                            )
                            return snapshot.model_copy(
                                update={"pull_requests": () if missing else (root,)}
                            )

                    with (
                        TemporaryDirectory() as directory,
                        patch.dict("os.environ", {"GH_TOKEN": "token"}, clear=True),
                    ):
                        state_dir = Path(directory) / "state"
                        _seed_merge_train_policy(state_dir)
                        _seed_executed_merge_train_stack_collapse_plan_record(state_dir)

                        class InterruptedStore(FilesystemRecordStore):
                            failed = False

                            def write_merge_train_stack_collapse_plan_record(
                                self, record: MergeTrainStackCollapsePlanRecord
                            ) -> Path:
                                if (
                                    mutate
                                    and not self.failed
                                    and record.status == "superseded"
                                    and record.plan.status == "waiting_for_root_checks"
                                ):
                                    self.failed = True
                                    raise MergeTrainGitHubError("interrupted retirement write")
                                return super().write_merge_train_stack_collapse_plan_record(record)

                        store = InterruptedStore(state_dir)
                        app = create_launchplane_fastapi_app(
                            verifier=_StubVerifier(_merge_train_service_identity()),
                            authz_policy=_merge_train_service_policy(),
                            record_store_factory=lambda: store,
                        )
                        with (
                            patch(
                                "control_plane.merge_train_github.GitHubMergeTrainSnapshotReader",
                                Reader,
                            ),
                            patch(
                                "control_plane.merge_train_controller_run_once.GitHubMergeTrainClient",
                                _FakeMergeTrainGitHubClient,
                            ),
                        ):
                            response = await _post_merge_train_controller_run_once(
                                app,
                                {
                                    "schema_version": 1,
                                    "repository": "cbusillo/sellyouroutboard",
                                    "base_branch": "main",
                                    "mutate": mutate,
                                },
                            )
                            if mutate:
                                self.assertEqual(response.status_code, 502, response.text)
                                response = await _post_merge_train_controller_run_once(
                                    app,
                                    {
                                        "schema_version": 1,
                                        "repository": "cbusillo/sellyouroutboard",
                                        "base_branch": "main",
                                        "mutate": True,
                                    },
                                )
                        self.assertEqual(response.status_code, 202, response.text)
                        records = store.list_merge_train_stack_collapse_plan_records()
                        self.assertTrue(records)
                        if mutate:
                            self.assertTrue(
                                all(record.status == "superseded" for record in records)
                            )
                            reason = (
                                "root_missing_from_open_snapshot"
                                if missing
                                else "root_head_changed"
                            )
                            self.assertTrue(all(reason in record.source for record in records))
                            decision = decide_merge_train_controller_record_action(
                                candidate_records=(),
                                landing_plan_records=(),
                                stack_collapse_plan_records=store.list_merge_train_stack_collapse_plan_records(
                                    status="active"
                                ),
                            )
                            self.assertEqual(decision.action, "idle")
                        else:
                            self.assertTrue(all(record.status == "active" for record in records))

    async def test_reconciles_every_root_and_resumes_after_second_stack_failure(self) -> None:
        comments: list[int] = []
        failed = False

        class Reader(_FakeCollapsedRootStackedMergeTrainSnapshotReader):
            def read_merge_train_snapshot(
                self, *, repository: str, base_branch: str
            ) -> MergeTrainDryRunSnapshot:
                snapshot = super().read_merge_train_snapshot(
                    repository=repository, base_branch=base_branch
                )
                root = snapshot.pull_requests[0].model_copy(
                    update={"head_sha": "refreshed-new-root"}
                )
                other = root.model_copy(
                    update={
                        "number": 11,
                        "head_sha": "refreshed-other",
                        "head_ref": "feature/other",
                        "url": f"https://github.com/{repository}/pull/11",
                    }
                )
                return snapshot.model_copy(update={"pull_requests": (root, other)})

        class Client(_FakeMergeTrainGitHubClient):
            def find_pull_request_comment_url(
                self, *, repository: str, pull_request_number: int, body_contains: str
            ) -> str:
                return ""

            def branch_contains_commit(
                self, *, repository: str, branch_ref: str, commit_sha: str
            ) -> bool:
                return (branch_ref, commit_sha) in {
                    ("refreshed-other", "collapsed-other"),
                    ("refreshed-new-root", "stack-merge-2-into-1"),
                    ("refreshed-new-root", "new-collapsed-root"),
                }

            def pull_request_is_closed(
                self, *, repository: str, pull_request_number: int, expected_head_sha: str
            ) -> bool:
                if pull_request_number == 2 and expected_head_sha != "child-new-head":
                    raise MergeTrainGitHubStaleHeadError("old child head cannot be disposed")
                return True

            def comment_pull_request(
                self, *, repository: str, pull_request_number: int, body: str
            ) -> str:
                nonlocal failed
                if pull_request_number == 12 and not failed:
                    failed = True
                    raise MergeTrainGitHubError("interrupted second stack")
                comments.append(pull_request_number)
                return f"https://github.com/{repository}/pull/{pull_request_number}#comment"

        with (
            TemporaryDirectory() as directory,
            patch.dict("os.environ", {"GH_TOKEN": "token"}, clear=True),
        ):
            state_dir = Path(directory) / "state"
            _seed_merge_train_policy(state_dir)
            first_id = _seed_executed_merge_train_stack_collapse_plan_record(state_dir)
            store = FilesystemRecordStore(state_dir)
            first = next(
                record
                for record in store.list_merge_train_stack_collapse_plan_records()
                if record.record_id == first_id
            )
            other_plan = first.plan.model_copy(
                update={
                    "collapse_id": "other-collapse",
                    "root_pull_request_number": 11,
                    "root_head_ref": "feature/other",
                    "entries": tuple(
                        entry.model_copy(
                            update={"pull_request_number": entry.pull_request_number + 10}
                        )
                        for entry in first.plan.entries
                    ),
                    "mutations": tuple(
                        mutation.model_copy(
                            update={
                                "child_pull_request_number": mutation.child_pull_request_number
                                + 10,
                                "parent_pull_request_number": mutation.parent_pull_request_number
                                + 10,
                                "merge_commit_sha": "collapsed-other",
                            }
                        )
                        for mutation in first.plan.mutations
                    ),
                    "child_dispositions": tuple(
                        disposition.model_copy(
                            update={"pull_request_number": disposition.pull_request_number + 10}
                        )
                        for disposition in first.plan.child_dispositions
                    ),
                }
            )
            store.write_merge_train_stack_collapse_plan_record(
                build_merge_train_stack_collapse_plan_record(
                    plan=other_plan, source="test:other", updated_at=first.updated_at
                )
            )
            # Retire the first exact-head wait too, simulating a root returning after a push.
            for record in store.list_merge_train_stack_collapse_plan_records():
                if record.plan.collapse_id == first.plan.collapse_id:
                    store.write_merge_train_stack_collapse_plan_record(
                        record.model_copy(update={"status": "superseded"})
                    )
            latest_plan = first.plan.model_copy(
                update={
                    "collapse_id": "new-collapse-of-first-root",
                    "created_at": "2026-05-14T21:02:00Z",
                    "mutations": tuple(
                        mutation.model_copy(update={"merge_commit_sha": "new-collapsed-root"})
                        for mutation in first.plan.mutations
                    ),
                    "child_dispositions": tuple(
                        disposition.model_copy(update={"expected_head_sha": "child-new-head"})
                        for disposition in first.plan.child_dispositions
                    ),
                }
            )
            store.write_merge_train_stack_collapse_plan_record(
                build_merge_train_stack_collapse_plan_record(
                    plan=latest_plan,
                    source="test:new-child-head",
                    updated_at="2026-05-14T21:02:00Z",
                )
            )
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_merge_train_service_identity()),
                authz_policy=_merge_train_service_policy(),
                record_store_factory=lambda: store,
            )
            payload = {
                "schema_version": 1,
                "repository": "cbusillo/sellyouroutboard",
                "base_branch": "main",
                "mutate": True,
            }
            results: list[dict[str, Any]] = []
            with (
                patch("control_plane.merge_train_github.GitHubMergeTrainSnapshotReader", Reader),
                patch(
                    "control_plane.merge_train_controller_run_once.GitHubMergeTrainClient", Client
                ),
            ):
                for _ in range(6):
                    response = await _post_merge_train_controller_run_once(app, payload)
                    if response.status_code == 502:
                        self.assertTrue(failed)
                        self.assertEqual(comments, [2])
                        completed = store.list_merge_train_stack_collapse_plan_records()
                        self.assertTrue(
                            any(
                                record.plan.root_pull_request_number == 1
                                and record.plan.status == "ready_for_train"
                                for record in completed
                            )
                        )
                        self.assertFalse(
                            any(
                                record.plan.root_pull_request_number == 11
                                and record.plan.status == "ready_for_train"
                                for record in completed
                            )
                        )
                        continue
                    self.assertEqual(response.status_code, 202, response.text)
                    results.append(response.json()["result"])
            plans = results[-1]["stack_collapse_plans"]
            self.assertEqual({plan["root_pull_request_number"] for plan in plans}, {1, 11})
            self.assertTrue(all(plan["status"] == "ready_for_train" for plan in plans))
            self.assertEqual(comments, [2, 12])
            self.assertTrue(failed)
