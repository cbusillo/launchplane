from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
import unittest
from unittest.mock import patch

from control_plane.contracts.merge_train_stack_collapse import (
    build_merge_train_stack_collapse_plan_record,
)
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.merge_train import MergeTrainDryRunSnapshot, MergeTrainPullRequestSnapshot
from control_plane.merge_train_github import MergeTrainGitHubStaleHeadError
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.workflows.merge_train_controller import (
    decide_merge_train_controller_record_action,
)
from tests.http_app_test_support import _post_merge_train_controller_run_once
from tests.support.auth import _StubVerifier
from tests.support.merge_train import (
    _FakeCollapsedRootStackedMergeTrainSnapshotReader,
    _FakeMergeTrainGitHubClient,
    _merge_train_service_identity,
    _merge_train_service_policy,
    _seed_executed_merge_train_stack_collapse_plan_record,
    _seed_merge_train_policy,
)
from tests.test_merge_train_saved_collapse_ordering import _other_stack


class ObsoleteCollapseTests(unittest.IsolatedAsyncioTestCase):
    async def _obsolete_case(
        self,
        *,
        reason: str,
        mutate: bool,
        waiting: bool = False,
        status: str = "planned",
        reopen: bool = False,
    ) -> None:
        probes: list[int] = []
        merges: list[int] = []
        root_visible = reason != "root_missing_from_open_snapshot"

        class Reader(_FakeCollapsedRootStackedMergeTrainSnapshotReader):
            def read_merge_train_snapshot(
                self, *, repository: str, base_branch: str
            ) -> MergeTrainDryRunSnapshot:
                snapshot = super().read_merge_train_snapshot(
                    repository=repository, base_branch=base_branch
                )
                root, child = snapshot.pull_requests
                other_root = root.model_copy(
                    update={
                        "number": 11,
                        "head_ref": "feature/root-10",
                        "head_sha": "unrelated-push"
                        if reason in {"root_moved", "snapshot_lag"}
                        else ("head-root" if reopen else root.head_sha),
                    }
                )
                return snapshot.model_copy(
                    update={
                        "pull_requests": (
                            root.model_copy(update={"required_checks_status": "pending"}),
                            child,
                            *((other_root,) if root_visible else ()),
                            *(
                                tuple(
                                    child.model_copy(
                                        update={
                                            "number": number,
                                            "head_ref": "feature/child-10"
                                            if number == 12
                                            else "feature/leaf-10",
                                            "head_sha": "partial-middle-head"
                                            if number == 12
                                            else "head-leaf",
                                            "base_ref": "feature/root-10"
                                            if number == 12
                                            else "feature/child-10",
                                            "required_checks_status": "pass",
                                        }
                                    )
                                    for number in (12, 13)
                                )
                                if reopen
                                else ()
                            ),
                        )
                    }
                )

        class Client(_FakeMergeTrainGitHubClient):
            def find_stack_child_merge_commit(self, **kwargs: Any) -> str:
                probes.append(kwargs["parent_pull_request_number"])
                if reason == "snapshot_lag" or reopen:
                    return ""
                raise MergeTrainGitHubStaleHeadError(
                    "Parent moved outside stored plan", status_code=409
                )

            def read_pull_request_snapshot(
                self, *, repository: str, pull_request_number: int
            ) -> MergeTrainPullRequestSnapshot:
                snapshot = Reader(transport=self.transport).read_merge_train_snapshot(
                    repository=repository, base_branch="main"
                )
                return next(pr for pr in snapshot.pull_requests if pr.number == pull_request_number)

            def merge_stack_child_into_parent(self, **kwargs: Any) -> str:
                if reopen and kwargs["child_pull_request_number"] == 12:
                    merges.append(12)
                    return super().merge_stack_child_into_parent(**kwargs)
                raise AssertionError("obsolete plan must not merge a child")

        with (
            TemporaryDirectory() as directory,
            patch.dict("os.environ", {"GH_TOKEN": "token"}, clear=True),
        ):
            state_dir = Path(directory) / "state"
            _seed_merge_train_policy(state_dir)
            waiting_id = _seed_executed_merge_train_stack_collapse_plan_record(state_dir)
            store = FilesystemRecordStore(state_dir)
            original = next(
                r
                for r in store.list_merge_train_stack_collapse_plan_records()
                if r.record_id == waiting_id
            )
            obsolete = _other_stack(
                original,
                offset=10,
                status="waiting_for_root_checks" if waiting else status,
                newer=True,
            )
            if status == "collapsing" and not waiting:
                plan = obsolete.plan.model_dump(mode="json")
                middle = plan["entries"][1]
                plan["entries"].append(
                    {
                        **middle,
                        "position": 3,
                        "pull_request_number": 13,
                        "head_ref": "feature/leaf-10",
                        "head_sha": "head-leaf",
                        "base_ref": middle["head_ref"],
                        "base_sha": middle["head_sha"],
                    }
                )
                root_mutation = {
                    **plan["mutations"][0],
                    "status": "planned",
                    "merge_commit_sha": "",
                }
                plan["mutations"] = [
                    {
                        **root_mutation,
                        "child_pull_request_number": 13,
                        "parent_pull_request_number": 12,
                        "parent_head_ref": middle["head_ref"],
                        "expected_parent_head_sha": middle["head_sha"],
                        "child_head_sha": "head-leaf",
                        "status": "mutated",
                        "merge_commit_sha": "partial-middle-head",
                    },
                    root_mutation,
                ]
                plan["child_dispositions"] = []
                obsolete = build_merge_train_stack_collapse_plan_record(
                    plan=obsolete.plan.model_validate(plan),
                    source=obsolete.source,
                    updated_at=obsolete.updated_at,
                )
            if reason == "policy_changed":
                obsolete = obsolete.model_copy(
                    update={"plan": obsolete.plan.model_copy(update={"policy_sha256": "0" * 64})}
                )
            if not waiting:
                older = _other_stack(original, offset=10, status="planned", newer=False)
                store.write_merge_train_stack_collapse_plan_record(older)
            store.write_merge_train_stack_collapse_plan_record(obsolete)
            before = store.list_merge_train_stack_collapse_plan_records()
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_merge_train_service_identity()),
                authz_policy=_merge_train_service_policy(),
                record_store_factory=lambda: store,
            )
            with (
                patch("control_plane.merge_train_github.GitHubMergeTrainSnapshotReader", Reader),
                patch(
                    "control_plane.merge_train_controller_run_once.GitHubMergeTrainClient", Client
                ),
            ):
                for index in range(2):
                    response = await _post_merge_train_controller_run_once(
                        app,
                        {
                            "schema_version": 1,
                            "repository": original.plan.repository,
                            "base_branch": "main",
                            "mutate": True if reopen and index == 0 else mutate,
                        },
                    )
                    if waiting:
                        self.assertEqual(response.status_code, 400, response.text)
                    else:
                        self.assertEqual(response.status_code, 202, response.text)
                        self.assertEqual(
                            response.json()["result"]["controller_action"],
                            "execute_stack_collapse"
                            if reopen and index == 1
                            else "wait_for_root_checks",
                        )
                    if reopen and index == 0:
                        root_visible = True
                        before = store.list_merge_train_stack_collapse_plan_records()
            if reopen:
                self.assertEqual(merges, [12] if mutate else [])
                if not mutate:
                    self.assertEqual(store.list_merge_train_stack_collapse_plan_records(), before)
                return
            if waiting or not mutate or reason == "snapshot_lag":
                self.assertEqual(store.list_merge_train_stack_collapse_plan_records(), before)
            else:
                active = store.list_merge_train_stack_collapse_plan_records(status="active")
                self.assertFalse(
                    any(r.plan.collapse_id == obsolete.plan.collapse_id for r in active)
                )
                retired = store.list_merge_train_stack_collapse_plan_records(status="superseded")
                saved = next(r for r in retired if r.record_id == obsolete.record_id)
                self.assertEqual(saved.plan, obsolete.plan)
                self.assertIn(reason, saved.source)
            self.assertFalse(store.list_merge_train_batch_candidate_records())
            self.assertEqual(
                probes,
                [11, 11]
                if reason == "snapshot_lag"
                else (([11] if mutate else [11, 11]) if reason == "root_moved" else []),
            )

    async def test_obsolete_execution_is_retired_without_repeated_probes(self) -> None:
        for reason in ("root_moved", "policy_changed", "root_missing_from_open_snapshot"):
            for mutate in (False, True):
                for status in ("planned", "collapsing"):
                    with self.subTest(reason=reason, mutate=mutate, status=status):
                        await self._obsolete_case(reason=reason, mutate=mutate, status=status)

    async def test_contradictory_snapshot_and_probe_leave_execution_recoverable(self) -> None:
        for mutate in (False, True):
            with self.subTest(mutate=mutate):
                await self._obsolete_case(reason="snapshot_lag", mutate=mutate, status="collapsing")

    async def test_reopened_root_resumes_retired_partial_execution(self) -> None:
        for mutate in (False, True):
            with self.subTest(mutate=mutate):
                await self._obsolete_case(
                    reason="root_missing_from_open_snapshot",
                    mutate=mutate,
                    status="collapsing",
                    reopen=True,
                )

    async def test_obsolete_wait_preserves_current_policy_validation(self) -> None:
        for mutate in (False, True):
            with self.subTest(mutate=mutate):
                await self._obsolete_case(reason="policy_changed", mutate=mutate, waiting=True)

    def test_record_projection_prioritizes_execution_across_collapses(self) -> None:
        with TemporaryDirectory() as directory:
            state_dir = Path(directory)
            _seed_merge_train_policy(state_dir)
            record_id = _seed_executed_merge_train_stack_collapse_plan_record(state_dir)
            store = FilesystemRecordStore(state_dir)
            waiting = next(
                r
                for r in store.list_merge_train_stack_collapse_plan_records()
                if r.record_id == record_id
            )
            for status in ("planned", "collapsing"):
                execution = _other_stack(waiting, offset=10, status=status, newer=False)
                completed = _other_stack(waiting, offset=20, status="ready_for_train", newer=True)
                decision = decide_merge_train_controller_record_action(
                    candidate_records=(),
                    landing_plan_records=(),
                    stack_collapse_plan_records=(waiting, execution, completed),
                )
                self.assertEqual(decision.action, "execute_stack_collapse")
                self.assertEqual(decision.stack_collapse_plan_record_id, execution.record_id)
                self.assertIn("record", decision.reason)

    def test_landing_projection_does_not_attach_an_unrelated_wait(self) -> None:
        from tests.test_merge_train_controller import (
            _candidate_record,
            _landing_plan_record,
            _stack_collapse_record,
        )

        candidate = _candidate_record(status="passed", candidate_sha="candidate-sha")
        landing = _landing_plan_record(candidate=candidate.candidate)
        waiting = _stack_collapse_record(status="waiting_for_root_checks")
        unrelated = _other_stack(waiting, offset=10, status="waiting_for_root_checks", newer=True)
        decision = decide_merge_train_controller_record_action(
            candidate_records=(candidate,),
            landing_plan_records=(landing,),
            stack_collapse_plan_records=(waiting, unrelated),
        )
        self.assertEqual(decision.action, "land_batch")
        self.assertEqual(decision.stack_collapse_plan_record_id, waiting.record_id)
