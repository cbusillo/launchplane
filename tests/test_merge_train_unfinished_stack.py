from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.merge_train import (
    MergeTrainDryRunSnapshot,
    build_merge_train_dry_run_result,
    discover_merge_train_stack,
)
from control_plane.merge_train_controller_feedback import build_feedback_payloads
from control_plane.storage.filesystem import FilesystemRecordStore
from tests.http_app_test_support import _post_merge_train_controller_run_once
from tests.support.auth import _StubVerifier
from tests.merge_train_policy_fixtures import build_test_merge_train_policy
from tests.support.merge_train import (
    _FakeMergeTrainGitHubClient,
    _FakeStackedMergeTrainSnapshotReader,
    _merge_train_service_identity,
    _merge_train_service_policy,
    _seed_merge_train_policy,
    _seed_merge_train_stack_collapse_plan_record,
)


class _UnfinishedStackReader(_FakeStackedMergeTrainSnapshotReader):
    def read_merge_train_snapshot(
        self, *, repository: str, base_branch: str
    ) -> MergeTrainDryRunSnapshot:
        snapshot = super().read_merge_train_snapshot(repository=repository, base_branch=base_branch)
        root, child = snapshot.pull_requests
        independent = root.model_copy(
            update={
                "number": 3,
                "head_sha": "head-independent",
                "head_ref": "feature/independent",
                "created_at": "2026-05-08T12:00:00Z",
            }
        )
        return snapshot.model_copy(
            update={
                "pull_requests": (
                    root,
                    child.model_copy(update={"is_draft": True, "labels": (), "label_actors": ()}),
                    independent,
                )
            }
        )


class UnfinishedStackTests(unittest.IsolatedAsyncioTestCase):
    async def test_unfinished_stack_does_not_stop_independent_landing(self) -> None:
        await self._assert_independent_landing(saved_plan=False)

    async def test_planned_stack_with_new_draft_child_does_not_stop_independent_landing(
        self,
    ) -> None:
        await self._assert_independent_landing(saved_plan=True)

    async def _assert_independent_landing(self, *, saved_plan: bool) -> None:
        with (
            TemporaryDirectory() as tmp,
            patch("control_plane.http_app.resolve_merge_train_github_token", return_value="token"),
            patch(
                "control_plane.merge_train_controller_run_once.GitHubMergeTrainClient",
                _FakeMergeTrainGitHubClient,
            ),
            patch(
                "control_plane.merge_train_github.GitHubMergeTrainSnapshotReader",
                _UnfinishedStackReader,
            ),
        ):
            state_dir = Path(tmp) / "state"
            _seed_merge_train_policy(state_dir)
            if saved_plan:
                _seed_merge_train_stack_collapse_plan_record(state_dir)
            store = FilesystemRecordStore(state_dir=state_dir)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_merge_train_service_identity()),
                authz_policy=_merge_train_service_policy(),
                record_store_factory=lambda: store,
            )
            results = []
            for _ in range(12):
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
                results.append(response.json()["result"])
                if results[-1]["controller_action"] == "land_batch":
                    break
            self.assertEqual(results[0]["controller_action"], "plan_candidate", results)
            feedback = build_feedback_payloads(response={"result": results[0]})
            held = next(item for item in feedback if item["pull_request_number"] == 1)
            self.assertEqual(held["event"], "blocked")
            self.assertIn("#2", str(held["message"]))
            self.assertEqual(results[0]["dry_run_result"]["queue_order"], [3])
            self.assertEqual(results[-1]["controller_action"], "land_batch", results)
            self.assertEqual(
                [
                    (entry["pull_request_number"], entry["status"])
                    for entry in results[-1]["landing_plan"]["entries"]
                ],
                [(3, "merged")],
            )


