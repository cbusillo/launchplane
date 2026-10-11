from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from typing import Any
from unittest.mock import patch

from fastapi import FastAPI

from control_plane.contracts.merge_train_batch import (
    MergeTrainBatchHeldOutEntry,
    dependency_not_ready_holds,
)
from control_plane.contracts.merge_train_policy import MergeTrainPolicyRecord
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.merge_train import MergeTrainDryRunSnapshot, build_merge_train_dry_run_result
from control_plane.merge_train_controller_feedback import build_feedback_payloads
from control_plane.merge_train_controller_run_once import _surviving_held_out_entries
from control_plane.storage.filesystem import FilesystemRecordStore
from tests.http_app_test_support import _post_merge_train_controller_run_once
from tests.merge_train_policy_fixtures import build_test_merge_train_policy_record
from tests.support.auth import _StubVerifier
from tests.support.merge_train import (
    _FakeCollapsedRootStackedMergeTrainSnapshotReader,
    _FakeExpandedMergeTrainSnapshotReader,
    _FakeFailingMergeTrainGitHubClient,
    _FakeMergeTrainGitHubClient,
    _FakeStackedMergeTrainSnapshotReader,
    _merge_train_service_identity,
    _merge_train_service_policy,
    _seed_merge_train_policy,
)


class DependencyRequalificationTests(unittest.IsolatedAsyncioTestCase):
    """An older dependency head that settles mid-candidate waits for the next base (#3259)."""

    def test_client_notes_refusal_is_not_a_transient_dependency_hold(self) -> None:
        snapshot = _FakeExpandedMergeTrainSnapshotReader(
            transport=object()
        ).read_merge_train_snapshot(repository="cbusillo/sellyouroutboard", base_branch="main")
        dependency, green = snapshot.pull_requests
        notes_error = "Client test notes are missing"
        for mergeable in ("mergeable", "unknown", "conflicting"):
            with self.subTest(mergeable=mergeable):
                refused = dependency.model_copy(
                    update={
                        "labels": (),
                        "label_actors": (),
                        "actor_id": 42,
                        "actor_role": "trusted_automation",
                        "dependency_update_class": "patch_or_minor",
                        "mergeable": mergeable,
                        "required_checks_status": "fail",
                        "client_test_notes_error": notes_error,
                    }
                )
                result = build_merge_train_dry_run_result(
                    policy=_dependency_policy_record().policy,
                    snapshot=snapshot.model_copy(update={"pull_requests": (refused, green)}),
                    batch_landing=True,
                )
                self.assertEqual(result.queue[0].ineligible_reasons, (notes_error,))
                self.assertEqual(dependency_not_ready_holds(result, snapshot.base_sha), ())

    async def test_settled_dependency_waits_instead_of_replacing_the_candidate(self) -> None:
        for settled in ("mergeable", "rerun_passed"):
            for position in ("ahead", "behind"):
                with self.subTest(settled=settled, position=position):
                    await self._assert_candidate_kept(settled=settled, position=position)

    async def test_changed_dependency_head_waits_for_the_next_candidate_without_a_hold(
        self,
    ) -> None:
        state = _DependencyState(mergeable="unknown")
        async with _controller(state) as (app, store, request):
            planned = await _post(app, request, "plan_candidate")
            self.assertEqual(_entries(planned), [2])
            await _post(app, request, "build_candidate")

            state.mergeable = "mergeable"
            state.head_sha = "dependency-head-2"
            for action in ("observe_candidate", "plan_landing", "land_batch"):
                await _post(app, request, action)
            self.assertEqual(len(_batch_ids(store)), 1)

            state.landed = True
            reentered = await _post(app, request, "plan_candidate")
            self.assertEqual(_entries(reentered), [1])
            self.assertEqual(reentered["candidate"]["held_out"], [])

    async def test_conflicting_dependency_stays_out_and_the_batch_lands(self) -> None:
        state = _DependencyState(mergeable="unknown")
        async with _controller(state) as (app, store, request):
            await _post(app, request, "plan_candidate")
            await _post(app, request, "build_candidate")
            state.mergeable = "conflicting"
            for action in ("observe_candidate", "plan_landing", "land_batch"):
                await _post(app, request, action)
            self.assertEqual(len(_batch_ids(store)), 1)

            state.landed = True
            idle = await _post(app, request, "idle")
            self.assertNotIn("candidate", idle)

    async def test_settled_dependency_does_not_replace_a_failed_candidate_on_recovery(self) -> None:
        state = _DependencyState(mergeable="unknown")
        async with _controller(state) as (app, store, request):
            await _post(app, request, "plan_candidate")
            await _post(app, request, "build_candidate")
            with patch(
                "control_plane.merge_train_controller_run_once.GitHubMergeTrainClient",
                _FakeFailingMergeTrainGitHubClient,
            ):
                failed = await _post(app, request, "observe_candidate")
                self.assertEqual(failed["candidate"]["status"], "failed")

            state.mergeable = "mergeable"
            recovered = await _post(app, request, "observe_candidate")
            self.assertEqual(_entries(recovered), [2])
            self.assertEqual(recovered["candidate"]["status"], "passed")
            self.assertEqual(len(_batch_ids(store)), 1)
            for action in ("plan_landing", "land_batch"):
                await _post(app, request, action)

    async def test_collapsed_root_candidate_records_the_dependency_hold(self) -> None:
        state = _DependencyState(mergeable="unknown")
        collapsed = False

        class Reader(_FakeStackedMergeTrainSnapshotReader):
            def read_merge_train_snapshot(
                self, *, repository: str, base_branch: str
            ) -> MergeTrainDryRunSnapshot:
                stacked = (
                    _FakeCollapsedRootStackedMergeTrainSnapshotReader
                    if collapsed
                    else _FakeStackedMergeTrainSnapshotReader
                )
                snapshot = stacked(transport=self.transport).read_merge_train_snapshot(
                    repository=repository, base_branch=base_branch
                )
                root = next(pr for pr in snapshot.pull_requests if pr.number == 1)
                dependency = root.model_copy(
                    update={
                        "number": 9,
                        "url": f"https://github.com/{repository}/pull/9",
                        "head_ref": "dependabot/patch",
                        "labels": (),
                        "label_actors": (),
                        "actor_id": 42,
                        "actor_role": "trusted_automation",
                        "dependency_update_class": "patch_or_minor",
                        "created_at": "2026-05-01T00:00:00Z",
                        "head_sha": state.head_sha,
                        "mergeable": state.mergeable,
                    }
                )
                return snapshot.model_copy(
                    update={"pull_requests": (dependency, *snapshot.pull_requests)}
                )

        async with _controller(state, Reader) as (app, _, request):
            await _post(app, request, "plan_stack_collapse")
            await _post(app, request, "execute_stack_collapse")
            collapsed = True
            admitted = await _post(app, request, "admit_collapsed_root")
        self.assertEqual(_entries(admitted), [1])
        self.assertEqual(
            [
                (hold["pull_request_number"], hold["reason"])
                for hold in admitted["candidate"]["held_out"]
            ],
            [(9, "dependency_not_ready")],
        )

    def test_unsettled_dependency_hold_lasts_until_its_head_or_base_moves(self) -> None:
        snapshot = _FakeExpandedMergeTrainSnapshotReader(
            transport=object()
        ).read_merge_train_snapshot(repository="cbusillo/sellyouroutboard", base_branch="main")
        dependency, green = snapshot.pull_requests
        dependency = dependency.model_copy(
            update={
                "labels": (),
                "label_actors": (),
                "actor_id": 42,
                "actor_role": "trusted_automation",
                "dependency_update_class": "patch_or_minor",
                "mergeable": "unknown",
            }
        )
        hold = MergeTrainBatchHeldOutEntry(
            pull_request_number=dependency.number,
            head_sha=dependency.head_sha,
            reason="dependency_not_ready",
            probe_base_sha=snapshot.base_sha,
        )
        policy = _dependency_policy_record().policy
        cases = (
            ("unsettled", dependency, snapshot.base_sha, (hold,)),
            ("settled", dependency.model_copy(update={"mergeable": "mergeable"}), None, (hold,)),
            ("new_head", dependency.model_copy(update={"head_sha": "new-head"}), None, ()),
            ("moved_base", dependency, "moved-base", ()),
        )
        for case, pull_request, base_sha, expected in cases:
            with self.subTest(case=case):
                current = snapshot.model_copy(
                    update={
                        "base_sha": base_sha or snapshot.base_sha,
                        "pull_requests": (pull_request, green),
                    }
                )
                self.assertEqual(
                    _surviving_held_out_entries(
                        policy=policy, snapshot=current, held_out=(hold,), batch_landing=True
                    ),
                    expected,
                )

    async def _assert_candidate_kept(self, *, settled: str, position: str) -> None:
        state = _DependencyState(
            mergeable="unknown" if settled == "mergeable" else "mergeable",
            checks="pass" if settled == "mergeable" else "fail",
        )
        if position == "behind":
            state.created_at = "2026-05-08T10:10:00Z"
        async with _controller(state) as (app, store, request):
            planned_response = await _post(app, request, "plan_candidate", full=True)
            planned = planned_response["result"]
            self.assertEqual(_entries(planned), [2])
            self.assertEqual(
                [
                    (hold["pull_request_number"], hold["head_sha"], hold["reason"])
                    for hold in planned["candidate"]["held_out"]
                ],
                [(1, "dependency-head-1", "dependency_not_ready")],
            )
            # The author has nothing to do; the head simply waits for the next base.
            self.assertNotIn(
                1,
                [
                    payload["pull_request_number"]
                    for payload in build_feedback_payloads(response=planned_response)
                ],
            )
            await _post(app, request, "build_candidate")

            state.mergeable = "mergeable"
            state.checks = "pass"
            for action in ("observe_candidate", "plan_landing", "land_batch"):
                landed = await _post(app, request, action, full=True)
            self.assertEqual(len(_batch_ids(store)), 1)
            landing = next(
                record
                for record in store.list_merge_train_batch_landing_plan_records()
                if record.record_id == landed["records"]["merge_train_batch_landing_plan_record_id"]
            )
            self.assertEqual(
                [
                    (entry.pull_request_number, entry.status)
                    for entry in landing.landing_plan.entries
                ],
                [(2, "merged")],
            )

            state.landed = True
            reentered = await _post(app, request, "plan_candidate")
            self.assertEqual(_entries(reentered), [1])
            self.assertEqual(reentered["candidate"]["held_out"], [])


