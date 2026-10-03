import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from typing import Literal

from control_plane.contracts.merge_train_batch import MergeTrainBatchHeldOutEntry

from control_plane.merge_train import build_merge_train_dry_run_result
from tests.merge_train_policy_fixtures import build_test_merge_train_policy
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.storage.filesystem import FilesystemRecordStore
from tests.http_app_test_support import _post_merge_train_controller_run_once
from tests.support.auth import _StubVerifier
from tests.support.merge_train import (
    _FakeExpandedMergeTrainSnapshotReader,
    _FakeFailingMergeTrainGitHubClient,
    _FakeMergeTrainGitHubClient,
    _merge_train_service_identity,
    _merge_train_service_policy,
    _seed_merge_train_policy,
)


class BatchBranchRefreshTests(unittest.IsolatedAsyncioTestCase):
    async def _check_planning(
        self,
        *,
        multiple: bool,
        failed_candidate: bool = False,
        conflict: Literal["none", "leaves_batch", "leaves_single"] = "none",
    ) -> None:
        snapshot = _FakeExpandedMergeTrainSnapshotReader(
            transport=object()
        ).read_merge_train_snapshot(repository="cbusillo/sellyouroutboard", base_branch="main")
        if conflict == "leaves_batch":
            snapshot = snapshot.model_copy(
                update={
                    "pull_requests": (
                        *snapshot.pull_requests,
                        snapshot.pull_requests[1].model_copy(
                            update={
                                "number": 3,
                                "head_sha": "head-3",
                                "head_ref": "feature/third",
                                "url": f"https://github.com/{snapshot.repository}/pull/3",
                                "created_at": "2026-05-08T10:10:00Z",
                            }
                        ),
                    )
                }
            )
        conflicts = (
            ()
            if conflict == "none"
            else (
                MergeTrainBatchHeldOutEntry(
                    pull_request_number=2,
                    head_sha=snapshot.pull_requests[1].head_sha,
                ),
            )
        )
        pull_requests = snapshot.pull_requests if multiple else snapshot.pull_requests[:1]
        behind_snapshot = snapshot.model_copy(
            update={
                "pull_requests": tuple(
                    pull_request.model_copy(update={"branch_update_required": True})
                    for pull_request in pull_requests
                )
            }
        )
        if failed_candidate:
            # Base movement makes the failed candidate eligible for replanning.
            snapshot = snapshot.model_copy(update={"base_sha": "previous-base"})
        with (
            TemporaryDirectory() as directory,
            patch.dict("os.environ", {"GH_TOKEN": "token"}, clear=True),
            patch(
                "control_plane.merge_train_controller_run_once.GitHubMergeTrainClient",
                _FakeMergeTrainGitHubClient,
            ),
            patch.object(_FakeMergeTrainGitHubClient, "update_pull_request_branch") as update,
            patch(
                "control_plane.merge_train_github.GitHubMergeTrainSnapshotReader.read_merge_train_snapshot",
                return_value=snapshot,
            ),
        ):
            state_dir = Path(directory) / "state"
            _seed_merge_train_policy(state_dir)
            store = FilesystemRecordStore(state_dir=state_dir)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_merge_train_service_identity()),
                authz_policy=_merge_train_service_policy(),
                record_store_factory=lambda: store,
            )
            payload = {
                "repository": snapshot.repository,
                "base_branch": snapshot.base_branch,
                "mutate": True,
            }
            if failed_candidate:
                with patch.object(
                    _FakeMergeTrainGitHubClient, "read_merge_train_snapshot", return_value=snapshot
                ):
                    for _ in range(2):
                        response = await _post_merge_train_controller_run_once(app, payload)
                        self.assertEqual(response.status_code, 202, response.text)
                with patch(
                    "control_plane.merge_train_controller_run_once.GitHubMergeTrainClient",
                    _FakeFailingMergeTrainGitHubClient,
                ):
                    failed = await _post_merge_train_controller_run_once(app, payload)
                self.assertEqual(failed.status_code, 202, failed.text)
                self.assertEqual(failed.json()["result"]["candidate"]["status"], "failed")
            with (
                patch.object(
                    _FakeMergeTrainGitHubClient,
                    "read_merge_train_snapshot",
                    return_value=behind_snapshot,
                ),
                patch.object(
                    _FakeMergeTrainGitHubClient,
                    "probe_batch_entry_conflicts",
                    return_value=conflicts,
                    create=True,
                ) as probe,
            ):
                dry_run = await _post_merge_train_controller_run_once(
                    app, {**payload, "mutate": False}
                )
                update.assert_not_called()
                mutated = await _post_merge_train_controller_run_once(app, payload)
            self.assertEqual(dry_run.status_code, 202, dry_run.text)
            self.assertEqual(mutated.status_code, 202, mutated.text)
            result = mutated.json()["result"]
            expected_entries = tuple(
                entry
                for entry in behind_snapshot.pull_requests
                if entry.number not in {held.pull_request_number for held in conflicts}
            )
            if conflict != "none":
                probe.assert_called_once()
                self.assertEqual(
                    [entry.number for entry in probe.call_args.kwargs["queue"]],
                    [entry.number for entry in behind_snapshot.pull_requests],
                )
            if len(expected_entries) > 1:
                update.assert_not_called()
                self.assertEqual(dry_run.json()["result"]["controller_action"], "plan_candidate")
                self.assertEqual(result["controller_action"], "plan_candidate")
                candidate = result["candidate"]
                self.assertEqual(candidate["base_sha"], behind_snapshot.base_sha)
                self.assertEqual(
                    [
                        (entry["pull_request_number"], entry["head_sha"])
                        for entry in candidate["entries"]
                    ],
                    [(entry.number, entry.head_sha) for entry in expected_entries],
                )
                records = store.list_merge_train_batch_candidate_records(
                    repository=snapshot.repository, base_branch=snapshot.base_branch
                )
                planned = next(
                    record
                    for record in records
                    if record.record_id == result["merge_train_batch_candidate_record_id"]
                )
                self.assertEqual(planned.candidate.base_sha, behind_snapshot.base_sha)
            else:
                self.assertEqual(result["controller_action"], "update_branch")
                update.assert_called_once_with(
                    repository=snapshot.repository,
                    pull_request_number=pull_requests[0].number,
                    expected_head_sha=pull_requests[0].head_sha,
                )

    async def test_behind_entries_plan_a_batch_without_refreshing_heads(self) -> None:
        await self._check_planning(multiple=True)

    async def test_single_entry_still_refreshes(self) -> None:
        await self._check_planning(multiple=False)

    async def test_failed_candidate_reflows_to_a_batch_without_refreshing_heads(self) -> None:
        await self._check_planning(multiple=True, failed_candidate=True)

    async def test_failed_candidate_reflows_to_a_single_entry_and_refreshes(self) -> None:
        await self._check_planning(multiple=False, failed_candidate=True)

    async def test_probe_leaves_a_batch_and_preserves_the_remaining_heads(self) -> None:
        await self._check_planning(multiple=True, conflict="leaves_batch")

    async def test_probe_leaves_one_entry_and_restores_its_refresh(self) -> None:
        await self._check_planning(multiple=True, conflict="leaves_single")

    async def test_failed_candidate_probe_leaves_one_entry_and_restores_its_refresh(self) -> None:
        await self._check_planning(multiple=True, failed_candidate=True, conflict="leaves_single")


