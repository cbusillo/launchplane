from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from urllib.parse import unquote
import unittest
from unittest.mock import Mock, patch

from control_plane.contracts.merge_train_policy import MergeTrainPolicyRecord
from control_plane.contracts.merge_train_stack_collapse import MergeTrainStackCollapsePlanRecord
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.merge_train import MergeTrainDryRunSnapshot, MergeTrainPullRequestSnapshot
from control_plane.merge_train_github import (
    GitHubMergeTrainClient,
    MergeTrainGitHubError,
    MergeTrainGitHubStaleHeadError,
)
from control_plane.storage.filesystem import FilesystemRecordStore
from tests.http_app_test_support import _post_merge_train_controller_run_once
from tests.merge_train_policy_fixtures import build_test_merge_train_policy_record
from tests.support.auth import _StubVerifier
from tests.support.merge_train import (
    _FakeMergeTrainGitHubClient,
    _FakeStackedMergeTrainSnapshotReader,
    _merge_train_service_identity,
    _merge_train_service_policy,
    _seed_merge_train_policy,
    _seed_merge_train_stack_collapse_plan_record,
)


class _StackTransport:
    """A Git graph behind the real adapter, including GitHub's already-merged response."""

    def __init__(self) -> None:
        self.heads = {"feature/root": "head-root", "feature/child": "head-child"}
        self.parents: dict[str, tuple[str, ...]] = {}
        self.messages: dict[str, str] = {}
        self.merge_requests: list[dict[str, object]] = []

    def contains(self, head: str, child: str) -> bool:
        return head == child or any(
            self.contains(parent, child) for parent in self.parents.get(head, ())
        )

    def request(self, *, method: str, path: str, body: dict[str, object] | None = None) -> object:
        if method == "GET" and "/branches/" in path:
            return {"commit": {"sha": self.heads[unquote(path.split("/branches/")[1])]}}
        if method == "GET" and "/compare/" in path:
            child, head = unquote(path.split("/compare/")[1]).split("...")
            return {"status": "ahead" if self.contains(head, child) else "diverged"}
        if method == "GET" and "/commits/" in path:
            head = path.split("/commits/")[1]
            return {
                "commit": {"message": self.messages.get(head, "unrelated root push")},
                "parents": [{"sha": parent} for parent in self.parents.get(head, ())],
            }
        if method == "POST" and path.endswith("/merges"):
            assert body is not None
            self.merge_requests.append(body)
            ref, child_head, message = body["base"], body["head"], body["commit_message"]
            assert isinstance(ref, str) and isinstance(child_head, str) and isinstance(message, str)
            parent = self.heads[ref]
            if self.contains(parent, child_head):
                return None  # GitHub returns 204 with no JSON body.
            head = f"branch-merge-{len(self.merge_requests)}"
            self.parents[head] = (parent, child_head)
            self.messages[head] = message
            self.heads[ref] = head
            return {"sha": head}
        raise AssertionError(f"unexpected provider request: {method} {path}")


class ChangedPolicyRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def _recovery(
        self,
        *,
        checkpointed: bool,
        moved: bool,
        child_changed: bool = False,
        deeper: bool = False,
        indirectly_merged: bool = False,
        annotation_interrupted: bool = False,
        landing_contains_child: bool = True,
        label_configured: bool = True,
    ) -> None:
        graph = _StackTransport()
        if deeper:
            graph.heads["feature/leaf"] = "head-leaf"
        child_held = False
        comments: list[int] = []
        closed: set[int] = set()
        labels: set[int] = set()
        label_attempts: list[int] = []
        close_requests: list[int] = []

        class Reader(_FakeStackedMergeTrainSnapshotReader):
            def read_merge_train_snapshot(
                self, *, repository: str, base_branch: str
            ) -> MergeTrainDryRunSnapshot:
                snapshot = super().read_merge_train_snapshot(
                    repository=repository, base_branch=base_branch
                )
                root, child = snapshot.pull_requests
                if (
                    indirectly_merged
                    and deeper
                    and graph.contains(graph.heads[child.head_ref], "head-leaf")
                ):
                    closed.add(3)
                if indirectly_merged and graph.contains(graph.heads[root.head_ref], child.head_sha):
                    closed.add(2)
                    return snapshot.model_copy(
                        update={
                            "pull_requests": (
                                root.model_copy(
                                    update={
                                        "head_sha": graph.heads[root.head_ref],
                                        "base_sha": snapshot.base_sha,
                                    }
                                ),
                            )
                        }
                    )
                return snapshot.model_copy(
                    update={
                        "pull_requests": (
                            root.model_copy(update={"head_sha": graph.heads[root.head_ref]}),
                            child.model_copy(
                                update={
                                    "head_sha": graph.heads[child.head_ref],
                                    "required_checks_status": "pass",
                                    "is_draft": child_held,
                                }
                            ),
                            *(
                                (
                                    child.model_copy(
                                        update={
                                            "number": 3,
                                            "head_ref": "feature/leaf",
                                            "head_sha": graph.heads["feature/leaf"],
                                            "base_ref": child.head_ref,
                                            "base_sha": "head-child",
                                            "required_checks_status": "pass",
                                        }
                                    ),
                                )
                                if deeper and 3 not in closed
                                else ()
                            ),
                        )
                    }
                )

        class Client(_FakeMergeTrainGitHubClient):
            def land_batch_candidate(self, **kwargs: Any) -> Any:
                landed = super().land_batch_candidate(**kwargs)
                for entry in landed.entries:
                    graph.parents[entry.merge_commit_sha] = (
                        (entry.expected_head_sha,) if landing_contains_child else ()
                    )
                return landed

            def pull_request_has_label(self, **kwargs: Any) -> bool:
                return kwargs["pull_request_number"] in labels

            def add_pull_request_label(self, **kwargs: Any) -> None:
                number = kwargs["pull_request_number"]
                label_attempts.append(number)
                if annotation_interrupted and len(label_attempts) == 1:
                    raise MergeTrainGitHubError("label response failed after comment")
                labels.add(number)

            def find_stack_child_merge_commit(self, **kwargs: Any) -> str:
                return GitHubMergeTrainClient(transport=graph).find_stack_child_merge_commit(
                    **kwargs
                )

            def merge_stack_child_into_parent(self, **kwargs: Any) -> str:
                return GitHubMergeTrainClient(transport=graph).merge_stack_child_into_parent(
                    **kwargs
                )

            def read_pull_request_snapshot(
                self, *, repository: str, pull_request_number: int
            ) -> MergeTrainPullRequestSnapshot:
                return next(
                    pr
                    for pr in Reader(transport=graph)
                    .read_merge_train_snapshot(repository=repository, base_branch="main")
                    .pull_requests
                    if pr.number == pull_request_number
                )

            def branch_contains_commit(
                self, *, repository: str, branch_ref: str, commit_sha: str
            ) -> bool:
                return graph.contains(branch_ref, commit_sha)

            def find_pull_request_comment_url(self, **kwargs: Any) -> str:
                number = kwargs["pull_request_number"]
                return f"https://example.test/comments/{number}" if number in comments else ""

            def comment_pull_request(self, **kwargs: Any) -> str:
                comments.append(kwargs["pull_request_number"])
                return f"https://example.test/comments/{comments[-1]}"

            def pull_request_is_closed(
                self, *, repository: str, pull_request_number: int, expected_head_sha: str
            ) -> bool:
                self.require_disposition_head(pull_request_number, expected_head_sha)
                return pull_request_number in closed

            @staticmethod
            def require_disposition_head(number: int, expected_head_sha: str) -> None:
                if (
                    expected_head_sha
                    != graph.heads["feature/child" if number == 2 else "feature/leaf"]
                ):
                    raise MergeTrainGitHubStaleHeadError("child head moved", status_code=409)

            def close_pull_request(self, **kwargs: Any) -> None:
                close_requests.append(kwargs["pull_request_number"])
                self.require_disposition_head(
                    kwargs["pull_request_number"], kwargs["expected_head_sha"]
                )
                closed.add(kwargs["pull_request_number"])

        class InterruptedStore(FilesystemRecordStore):
            interrupted = False

            def write_merge_train_stack_collapse_plan_record(
                self, record: MergeTrainStackCollapsePlanRecord
            ) -> Path:
                if (
                    not checkpointed
                    and not self.interrupted
                    and any(m.status == "mutated" for m in record.plan.mutations)
                ):
                    self.interrupted = True
                    raise MergeTrainGitHubError("provider succeeded before progress persistence")
                return super().write_merge_train_stack_collapse_plan_record(record)

        with (
            TemporaryDirectory() as directory,
            patch.dict("os.environ", {}, clear=True),
            patch(
                "control_plane.merge_train_github_token.secrets.resolve_context_secret_value",
                return_value="test-private-key",
            ),
            patch(
                "control_plane.merge_train_github_token.mint_merge_train_installation_token",
                return_value=Mock(token="token"),
            ),
        ):
            state_dir = Path(directory) / "state"
            payload = build_test_merge_train_policy_record().model_dump(mode="json")
            target = payload["policy"]["policies"][0]
            target["github_token"] = {
                "github_app": {
                    "app_id": 42,
                    "repository_id": 123,
                    "private_key_context": "example_context",
                }
            }
            target["merge_identity"]["kind"] = "github_app"
            payload["policy_sha256"] = ""
            original_policy = _seed_merge_train_policy(
                state_dir, policy=MergeTrainPolicyRecord.model_validate(payload)
            )
            _seed_merge_train_stack_collapse_plan_record(
                state_dir, policy=original_policy.policy, snapshot_reader=Reader
            )
            store = InterruptedStore(state_dir)
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

                async def run(mutate: bool = True) -> Any:
                    return await _post_merge_train_controller_run_once(
                        app,
                        {
                            "schema_version": 1,
                            "repository": "cbusillo/sellyouroutboard",
                            "base_branch": "main",
                            "mutate": mutate,
                        },
                    )

                first = await run()
                self.assertEqual(first.status_code, 202 if checkpointed else 502, first.text)
                self.assertEqual(len(graph.merge_requests), 2 if checkpointed and deeper else 1)
                original_records = store.list_merge_train_stack_collapse_plan_records()
                self.assertEqual(
                    any(m.status == "mutated" for r in original_records for m in r.plan.mutations),
                    checkpointed,
                )
                if moved:
                    graph.parents["root-push"] = (graph.heads["feature/root"],)
                    graph.heads["feature/root"] = "root-push"
                if child_changed:
                    graph.parents["new-child"] = (graph.heads["feature/child"],)
                    graph.heads["feature/child"] = "new-child"
                changed_policy = original_policy.policy.model_dump(mode="json")
                changed_policy["policies"][0]["blocked_label"] = "current-policy-blocked"
                if not label_configured:
                    changed_policy["policies"][0]["stack_child_disposition_label"] = ""
                current = original_policy.model_validate(
                    {
                        **original_policy.model_dump(mode="json"),
                        "record_id": f"{original_policy.record_id}-changed",
                        "policy": changed_policy,
                        "policy_sha256": "",
                    }
                )
                store.write_merge_train_policy_record(current)
                if indirectly_merged:
                    for _ in range(10):
                        response = await run()
                        if response.status_code == 502 and annotation_interrupted:
                            self.assertEqual(comments, [2])
                            continue
                        self.assertEqual(response.status_code, 202, response.text)
                        result = response.json()["result"]
                        if result["controller_action"] == "land_batch":
                            break
                    self.assertEqual(result["controller_action"], "land_batch")
                    self.assertEqual(closed, {2, 3} if deeper else {2})
                    expected_annotations = (
                        ([2, 3] if deeper else [2])
                        if landing_contains_child and not child_changed
                        else []
                    )
                    self.assertEqual(comments, expected_annotations)
                    self.assertEqual(
                        labels, set(expected_annotations) if label_configured else set()
                    )
                    if not label_configured:
                        self.assertEqual(
                            result["historical_stack_child_label_status"], "not_configured"
                        )
                    self.assertEqual(close_requests, [])
                    self.assertEqual(len(graph.merge_requests), 2 if deeper else 1)
                    # Retry completed cleanup through the same HTTP controller.
                    replay = await run()
                    self.assertEqual(replay.status_code, 202, replay.text)
                    self.assertEqual(comments, expected_annotations)
                    self.assertEqual(
                        labels, set(expected_annotations) if label_configured else set()
                    )
                    self.assertEqual(len(graph.merge_requests), 2 if deeper else 1)
                    self.assertTrue(
                        all(
                            r.candidate.policy_sha256 == current.policy_sha256
                            for r in store.list_merge_train_batch_candidate_records()
                        )
                    )
                    for old in original_records:
                        retained = next(
                            r
                            for r in store.list_merge_train_stack_collapse_plan_records()
                            if r.record_id == old.record_id
                        )
                        self.assertEqual(retained.plan, old.plan)
                    return
                before_dry_run = store.list_merge_train_stack_collapse_plan_records()
                dry_run = await run(False)
                self.assertEqual(dry_run.status_code, 202, dry_run.text)
                self.assertEqual(
                    dry_run.json()["result"]["controller_action"],
                    "plan_stack_collapse" if checkpointed else "resume_reconciliation",
                )
                self.assertEqual(
                    store.list_merge_train_stack_collapse_plan_records(), before_dry_run
                )
                replanned = await run()
                self.assertEqual(replanned.status_code, 202, replanned.text)
                self.assertEqual(
                    replanned.json()["result"]["controller_action"], "plan_stack_collapse"
                )
                new_id = replanned.json()["result"]["merge_train_stack_collapse_plan_record_id"]
                plan_record = next(
                    r
                    for r in store.list_merge_train_stack_collapse_plan_records()
                    if r.record_id == new_id
                )
                self.assertEqual(plan_record.plan.policy_sha256, current.policy_sha256)
                self.assertNotEqual(plan_record.plan.policy_sha256, original_policy.policy_sha256)
                for old in original_records:
                    retained = next(
                        r
                        for r in store.list_merge_train_stack_collapse_plan_records()
                        if r.record_id == old.record_id
                    )
                    self.assertEqual(retained.plan, old.plan)
                    self.assertEqual(retained.status, "superseded")
                # A hold after planning must still prevent execution, even for an included child.
                child_held = True
                held = await run()
                self.assertEqual(held.status_code, 202, held.text)
                self.assertEqual(len(graph.merge_requests), 1)
                child_held = False
                executed = await run()
                self.assertEqual(executed.status_code, 202, executed.text)
                result = executed.json()["result"]
                self.assertEqual(result["controller_action"], "execute_stack_collapse")
                self.assertEqual(len(graph.merge_requests), 2 if child_changed or deeper else 1)
                self.assertEqual(result["stack_collapse_plan"]["status"], "waiting_for_root_checks")
                executed_id = result["merge_train_stack_collapse_plan_record_id"]
                # Drive the existing candidate/landing controller through disposition.
                actions: list[str] = []
                for _ in range(10):
                    response = await run()
                    self.assertEqual(response.status_code, 202, response.text)
                    action = response.json()["result"]["controller_action"]
                    actions.append(action)
                    if closed:
                        break
                self.assertEqual(closed, {2, 3} if deeper else {2}, actions)
                self.assertEqual(comments, [2, 3] if deeper else [2])
                completed = [
                    r
                    for r in store.list_merge_train_stack_collapse_plan_records()
                    if r.plan.collapse_id == plan_record.plan.collapse_id
                    and r.plan.status == "ready_for_train"
                ]
                self.assertTrue(completed, actions)
                self.assertEqual(
                    completed[0].plan.child_dispositions[0].expected_head_sha,
                    graph.heads["feature/child"],
                )
                candidates = store.list_merge_train_batch_candidate_records()
                self.assertTrue(candidates)
                self.assertTrue(
                    all(r.candidate.policy_sha256 == current.policy_sha256 for r in candidates)
                )
                self.assertTrue(
                    any(
                        r.candidate.stack_collapse_root is not None
                        and r.candidate.stack_collapse_root.collapse_record_id == executed_id
                        for r in candidates
                    )
                )

    async def test_missing_checkpoint_then_policy_change_recovers_without_repeating_merge(
        self,
    ) -> None:
        await self._recovery(checkpointed=False, moved=False)

    async def test_missing_checkpoint_then_root_push_and_policy_change_recovers(self) -> None:
        await self._recovery(checkpointed=False, moved=True)

    async def test_checkpointed_merge_then_root_push_and_policy_change_recovers(self) -> None:
        await self._recovery(checkpointed=True, moved=True)

    async def test_new_child_work_is_merged_and_reconciled_at_its_current_head(self) -> None:
        await self._recovery(checkpointed=False, moved=True, child_changed=True)

    async def test_missing_middle_checkpoint_recovers_then_merges_only_remaining_root(
        self,
    ) -> None:
        await self._recovery(checkpointed=False, moved=False, deeper=True)

    async def test_indirectly_merged_child_absent_from_discovery_does_not_block_current_policy_landing(
        self,
    ) -> None:
        await self._recovery(checkpointed=False, moved=False, indirectly_merged=True)

    async def test_checkpointed_indirect_child_receives_landing_annotations(self) -> None:
        await self._recovery(checkpointed=True, moved=True, indirectly_merged=True)

    async def test_missing_middle_checkpoint_recovers_absent_leaf_annotations(self) -> None:
        await self._recovery(checkpointed=False, moved=False, deeper=True, indirectly_merged=True)

    async def test_recorded_middle_progress_recovers_absent_leaf_annotations(self) -> None:
        await self._recovery(checkpointed=True, moved=True, deeper=True, indirectly_merged=True)

    async def test_annotation_retry_does_not_repeat_comment_or_merge(self) -> None:
        await self._recovery(
            checkpointed=False, moved=False, indirectly_merged=True, annotation_interrupted=True
        )

    async def test_historical_annotation_skips_moved_closed_child(self) -> None:
        await self._recovery(
            checkpointed=False, moved=True, indirectly_merged=True, child_changed=True
        )

    async def test_historical_annotation_requires_actual_landing_containment(self) -> None:
        await self._recovery(
            checkpointed=False, moved=False, indirectly_merged=True, landing_contains_child=False
        )

    async def test_removed_label_does_not_stall_landed_root_cleanup(self) -> None:
        await self._recovery(
            checkpointed=False, moved=False, indirectly_merged=True, label_configured=False
        )
