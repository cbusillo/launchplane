from datetime import datetime, timedelta
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
from control_plane.merge_train import MergeTrainDryRunSnapshot, MergeTrainPullRequestSnapshot
from control_plane.merge_train_github import MergeTrainGitHubStaleHeadError
from control_plane.storage.filesystem import FilesystemRecordStore
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


def _other_stack(
    record: MergeTrainStackCollapsePlanRecord, *, offset: int, status: str, newer: bool
) -> MergeTrainStackCollapsePlanRecord:
    timestamp = (
        datetime.fromisoformat(record.updated_at.replace("Z", "+00:00"))
        + timedelta(minutes=offset if newer else -offset)
    ).isoformat()
    root_ref = f"feature/root-{offset}"
    plan = record.plan.model_dump()
    plan.update(
        collapse_id=f"other-collapse-{offset}",
        root_pull_request_number=record.plan.root_pull_request_number + offset,
        root_head_ref=root_ref,
        status=status,
        created_at=timestamp,
        updated_at=timestamp,
    )
    for entry in plan["entries"]:
        entry["pull_request_number"] += offset
        if entry["position"] == 1:
            entry["head_ref"] = root_ref
        else:
            entry["head_ref"] = f"feature/child-{offset}"
            entry["base_ref"] = root_ref
    for mutation in plan["mutations"]:
        mutation["child_pull_request_number"] += offset
        mutation["parent_pull_request_number"] += offset
        mutation["parent_head_ref"] = root_ref
        if status == "planned":
            mutation.update(status="planned", merge_commit_sha="")
    for disposition in plan["child_dispositions"]:
        disposition["pull_request_number"] += offset
        if status == "ready_for_train":
            disposition["status"] = "closed"
    return build_merge_train_stack_collapse_plan_record(
        plan=record.plan.model_validate(plan), source="test:other-stack", updated_at=timestamp
    )


