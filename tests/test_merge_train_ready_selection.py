import unittest
from unittest.mock import patch

from control_plane.merge_train import (
    MergeTrainDryRunSnapshot,
    MergeTrainDryRunResult,
    MergeTrainPullRequestSnapshot,
    build_merge_train_dry_run_result,
    discover_merge_train_stack,
)
from tests.merge_train_policy_fixtures import build_test_merge_train_policy
from tests.test_merge_train import _pull_request
from tests.http_app_test_support import (
    _post_merge_train_controller_run_once,
    _post_merge_train_batch_candidate_run_once,
)
from tests.support.merge_train import _FakeExpandedMergeTrainSnapshotReader
from tests.test_merge_train_dependency_requalification import (
    _controller,
    _DependencyState,
    _post,
    _entries,
    _batch_ids,
)


class ReadySelectionTests(unittest.TestCase):
    def test_pending_or_unknown_heads_step_aside_for_a_ready_head(self) -> None:
        for checks, mergeable in (
            ("pending", "mergeable"),
            ("unknown", "mergeable"),
            ("pass", "unknown"),
        ):
            for refresh in (False, True):
                with self.subTest(checks=checks, mergeable=mergeable, refresh=refresh):
                    waiting = _pull_request(1).model_copy(
                        update={"required_checks_status": checks, "mergeable": mergeable}
                    )
                    ready = _pull_request(2).model_copy(update={"branch_update_required": refresh})
                    result = self._select(waiting, ready)
                    self.assertEqual(result.queue_order, (2,))
                    assert result.selected_pr is not None
                    self.assertEqual(result.selected_pr.number, ready.number)
                    self.assertEqual(result.selected_pr.head_sha, ready.head_sha)
                    self.assertEqual(
                        result.intended_next_action, "update_branch" if refresh else "merge"
                    )
                    deferred = result.queue[0]
                    self.assertFalse(deferred.eligible)
                    self.assertTrue(deferred.ineligible_reasons)
                    self.assertEqual(deferred.required_checks_status, checks)
                    self.assertEqual(deferred.mergeable, mergeable)

    def test_settled_head_rejoins_in_original_age_order_without_label_changes(self) -> None:
        older = _pull_request(1, created_at="2026-05-01T00:00:00Z")
        later = _pull_request(2)
        waiting = older.model_copy(update={"required_checks_status": "pending"})
        self.assertEqual(self._select(waiting, later).queue_order, (2,))
        for refreshed_head in (older.head_sha, "refreshed-head"):
            ready = older.model_copy(update={"head_sha": refreshed_head})
            result = self._select(later, ready)
            self.assertEqual(result.queue_order, (1, 2))
            assert result.selected_pr is not None
            self.assertEqual(result.selected_pr.head_sha, refreshed_head)

    def test_refreshed_later_head_must_pass_its_new_checks(self) -> None:
        waiting = _pull_request(1, required_checks_status="pending")
        later = _pull_request(2, branch_update_required=True)
        self.assertEqual(self._select(waiting, later).intended_next_action, "update_branch")
        refreshed = later.model_copy(
            update={
                "head_sha": "refreshed-head",
                "branch_update_required": False,
                "required_checks_status": "pending",
            }
        )
        self.assertEqual(self._select(waiting, refreshed).intended_next_action, "wait_for_checks")
        self.assertEqual(
            self._select(
                waiting, refreshed.model_copy(update={"required_checks_status": "pass"})
            ).queue_order,
            (2,),
        )

    def test_waits_remain_truthful_when_nothing_is_ready_and_failures_still_block(self) -> None:
        for checks in ("pending", "unknown"):
            result = self._select(_pull_request(1, required_checks_status=checks))
            self.assertEqual(result.intended_next_action, "wait_for_checks")
            assert result.selected_pr is not None
            self.assertEqual(result.selected_pr.required_checks_status, checks)
        for update in (
            {"required_checks_status": "fail"},
            {"mergeable": "conflicting"},
            {
                "client_test_notes_error": "Client notes unavailable",
                "required_checks_status": "pending",
            },
        ):
            result = self._select(_pull_request(1).model_copy(update=update), _pull_request(2))
            self.assertEqual(result.intended_next_action, "block")
            assert result.selected_pr is not None
            self.assertEqual(result.selected_pr.number, 1)

    def test_client_grouping_and_individual_landing_boundary_are_preserved(self) -> None:
        waiting = _pull_request(1, required_checks_status="pending")
        features = tuple(
            _pull_request(number).model_copy(update={"owner_review_required": True})
            for number in (2, 3)
        )
        result = self._select(waiting, *features)
        self.assertEqual(result.queue_order, (2, 3))
        unaccepted = features[0].model_copy(update={"required_checks_status": "pending"})
        self.assertEqual(self._select(waiting, unaccepted).intended_next_action, "wait_for_checks")
        owned = _pull_request(2).model_copy(update={"requires_individual_landing": True})
        self.assertEqual(self._select(waiting, owned, _pull_request(3)).queue_order, (2,))

    def test_deferred_stack_root_cannot_admit_its_child(self) -> None:
        root = _pull_request(
            1, required_checks_status="pending", repository="cbusillo/sellyouroutboard"
        ).model_copy(update={"head_ref": "feature/root", "base_ref": "main"})
        child = _pull_request(2, repository="cbusillo/sellyouroutboard").model_copy(
            update={"head_ref": "feature/child", "base_ref": root.head_ref}
        )
        independent = _pull_request(3)
        self.assertEqual(self._select(root, child, independent).queue_order, (3,))
        ready_root = root.model_copy(update={"required_checks_status": "pass"})
        snapshot = self._snapshot(ready_root, child, independent)
        result = build_merge_train_dry_run_result(
            policy=build_test_merge_train_policy(),
            snapshot=snapshot,
            batch_landing=True,
            new_candidate=True,
        )
        assert result.selected_pr is not None
        self.assertEqual(result.selected_pr.number, root.number)
        stack = discover_merge_train_stack(
            policy=build_test_merge_train_policy(),
            snapshot=snapshot,
            root_pull_request_number=root.number,
        )
        self.assertEqual([entry.number for entry in stack.entries], [1, 2])

    def test_existing_candidate_membership_does_not_use_readiness_selection(self) -> None:
        pending = _pull_request(1, required_checks_status="pending")
        ready = _pull_request(2)
        result = build_merge_train_dry_run_result(
            policy=build_test_merge_train_policy(),
            snapshot=self._snapshot(pending, ready),
            batch_landing=True,
        )
        self.assertEqual(result.queue_order, (1, 2))
        self.assertEqual(result.intended_next_action, "wait_for_checks")

    @staticmethod
    def _snapshot(*pull_requests: MergeTrainPullRequestSnapshot) -> MergeTrainDryRunSnapshot:
        return MergeTrainDryRunSnapshot(
            repository="cbusillo/sellyouroutboard",
            base_branch="main",
            base_sha="base",
            pull_requests=pull_requests,
        )

    def _select(self, *pull_requests: MergeTrainPullRequestSnapshot) -> MergeTrainDryRunResult:
        return build_merge_train_dry_run_result(
            policy=build_test_merge_train_policy(),
            snapshot=self._snapshot(*pull_requests),
            batch_landing=True,
            new_candidate=True,
        )