class UnfinishedStackSelectionTests(unittest.TestCase):
    def test_child_readiness_restores_combination_without_a_new_root_head(self) -> None:
        snapshot = _UnfinishedStackReader(transport=object()).read_merge_train_snapshot(
            repository="cbusillo/sellyouroutboard", base_branch="main"
        )
        policy = build_test_merge_train_policy()
        held = build_merge_train_dry_run_result(
            policy=policy, snapshot=snapshot, batch_landing=True, new_candidate=True
        )
        self.assertEqual(held.queue_order, (3,))
        ready_snapshot = snapshot.model_copy(
            update={
                "pull_requests": (
                    snapshot.pull_requests[0],
                    snapshot.pull_requests[1].model_copy(
                        update={
                            "is_draft": False,
                            "labels": snapshot.pull_requests[0].labels,
                            "label_actors": snapshot.pull_requests[0].label_actors,
                        }
                    ),
                    snapshot.pull_requests[2],
                )
            }
        )
        ready = build_merge_train_dry_run_result(
            policy=policy, snapshot=ready_snapshot, batch_landing=True, new_candidate=True
        )
        self.assertEqual(ready.queue_order, (1, 3))
        discovery = discover_merge_train_stack(
            policy=policy, snapshot=ready_snapshot, root_pull_request_number=1
        )
        self.assertEqual(discovery.status, "ready_for_collapse")
        self.assertEqual(
            ready.selected_pr.head_sha if ready.selected_pr else None,
            snapshot.pull_requests[0].head_sha,
        )

    def test_unready_children_defer_only_their_root(self) -> None:
        snapshot = _FakeStackedMergeTrainSnapshotReader(
            transport=object()
        ).read_merge_train_snapshot(repository="cbusillo/sellyouroutboard", base_branch="main")
        independent = (
            _UnfinishedStackReader(transport=object())
            .read_merge_train_snapshot(
                repository=snapshot.repository, base_branch=snapshot.base_branch
            )
            .pull_requests[2]
        )
        policy = build_test_merge_train_policy()
        repository_policy = policy.find_repository_policy(
            repository=snapshot.repository, base_branch=snapshot.base_branch
        )
        child_updates = (
            {"is_draft": True},
            {"labels": ()},
            {"labels": (repository_policy.enqueue_label, repository_policy.blocked_label)},
            {"head_repository": "example/fork"},
        )
        for update in child_updates:
            with self.subTest(update=update):
                current = snapshot.model_copy(
                    update={
                        "pull_requests": (
                            snapshot.pull_requests[0],
                            snapshot.pull_requests[1].model_copy(update=update),
                            independent,
                        )
                    }
                )
                result = build_merge_train_dry_run_result(
                    policy=policy, snapshot=current, batch_landing=True, new_candidate=True
                )
                self.assertEqual(result.queue_order, (3,))
                self.assertEqual(result.intended_next_action, "merge")
                self.assertTrue(result.queue[0].deferred_reasons)

    def test_existing_candidate_recheck_does_not_defer_a_root_for_its_child(self) -> None:
        snapshot = _UnfinishedStackReader(transport=object()).read_merge_train_snapshot(
            repository="cbusillo/sellyouroutboard", base_branch="main"
        )
        result = build_merge_train_dry_run_result(
            policy=build_test_merge_train_policy(), snapshot=snapshot, batch_landing=True
        )
        self.assertEqual(result.queue_order, (1, 3))

    def test_root_failure_is_reported_before_its_unfinished_child(self) -> None:
        snapshot = _UnfinishedStackReader(transport=object()).read_merge_train_snapshot(
            repository="cbusillo/sellyouroutboard", base_branch="main"
        )
        for update in ({"required_checks_status": "fail"}, {"mergeable": "conflicting"}):
            with self.subTest(update=update):
                failed_root = snapshot.model_copy(
                    update={
                        "pull_requests": (
                            snapshot.pull_requests[0].model_copy(update=update),
                            *snapshot.pull_requests[1:],
                        )
                    }
                )
                result = build_merge_train_dry_run_result(
                    policy=build_test_merge_train_policy(),
                    snapshot=failed_root,
                    batch_landing=True,
                    new_candidate=True,
                )
                self.assertEqual(result.intended_next_action, "block")
                self.assertEqual(result.selected_pr.number if result.selected_pr else None, 1)