class SavedCollapseOrderingTests(unittest.IsolatedAsyncioTestCase):
    async def _run_case(
        self,
        *,
        other_status: str,
        newer: bool,
        checks: str,
        mutate: bool,
        padding: int = 0,
        obsolete_reason: str = "",
        other_checks: str = "pass",
        root_mergeable: str = "mergeable",
        expected_action: str | None = None,
        shared_ref: bool = False,
    ) -> None:
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
                        "head_sha": "head-root" if other_status == "planned" else root.head_sha,
                        "required_checks_status": other_checks,
                    }
                )
                if obsolete_reason == "root_moved":
                    other_root = other_root.model_copy(update={"head_sha": "unrelated-push"})
                other_child = child.model_copy(
                    update={
                        "number": 12,
                        "head_ref": root.head_ref if shared_ref else "feature/child-10",
                        "base_ref": other_root.head_ref,
                        "required_checks_status": "pass",
                    }
                )
                return snapshot.model_copy(
                    update={
                        "pull_requests": (
                            root.model_copy(
                                update={
                                    "required_checks_status": checks,
                                    "mergeable": root_mergeable,
                                }
                            ),
                            child.model_copy(update={"required_checks_status": "pass"}),
                            other_root,
                            other_child,
                        )
                    }
                )

        merges: list[int] = []

        class Client(_FakeMergeTrainGitHubClient):
            def read_pull_request_snapshot(
                self, *, repository: str, pull_request_number: int
            ) -> MergeTrainPullRequestSnapshot:
                snapshot = Reader(transport=self.transport).read_merge_train_snapshot(
                    repository=repository, base_branch="main"
                )
                return next(pr for pr in snapshot.pull_requests if pr.number == pull_request_number)

            def find_stack_child_merge_commit(self, **kwargs: Any) -> str:
                if obsolete_reason == "root_moved":
                    raise MergeTrainGitHubStaleHeadError(
                        "Stack collapse parent branch moved outside the stored plan.",
                        status_code=409,
                    )
                return super().find_stack_child_merge_commit(**kwargs)

            def merge_stack_child_into_parent(self, **kwargs: Any) -> str:
                child = kwargs["child_pull_request_number"]
                if other_status != "planned" or child != 12:
                    raise AssertionError("checkpointed child must not be merged again")
                merges.append(child)
                return super().merge_stack_child_into_parent(**kwargs)

        with (
            TemporaryDirectory() as directory,
            patch.dict("os.environ", {"GH_TOKEN": "token"}, clear=True),
        ):
            state_dir = Path(directory) / "state"
            _seed_merge_train_policy(state_dir)
            waiting_id = _seed_executed_merge_train_stack_collapse_plan_record(state_dir)
            store = FilesystemRecordStore(state_dir)
            waiting = next(
                record
                for record in store.list_merge_train_stack_collapse_plan_records()
                if record.record_id == waiting_id
            )
            other = _other_stack(waiting, offset=10, status=other_status, newer=newer)
            if shared_ref:
                plan = other.plan.model_dump(mode="json")
                plan["entries"][1]["head_ref"] = waiting.plan.root_head_ref
                other = build_merge_train_stack_collapse_plan_record(
                    plan=other.plan.model_validate(plan),
                    source=other.source,
                    updated_at=other.updated_at,
                )
            if obsolete_reason == "policy_changed":
                other = other.model_copy(
                    update={"plan": other.plan.model_copy(update={"policy_sha256": "0" * 64})}
                )
            store.write_merge_train_stack_collapse_plan_record(other)
            for index in range(padding):
                store.write_merge_train_stack_collapse_plan_record(
                    _other_stack(waiting, offset=20 + index, status="ready_for_train", newer=True)
                )
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
                response = await _post_merge_train_controller_run_once(
                    app,
                    {
                        "schema_version": 1,
                        "repository": waiting.plan.repository,
                        "base_branch": "main",
                        "mutate": mutate,
                    },
                )
            self.assertEqual(response.status_code, 202, response.text)
            result = response.json()["result"]
            expected_action = expected_action or (
                "admit_collapsed_root"
                if other_status == "waiting_for_root_checks" or obsolete_reason
                else "execute_stack_collapse"
            )
            self.assertEqual(result["controller_action"], expected_action)
            if expected_action == "block":
                if obsolete_reason == "policy_changed":
                    self.assertEqual(
                        result["blocking_reason"]["code"],
                        "merge_train_stack_collapse_policy_changed",
                    )
                else:
                    self.assertEqual(result["dry_run_result"]["selected_pr"]["number"], 1)
            if obsolete_reason == "policy_changed" and other_status == "waiting_for_root_checks":
                self.assertEqual(
                    result["blocked_stack_collapse_records"][0]["record_id"], other.record_id
                )
                self.assertIn(other, store.list_merge_train_stack_collapse_plan_records())
            selected = waiting if obsolete_reason else other
            if not mutate and expected_action != "block":
                self.assertEqual(
                    result["merge_train_stack_collapse_plan_record_id"], selected.record_id
                )
                self.assertEqual(store.list_merge_train_stack_collapse_plan_records(), before)
            elif expected_action == "block":
                self.assertFalse(store.list_merge_train_batch_candidate_records())
                self.assertEqual(store.list_merge_train_stack_collapse_plan_records(), before)
            elif other_status == "waiting_for_root_checks" or obsolete_reason:
                candidate = store.list_merge_train_batch_candidate_records()[0].candidate
                self.assertEqual(
                    candidate.entries[0].pull_request_number, selected.plan.root_pull_request_number
                )
                assert candidate.stack_collapse_root is not None
                self.assertEqual(
                    candidate.stack_collapse_root.collapse_record_id, selected.record_id
                )
            else:
                progress = result["stack_collapse_plan"]
                self.assertEqual(progress["collapse_id"], other.plan.collapse_id)
                self.assertEqual(progress["status"], "waiting_for_root_checks")
            self.assertEqual(
                merges, [12] if mutate and other_status == "planned" and not obsolete_reason else []
            )

    async def test_pending_wait_does_not_hide_newer_interrupted_collapse(self) -> None:
        for mutate in (False, True):
            with self.subTest(mutate=mutate):
                await self._run_case(
                    other_status="collapsing", newer=True, checks="pending", mutate=mutate
                )

    async def test_later_wait_and_completed_history_do_not_hide_saved_execution(self) -> None:
        for status in ("planned", "collapsing"):
            for mutate in (False, True):
                with self.subTest(status=status, mutate=mutate):
                    await self._run_case(
                        other_status=status, newer=False, checks="fail", mutate=mutate, padding=30
                    )

    async def test_newer_pending_wait_does_not_hide_older_ready_wait(self) -> None:
        for mutate in (False, True):
            with self.subTest(mutate=mutate):
                await self._run_case(
                    other_status="waiting_for_root_checks",
                    newer=False,
                    checks="pending",
                    mutate=mutate,
                )

    async def test_obsolete_saved_execution_steps_aside_for_ready_wait(self) -> None:
        for reason in ("root_moved", "policy_changed"):
            for mutate in (False, True):
                with self.subTest(reason=reason, mutate=mutate):
                    await self._run_case(
                        other_status="planned",
                        newer=True,
                        checks="pass",
                        mutate=mutate,
                        obsolete_reason=reason,
                    )

    async def test_obsolete_policy_wait_does_not_mask_independent_ready_wait(self) -> None:
        for newer in (False, True):
            for mutate in (False, True):
                with self.subTest(newer=newer, mutate=mutate):
                    await self._run_case(
                        other_status="waiting_for_root_checks",
                        newer=newer,
                        checks="pass",
                        mutate=mutate,
                        obsolete_reason="policy_changed",
                    )

    async def test_current_policy_wait_sharing_obsolete_stack_ref_stays_blocked(self) -> None:
        for mutate in (False, True):
            with self.subTest(mutate=mutate):
                await self._run_case(
                    other_status="waiting_for_root_checks",
                    newer=True,
                    checks="pass",
                    mutate=mutate,
                    obsolete_reason="policy_changed",
                    shared_ref=True,
                    expected_action="block",
                )

    async def test_pending_saved_wait_does_not_mask_blocked_queue_head(self) -> None:
        for checks, mergeable in (("fail", "mergeable"), ("pass", "conflicting")):
            for newer in (False, True):
                for mutate in (False, True):
                    with self.subTest(checks=checks, newer=newer, mutate=mutate):
                        await self._run_case(
                            other_status="waiting_for_root_checks",
                            newer=newer,
                            checks=checks,
                            root_mergeable=mergeable,
                            other_checks="pending",
                            mutate=mutate,
                            expected_action="block",
                        )