class ReadySelectionControllerTests(unittest.IsolatedAsyncioTestCase):
    async def test_controller_and_standalone_plan_choose_the_later_ready_head(self) -> None:
        for refresh in (False, True):
            with self.subTest(refresh=refresh):

                class Reader(_FakeExpandedMergeTrainSnapshotReader):
                    def read_merge_train_snapshot(
                        self, *, repository: str, base_branch: str
                    ) -> MergeTrainDryRunSnapshot:
                        snapshot = super().read_merge_train_snapshot(
                            repository=repository, base_branch=base_branch
                        )
                        older, later = snapshot.pull_requests
                        return snapshot.model_copy(
                            update={
                                "pull_requests": (
                                    older.model_copy(update={"required_checks_status": "pending"}),
                                    later.model_copy(update={"branch_update_required": refresh}),
                                )
                            }
                        )

                async with _controller(_DependencyState(mergeable="mergeable"), Reader) as (
                    app,
                    store,
                    request,
                ):
                    dry = await _post_merge_train_controller_run_once(
                        app, {**request, "mutate": False}
                    )
                    self.assertEqual(dry.status_code, 202, dry.text)
                    result = dry.json()["result"]
                    self.assertEqual(
                        result["controller_action"],
                        "update_branch" if refresh else "plan_candidate",
                    )
                    self.assertEqual(result["dry_run_result"]["queue_order"], [2])
                    self.assertFalse(store.list_merge_train_batch_candidate_records())
                    with patch(
                        "control_plane.merge_train_batch_candidate.GitHubMergeTrainSnapshotReader",
                        Reader,
                    ):
                        plan = await _post_merge_train_batch_candidate_run_once(
                            app, {**request, "mode": "plan"}
                        )
                    self.assertEqual(plan.status_code, 202, plan.text)
                    self.assertEqual(plan.json()["result"]["dry_run_result"]["queue_order"], [2])

    async def test_readiness_changes_do_not_rebuild_active_candidate_and_older_head_rejoins(
        self,
    ) -> None:
        settled = False
        landed = False

        class Reader(_FakeExpandedMergeTrainSnapshotReader):
            def read_merge_train_snapshot(
                self, *, repository: str, base_branch: str
            ) -> MergeTrainDryRunSnapshot:
                snapshot = super().read_merge_train_snapshot(
                    repository=repository, base_branch=base_branch
                )
                older, later = snapshot.pull_requests
                older = older.model_copy(
                    update={"required_checks_status": "pass" if settled else "pending"}
                )
                return snapshot.model_copy(
                    update={
                        "base_sha": "base-after-landing" if landed else snapshot.base_sha,
                        "pull_requests": (older,) if landed else (older, later),
                    }
                )

        async with _controller(_DependencyState(mergeable="mergeable"), Reader) as (
            app,
            store,
            request,
        ):
            planned = await _post(app, request, "plan_candidate")
            self.assertEqual(_entries(planned), [2])
            await _post(app, request, "build_candidate")
            settled = True
            for action in ("observe_candidate", "plan_landing", "land_batch"):
                await _post(app, request, action)
            self.assertEqual(len(_batch_ids(store)), 1)
            landed = True
            reentered = await _post(app, request, "plan_candidate")
            self.assertEqual(_entries(reentered), [1])