class _DependencyState:
    def __init__(self, *, mergeable: str, checks: str = "pass") -> None:
        self.mergeable = mergeable
        self.checks = checks
        self.head_sha = "dependency-head-1"
        self.created_at = "2026-05-08T10:00:00Z"
        self.landed = False


@asynccontextmanager
async def _controller(
    state: _DependencyState, reader: type | None = None
) -> AsyncIterator[tuple[FastAPI, FilesystemRecordStore, dict[str, str]]]:
    class Reader(_FakeExpandedMergeTrainSnapshotReader):
        def read_merge_train_snapshot(
            self, *, repository: str, base_branch: str
        ) -> MergeTrainDryRunSnapshot:
            snapshot = super().read_merge_train_snapshot(
                repository=repository, base_branch=base_branch
            )
            dependency, green = snapshot.pull_requests
            dependency = dependency.model_copy(
                update={
                    "labels": (),
                    "label_actors": (),
                    "actor_id": 42,
                    "actor_role": "trusted_automation",
                    "dependency_update_class": "patch_or_minor",
                    "head_sha": state.head_sha,
                    "created_at": state.created_at,
                    "mergeable": state.mergeable,
                    "required_checks_status": state.checks,
                }
            )
            if state.landed:
                return snapshot.model_copy(
                    update={"base_sha": "base-after-landing", "pull_requests": (dependency,)}
                )
            return snapshot.model_copy(update={"pull_requests": (dependency, green)})

    with (
        TemporaryDirectory() as directory,
        patch("control_plane.http_app.resolve_merge_train_github_token", return_value="token"),
        patch("control_plane.merge_train_github.GitHubMergeTrainSnapshotReader", reader or Reader),
        patch(
            "control_plane.merge_train_controller_run_once.GitHubMergeTrainClient",
            _FakeMergeTrainGitHubClient,
        ),
    ):
        state_dir = Path(directory) / "state"
        record = _dependency_policy_record()
        repository_policy = record.policy.policies[0]
        _seed_merge_train_policy(state_dir, policy=record)
        store = FilesystemRecordStore(state_dir)
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_merge_train_service_identity()),
            authz_policy=_merge_train_service_policy(),
            record_store_factory=lambda: store,
        )
        yield app, store, {"repository": repository_policy.repository, "base_branch": "main"}


