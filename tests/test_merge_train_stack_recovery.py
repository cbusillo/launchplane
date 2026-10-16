from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Callable
import unittest
from urllib.parse import unquote

from control_plane.contracts.merge_train_stack_collapse import (
    MergeTrainStackChildNotReadyError,
    MergeTrainStackCollapsePlan,
    build_merge_train_stack_collapse_plan_record,
    execute_merge_train_stack_collapse_plan,
)
from control_plane.merge_train import build_merge_train_dry_run_result
from control_plane.merge_train_github import (
    GitHubMergeTrainClient,
    GitHubMergeTrainSnapshotReader,
    MergeTrainGitHubStaleHeadError,
)
from control_plane.storage.filesystem import FilesystemRecordStore
from tests.test_merge_train_handoff import _comment, _snapshot, _stack_fixture


class _Provider:
    """A mutable provider graph; construction and feature effects use the real adapter."""

    def __init__(self, plan: MergeTrainStackCollapsePlan) -> None:
        self.heads = {entry.head_ref: entry.head_sha for entry in plan.entries}
        self.refs = {entry.pull_request_number: entry.head_ref for entry in plan.entries}
        self.commits: dict[str, dict[str, Any]] = {
            entry.head_sha: {
                "sha": entry.head_sha,
                "tree": {"sha": f"tree:{entry.head_sha}"},
                "parents": [],
                "message": "source",
            }
            for entry in plan.entries
        }
        self.feature_effects: list[str] = []
        self.constructions: list[str] = []
        self.handoffs = {entry.pull_request_number: entry.head_sha for entry in plan.entries}

    def contains(self, head: str, child: str) -> bool:
        return head == child or any(
            self.contains(parent["sha"], child) for parent in self.commits[head]["parents"]
        )

    def request(self, *, method: str, path: str, body: dict[str, object] | None = None) -> object:
        if method == "GET" and "/branches/" in path:
            return {"commit": {"sha": self.heads[unquote(path.split("/branches/")[1])]}}
        if method == "GET" and "/compare/" in path:
            child, head = unquote(path.split("/compare/")[1]).split("...")
            return {"status": "ahead" if self.contains(head, child) else "diverged"}
        if method == "GET" and "/issues/" in path:
            number = int(path.split("/issues/")[1].split("/")[0])
            return [
                _comment(
                    f"Ready for the merge train\nHead: {self.handoffs[number]}",
                    user={"id": 20, "login": "example"},
                )
            ]
        if method == "GET" and "/pulls/" in path:
            number = int(path.rsplit("/", 1)[1])
            return {"head": {"sha": self.heads[self.refs[number]]}}
        if method == "GET" and "/git/commits/" in path:
            return self.commits[path.rsplit("/", 1)[1]]
        if method == "GET" and "/commits/" in path:
            commit = self.commits[path.rsplit("/", 1)[1]]
            return {"sha": commit["sha"], "commit": commit, "parents": commit["parents"]}
        if method == "POST" and path.endswith("/git/refs"):
            assert body is not None
            self.heads[str(body["ref"]).removeprefix("refs/heads/")] = str(body["sha"])
            return {}
        if method == "DELETE" and "/git/refs/heads/" in path:
            del self.heads[unquote(path.split("/git/refs/heads/")[1])]
            return None
        if method == "POST" and path.endswith("/merges"):
            assert body is not None
            ref, child = str(body["base"]), str(body["head"])
            parent = self.heads[ref]
            if self.contains(parent, child):
                return None
            tree = f"tree:{parent}:{child}"
            result = sha256(f"{ref}:{parent}:{child}".encode()).hexdigest()[:40]
            self.commits[result] = {
                "sha": result,
                "tree": {"sha": tree},
                "message": body["commit_message"],
                "parents": [{"sha": parent}, {"sha": child}],
            }
            self.heads[ref] = result
            (self.feature_effects if ref in self.refs.values() else self.constructions).append(
                result
            )
            return {"sha": result}
        raise AssertionError(f"Unexpected provider request: {method} {path}")


class StackRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.reset_case()

    def reset_case(self) -> None:
        self.policy, self.plan, self.members = _stack_fixture()
        self.provider = _Provider(self.plan)
        self.client = GitHubMergeTrainClient(transport=self.provider)
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = FilesystemRecordStore(Path(self.directory.name))
        self.store.write_merge_train_policy_record(self.policy)
        self.saved = self.plan

    def checkpoint(self, progress: MergeTrainStackCollapsePlan) -> None:
        record = build_merge_train_stack_collapse_plan_record(
            plan=progress,
            source="test:recovery",
            updated_at=progress.updated_at,
        )
        self.store.write_merge_train_stack_collapse_plan_record(record)
        # Reload through persisted serialization, rather than retaining the caller's object.
        self.saved = next(
            item.plan
            for item in self.store.list_merge_train_stack_collapse_plan_records()
            if item.record_id == record.record_id
        )

    def execute(
        self,
        checkpoint: Callable[[MergeTrainStackCollapsePlan], None] | None = None,
        readiness: Callable[[int], tuple[str, ...]] = lambda number: (),
    ) -> MergeTrainStackCollapsePlan:
        return execute_merge_train_stack_collapse_plan(
            plan=self.saved,
            branch_client=self.client,
            child_readiness_reasons=readiness,
            updated_at=self.plan.updated_at,
            checkpoint=checkpoint or self.checkpoint,
        )

    def crash_after_merge(self) -> None:
        def crash(progress: MergeTrainStackCollapsePlan) -> None:
            if progress.mutations[0].status == "mutated":
                raise RuntimeError("process lost before checkpoint")
            self.checkpoint(progress)

        with self.assertRaisesRegex(RuntimeError, "process lost"):
            self.execute(crash)
        self.assertEqual(self.saved.mutations[0].status, "planned")
        self.assertTrue(self.saved.mutations[0].expected_merge_tree_sha)

    def handoff(self) -> str:
        root = self.members[0]
        current = self.provider.heads[root.head_ref]
        reader = GitHubMergeTrainSnapshotReader(
            transport=self.provider,
            repository_policy=self.policy.policy.policies[0],
            branch_refresh_store=self.store,
        )
        handoff = reader._read_ready_handoff(
            repository_path=self.plan.repository,
            number=root.number,
            head_sha=current,
            base_branch="main",
            base_sha="f" * 40,
        )
        if handoff.status == "ready":
            result = build_merge_train_dry_run_result(
                policy=self.policy.policy,
                snapshot=_snapshot(
                    root.model_copy(update={"head_sha": current, "ready_handoff": handoff})
                ),
                batch_landing=True,
            )
            self.assertTrue(result.queue_order)
        return handoff.status

    def test_saved_tree_recovers_provider_effect_and_original_handoff_without_remerge(self) -> None:
        self.crash_after_merge()
        before = self.store.list_merge_train_stack_collapse_plan_records()
        self.assertEqual(self.handoff(), "ready")
        self.assertEqual(self.store.list_merge_train_stack_collapse_plan_records(), before)
        effects, constructions = (
            list(self.provider.feature_effects),
            list(self.provider.constructions),
        )
        result = self.execute()
        self.assertEqual(result.status, "waiting_for_root_checks")
        self.assertEqual(self.provider.feature_effects, effects)
        self.assertEqual(self.provider.constructions, constructions)
        self.assertEqual(self.handoff(), "ready")

    def test_saved_tree_plan_outranks_later_unprepared_checkpoint(self) -> None:
        from control_plane.workflows.merge_train_controller import (
            latest_merge_train_stack_collapse_progress_record,
        )

        self.crash_after_merge()
        unprepared = self.saved.model_copy(
            update={
                "updated_at": "2026-10-16T11:00:00Z",
                "mutations": (
                    self.saved.mutations[0].model_copy(update={"expected_merge_tree_sha": ""}),
                ),
            }
        )
        self.store.write_merge_train_stack_collapse_plan_record(
            build_merge_train_stack_collapse_plan_record(
                plan=unprepared,
                source="test:older-progress",
                updated_at=unprepared.updated_at,
            )
        )
        selected = latest_merge_train_stack_collapse_progress_record(
            self.store.list_merge_train_stack_collapse_plan_records()
        )
        assert selected is not None
        self.assertEqual(
            selected.plan.mutations[0].expected_merge_tree_sha,
            self.saved.mutations[0].expected_merge_tree_sha,
        )
        self.assertEqual(self.handoff(), "ready")

    def test_planned_write_failure_prevents_feature_effect(self) -> None:
        def failed_write(progress: MergeTrainStackCollapsePlan) -> None:
            if progress.mutations[0].expected_merge_tree_sha:
                raise RuntimeError("storage failed")
            self.checkpoint(progress)

        with self.assertRaisesRegex(RuntimeError, "storage failed"):
            self.execute(failed_write)
        self.assertFalse(self.provider.feature_effects)
        self.assertEqual(
            self.provider.heads[self.plan.root_head_ref], self.plan.root_initial_head_sha
        )
        self.assertEqual(set(self.provider.heads), set(self.provider.refs.values()))

    def test_saved_plan_before_effect_resumes_without_reconstruction(self) -> None:
        def crash(progress: MergeTrainStackCollapsePlan) -> None:
            self.checkpoint(progress)
            if progress.mutations[0].expected_merge_tree_sha:
                raise RuntimeError("crash before effect")

        with self.assertRaisesRegex(RuntimeError, "crash before effect"):
            self.execute(crash)
        constructions = list(self.provider.constructions)
        self.assertFalse(self.provider.feature_effects)
        self.assertEqual(self.execute().status, "waiting_for_root_checks")
        self.assertEqual(self.provider.constructions, constructions)

    def test_forged_tree_and_changed_child_cannot_recover_or_carry_handoff(self) -> None:
        for change in ("tree", "child", "parents", "message", "missing_plan"):
            with self.subTest(change=change):
                # Each case starts with its own saved pre-effect plan and real provider effect.
                self.reset_case()
                self.crash_after_merge()
                commit = self.provider.commits[self.provider.heads[self.plan.root_head_ref]]
                if change == "tree":
                    commit["tree"] = {"sha": "forged-tree"}
                elif change == "child":
                    self.provider.heads[self.members[1].head_ref] = "changed-child"
                elif change == "parents":
                    commit["parents"].reverse()
                elif change == "message":
                    commit["message"] = "unrelated merge"
                else:
                    self.store = FilesystemRecordStore(Path(self.directory.name) / "legacy")
                    self.store.write_merge_train_policy_record(self.policy)
                    self.checkpoint(
                        self.saved.model_copy(
                            update={
                                "mutations": (
                                    self.saved.mutations[0].model_copy(
                                        update={"expected_merge_tree_sha": ""}
                                    ),
                                )
                            }
                        )
                    )
                self.assertEqual(self.handoff(), "stale")
                effects = list(self.provider.feature_effects)
                with self.assertRaises(MergeTrainGitHubStaleHeadError):
                    self.execute()
                self.assertEqual(self.provider.feature_effects, effects)
                self.assertEqual(self.saved.mutations[0].status, "planned")

    def test_held_recovered_child_never_checkpoints_mutated(self) -> None:
        self.crash_after_merge()
        with self.assertRaises(MergeTrainStackChildNotReadyError):
            self.execute(readiness=lambda number: ("child held",))
        self.assertEqual(self.saved.mutations[0].status, "planned")
