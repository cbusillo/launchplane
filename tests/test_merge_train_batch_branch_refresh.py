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
        accepted_features: bool = False,
        individual: bool = False,
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
                    pull_request.model_copy(
                        update={
                            "branch_update_required": True,
                            "requires_individual_landing": individual,
                        }
                    )
                    for pull_request in pull_requests
                )
            }
        )
        if accepted_features:
            ready = tuple(
                pr.model_copy(update={"owner_review_required": True})
                for pr in behind_snapshot.pull_requests
            )
            waiting = tuple(
                ready[0].model_copy(
                    update={
                        "number": number,
                        "head_sha": f"head-{number}",
                        "head_ref": f"feature/{number}",
                        "required_checks_status": status,
                        "created_at": "2026-05-08T09:00:00Z",
                    }
                )
                for number, status in ((3, "pending"), (4, "fail"), (5, "unknown"))
            )
            behind_snapshot = behind_snapshot.model_copy(
                update={"pull_requests": (*waiting, *ready)}
            )
        if failed_candidate:
            # Base movement makes the failed candidate eligible for replanning.
            snapshot = snapshot.model_copy(update={"base_sha": "previous-base"})
        with (
            TemporaryDirectory() as directory,
            patch("control_plane.http_app.resolve_merge_train_github_token", return_value="token"),
            patch(
                "control_plane.merge_train_controller_run_once.GitHubMergeTrainClient",
                _FakeMergeTrainGitHubClient,
            ),
            patch.object(_FakeMergeTrainGitHubClient, "update_pull_request_branch") as update,
            patch.object(
                _FakeMergeTrainGitHubClient, "ensure_batch_pull_request", return_value=99
            ) as candidate_pr,
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
                if conflict == "none" and not individual:
                    actions = []
                    for _ in range(3):
                        response = await _post_merge_train_controller_run_once(app, payload)
                        self.assertEqual(response.status_code, 202, response.text)
                        actions.append(response.json()["result"]["controller_action"])
                    self.assertEqual(
                        actions,
                        ["build_candidate", "observe_candidate", "plan_landing"],
                    )
                    planned_landing = store.list_merge_train_batch_landing_plan_records(
                        repository=snapshot.repository, base_branch=snapshot.base_branch
                    )
                    self.assertTrue(planned_landing)
                    self.assertTrue(
                        all(
                            record.landing_plan.candidate_pull_request_number == 99
                            for record in planned_landing
                        )
                    )
                    self.assertTrue(candidate_pr.called)
            self.assertEqual(dry_run.status_code, 202, dry_run.text)
            self.assertEqual(mutated.status_code, 202, mutated.text)
            result = mutated.json()["result"]
            expected_entries = tuple(
                entry
                for entry in behind_snapshot.pull_requests
                if entry.number not in {held.pull_request_number for held in conflicts}
                and (not accepted_features or entry.required_checks_status == "pass")
            )
            if conflict != "none":
                probe.assert_called_once()
                self.assertEqual(
                    [entry.number for entry in probe.call_args.kwargs["queue"]],
                    [entry.number for entry in behind_snapshot.pull_requests],
                )
            if expected_entries and not individual:
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
                candidate_pr.assert_not_called()
                update.assert_called_once_with(
                    repository=snapshot.repository,
                    pull_request_number=pull_requests[0].number,
                    expected_head_sha=pull_requests[0].head_sha,
                )

    async def test_behind_entries_plan_a_batch_without_refreshing_heads(self) -> None:
        await self._check_planning(multiple=True)

    async def test_accepted_features_land_together_while_other_features_wait(self) -> None:
        await self._check_planning(multiple=True, accepted_features=True)

    async def test_single_entry_uses_a_candidate_without_refreshing(self) -> None:
        await self._check_planning(multiple=False)

    async def test_owned_single_entry_keeps_its_original_pr_refresh(self) -> None:
        await self._check_planning(multiple=False, individual=True)

    async def test_failed_candidate_reflows_to_a_batch_without_refreshing_heads(self) -> None:
        await self._check_planning(multiple=True, failed_candidate=True)

    async def test_failed_candidate_reflows_to_a_single_entry_without_refreshing(self) -> None:
        await self._check_planning(multiple=False, failed_candidate=True)

    async def test_probe_leaves_a_batch_and_preserves_the_remaining_heads(self) -> None:
        await self._check_planning(multiple=True, conflict="leaves_batch")

    async def test_probe_leaves_one_entry_and_preserves_its_head(self) -> None:
        await self._check_planning(multiple=True, conflict="leaves_single")

    async def test_failed_candidate_probe_leaves_one_entry_and_preserves_its_head(self) -> None:
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

    def test_individual_landing_and_non_merge_methods_still_refresh(self) -> None:
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
                                first.model_copy(
                                    update={
                                        "branch_update_required": True,
                                        "requires_individual_landing": merge_method == "merge",
                                    }
                                ),
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