class BatchBranchRefreshDecisionTests(unittest.TestCase):
    def test_batch_planning_preserves_conflicts_and_required_checks(self) -> None:
        snapshot = _FakeExpandedMergeTrainSnapshotReader(
            transport=object()
        ).read_merge_train_snapshot(repository="cbusillo/sellyouroutboard", base_branch="main")
        for updates, expected in (
            ({"mergeable": "conflicting"}, "block"),
            ({"required_checks_status": "fail"}, "block"),
            ({"mergeable": "unknown"}, "wait_for_checks"),
            ({"required_checks_status": "pending"}, "wait_for_checks"),
            ({"required_checks_status": "unknown"}, "wait_for_checks"),
            ({}, "merge"),
        ):
            with self.subTest(updates=updates):
                first, second = snapshot.pull_requests
                result = build_merge_train_dry_run_result(
                    policy=build_test_merge_train_policy(),
                    snapshot=snapshot.model_copy(
                        update={
                            "pull_requests": (
                                first.model_copy(
                                    update={"branch_update_required": True, **updates}
                                ),
                                second,
                            )
                        }
                    ),
                    batch_landing=True,
                )
                self.assertEqual(result.intended_next_action, expected)
                self.assertIsNotNone(result.selected_pr)
                self.assertTrue(result.selected_pr and result.selected_pr.branch_update_required)

    def test_single_eligible_entry_and_non_merge_methods_still_refresh(self) -> None:
        snapshot = _FakeExpandedMergeTrainSnapshotReader(
            transport=object()
        ).read_merge_train_snapshot(repository="cbusillo/sellyouroutboard", base_branch="main")
        first, second = snapshot.pull_requests
        policy = build_test_merge_train_policy()
        for merge_method, eligible_second in (("merge", False), ("squash", True), ("rebase", True)):
            with self.subTest(merge_method=merge_method, eligible_second=eligible_second):
                result = build_merge_train_dry_run_result(
                    policy=policy.model_copy(
                        update={
                            "policies": (
                                policy.policies[0].model_copy(
                                    update={"merge_method": merge_method}
                                ),
                            )
                        }
                    ),
                    snapshot=snapshot.model_copy(
                        update={
                            "pull_requests": (
                                first.model_copy(update={"branch_update_required": True}),
                                second.model_copy(update={"is_draft": not eligible_second}),
                            )
                        }
                    ),
                    batch_landing=True,
                )
                self.assertEqual(result.intended_next_action, "update_branch")

    def test_direct_merge_planning_still_refreshes_even_with_multiple_entries(self) -> None:
        snapshot = _FakeExpandedMergeTrainSnapshotReader(
            transport=object()
        ).read_merge_train_snapshot(repository="cbusillo/sellyouroutboard", base_branch="main")
        first, second = snapshot.pull_requests
        result = build_merge_train_dry_run_result(
            policy=build_test_merge_train_policy(),
            snapshot=snapshot.model_copy(
                update={
                    "pull_requests": (
                        first.model_copy(update={"branch_update_required": True}),
                        second,
                    )
                }
            ),
        )
        self.assertEqual(result.intended_next_action, "update_branch")
