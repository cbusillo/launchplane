from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast
import unittest
from unittest.mock import patch

from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.merge_train import MergeTrainDryRunSnapshot
from control_plane.merge_train_controller_feedback import build_feedback_payloads
from control_plane.merge_train_github import RecordingMergeTrainGitHubTransport
from control_plane.storage.filesystem import FilesystemRecordStore
from tests.http_app_test_support import _post_merge_train_batch_candidate_run_once
from tests.support.auth import _StubVerifier
from tests.support.merge_train import (
    _FakeCollapsedRootStackedMergeTrainSnapshotReader,
    _merge_train_service_identity,
    _merge_train_service_policy,
    _seed_executed_merge_train_stack_collapse_plan_record,
    _seed_merge_train_policy,
)


class BatchPlanOrderingTests(unittest.IsolatedAsyncioTestCase):
    async def test_blocked_saved_root_does_not_plan_another_carried_child_collapse(self) -> None:
        for checks, mergeable in (("fail", "mergeable"), ("pass", "conflicting")):
            with self.subTest(checks=checks, mergeable=mergeable):
                await self._assert_blocked_plan(checks=checks, mergeable=mergeable)

    async def _assert_blocked_plan(self, *, checks: str, mergeable: str) -> None:
        class Reader(_FakeCollapsedRootStackedMergeTrainSnapshotReader):
            def read_merge_train_snapshot(
                self, *, repository: str, base_branch: str
            ) -> MergeTrainDryRunSnapshot:
                snapshot = super().read_merge_train_snapshot(
                    repository=repository, base_branch=base_branch
                )
                root, child = snapshot.pull_requests
                return snapshot.model_copy(
                    update={
                        "pull_requests": (
                            root.model_copy(
                                update={"required_checks_status": checks, "mergeable": mergeable}
                            ),
                            child.model_copy(update={"required_checks_status": "pass"}),
                        )
                    }
                )

        with (
            TemporaryDirectory() as directory,
            patch.dict("os.environ", {"GH_TOKEN": "token"}, clear=True),
        ):
            state_dir = Path(directory) / "state"
            _seed_merge_train_policy(state_dir)
            saved_id = _seed_executed_merge_train_stack_collapse_plan_record(state_dir)
            store = FilesystemRecordStore(state_dir)
            before = store.list_merge_train_stack_collapse_plan_records()
            saved = next(record for record in before if record.record_id == saved_id)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_merge_train_service_identity()),
                authz_policy=_merge_train_service_policy(),
                record_store_factory=lambda: store,
            )
            transport = RecordingMergeTrainGitHubTransport(responses=())
            with (
                patch(
                    "control_plane.merge_train_batch_candidate.GitHubMergeTrainSnapshotReader",
                    Reader,
                ),
                patch(
                    "control_plane.merge_train_batch_candidate.UrllibMergeTrainGitHubTransport",
                    return_value=transport,
                ),
            ):
                response = await _post_merge_train_batch_candidate_run_once(
                    app,
                    {"repository": saved.plan.repository, "base_branch": "main", "mode": "plan"},
                )
            self.assertEqual(response.status_code, 202, response.text)
            payload = response.json()
            result = payload["result"]
            self.assertEqual(result.get("next_action"), "block", result)
            self.assertEqual(result["dry_run_result"]["intended_next_action"], "block")
            selected = result["dry_run_result"]["selected_pr"]
            self.assertEqual(selected["number"], saved.plan.root_pull_request_number)
            self.assertEqual(selected["head_sha"], saved.plan.mutations[-1].merge_commit_sha)
            self.assertEqual(selected["required_checks_status"], checks)
            self.assertEqual(selected["mergeable"], mergeable)
            self.assertEqual(payload["records"], {})
            self.assertNotIn("stack_collapse_plan", result)
            self.assertNotIn("candidate", result)
            self.assertEqual(store.list_merge_train_stack_collapse_plan_records(), before)
            self.assertFalse(store.list_merge_train_batch_candidate_records())
            self.assertFalse(transport.requests)
            feedback = build_feedback_payloads(response=payload, phase="batch-candidate")
            self.assertEqual(
                [(entry["pull_request_number"], entry["event"]) for entry in feedback],
                [(selected["number"], "blocked")],
            )
            self.assertEqual(feedback[0]["repository"], saved.plan.repository)
            self.assertEqual(feedback[0]["base_branch"], saved.plan.base_branch)
            self.assertEqual(feedback[0]["controller_action"], "block")
            self.assertEqual(feedback[0]["controller_record_id"], "")
            self.assertIn(
                "checks failed" if checks == "fail" else "merge conflicts",
                cast(str, feedback[0]["message"]),
            )