def _dependency_policy_record() -> MergeTrainPolicyRecord:
    record = build_test_merge_train_policy_record()
    repository_policy = record.policy.policies[0]
    repository_policy = repository_policy.model_copy(
        update={
            "enqueue": repository_policy.enqueue.model_copy(
                update={
                    "dependency_update_github_user_ids": (42,),
                    "trusted_automation_github_user_ids": (42,),
                }
            ),
        }
    )
    return MergeTrainPolicyRecord(
        record_id=record.record_id,
        source=record.source,
        updated_at=record.updated_at,
        policy=record.policy.model_copy(update={"policies": (repository_policy,)}),
    )


async def _post(
    app: FastAPI, request: dict[str, str], action: str, *, full: bool = False
) -> dict[str, Any]:
    response = await _post_merge_train_controller_run_once(app, {**request, "mutate": True})
    assert response.status_code == 202, response.text
    payload: dict[str, Any] = response.json()
    assert payload["result"]["controller_action"] == action, payload["result"]
    result: dict[str, Any] = payload["result"]
    return payload if full else result


def _entries(result: dict[str, Any]) -> list[int]:
    return [entry["pull_request_number"] for entry in result["candidate"]["entries"]]


def _batch_ids(store: FilesystemRecordStore) -> set[str]:
    return {
        record.candidate.batch_id for record in store.list_merge_train_batch_candidate_records()
    }
