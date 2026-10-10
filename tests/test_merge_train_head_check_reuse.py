from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from urllib.parse import unquote

from control_plane.contracts.merge_train_batch import (
    MergeTrainBatchCandidate,
    build_merge_train_batch_candidate_record,
    build_merge_train_batch_landing_plan,
)
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.merge_train_github import (
    GitHubMergeTrainClient,
    MergeTrainGitHubError,
    MergeTrainGitHubRequest,
    merge_train_construction_ref,
)
from control_plane.workflows.merge_train_controller import (
    decide_merge_train_controller_record_action,
)
from tests.test_merge_train_github import (
    _batch_candidate,
    _git_commit,
    _github_branch,
    _protected_branch_with_checks,
    _required_check_run,
)


class _ReuseTransport:
    def __init__(self) -> None:
        self.requests: list[MergeTrainGitHubRequest] = []
        self.refs: dict[str, str] = {}
        self.base_sha = "base-main"
        self.head_sha = "head-1"
        self.head_tree = "tree-head-1"
        self.candidate_tree = self.head_tree
        self.contains_base = True
        self.check_head = self.head_sha
        self.status_head = self.head_sha
        self.check_conclusion = "success"
        self.check_status = "completed"
        self.check_app = 15368
        self.required_names: tuple[str, ...] = ("ci-gate", "security-gate")
        self.observed_names: tuple[str, ...] = self.required_names
        self.pull_reads = 0
        self.change_on_confirmation = False
        self.change_unbound_fields = False
        self.base_history: list[dict[str, object]] = []
        self.unavailable_path = ""

    def request(self, *, method: str, path: str, body: dict[str, object] | None = None) -> object:
        self.requests.append(MergeTrainGitHubRequest(method=method, path=path, body=body))
        path = unquote(path)
        if self.unavailable_path and self.unavailable_path in path:
            raise MergeTrainGitHubError("unavailable", status_code=403)
        if path == "/graphql":
            history = [
                event
                for event in self.base_history
                if event.get("event")
                in {"base_ref_changed", "base_ref_force_pushed", "automatic_base_change_succeeded"}
            ]
            return {
                "data": {
                    "repository": {
                        "pullRequest": {
                            "timelineItems": {
                                "nodes": history[:1],
                                "pageInfo": {"hasNextPage": len(history) > 1},
                            }
                        }
                    }
                }
            }
        prefix = "/repos/example/merge-train-repo"
        suffix = path.removeprefix(prefix)
        if method == "POST" and suffix == "/git/refs":
            assert body is not None
            self.refs[str(body["ref"]).removeprefix("refs/heads/")] = str(body["sha"])
            return {}
        if method == "DELETE":
            self.refs.pop(suffix.removeprefix("/git/refs/heads/"), None)
            return None
        if suffix == "/merges":
            assert body is not None
            result_sha = "candidate-after-" + str(body["head"]).removeprefix("head-")
            self.refs[str(body["base"])] = result_sha
            return {"sha": result_sha}
        if suffix.startswith("/branches/"):
            branch = suffix.removeprefix("/branches/")
            if branch == "main":
                return _github_branch(sha=self.base_sha) | _protected_branch_with_checks(
                    *self.required_names
                )
            return _github_branch(sha=self.refs[branch])
        if suffix.startswith("/git/commits/"):
            sha = suffix.removeprefix("/git/commits/")
            if sha == "base-main":
                return _git_commit(sha, "tree-base")
            if sha in {"head-1", "head-2"}:
                return _git_commit(sha, self.head_tree)
            if sha == "candidate-after-1":
                return _git_commit(sha, self.candidate_tree, parents=("base-main", "head-1"))
            if sha == "candidate-after-2":
                return _git_commit(
                    sha, self.candidate_tree, parents=("candidate-after-1", "head-2")
                )
        if suffix.startswith("/pulls/"):
            self.pull_reads += 1
            head = self.head_sha
            if self.change_on_confirmation and self.pull_reads > 1:
                head = "changed-head"
            return {
                "number": 1,
                "state": "open",
                "merged": False,
                "draft": False,
                "mergeable": None if self.change_unbound_fields and self.pull_reads == 1 else True,
                "head": {"sha": head},
                "base": {
                    "sha": self.base_sha,
                    "ref": "main",
                    "repo": {"full_name": "example/merge-train-repo"},
                },
            }
        if suffix.startswith("/compare/"):
            return {"status": "ahead" if self.contains_base else "diverged"}
        if "/status" in suffix:
            return {"sha": self.status_head, "state": "success", "statuses": []}
        if "/check-runs" in suffix:
            return {
                "check_runs": [
                    _required_check_run(
                        name, self.check_status, self.check_conclusion, app_id=self.check_app
                    )
                    | {"head_sha": self.check_head}
                    for name in self.observed_names
                ]
            }
        raise AssertionError(f"unexpected provider request: {method} {path}")


class HeadCheckReuseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.transport = _ReuseTransport()
        self.client = GitHubMergeTrainClient(transport=self.transport)
        candidate = _batch_candidate()
        self.candidate = candidate.model_copy(update={"entries": candidate.entries[:1]})

    def build(self) -> MergeTrainBatchCandidate:
        return self.client.build_batch_candidate(candidate=self.candidate)

    def test_identical_tree_reuses_exact_head_checks_without_publishing_ci_ref(self) -> None:
        built = self.build()
        self.assertEqual(built.status, "passed")
        self.assertEqual(built.required_checks_status, "pass")
        self.assertEqual(built.candidate_tree_sha, built.entries[0].head_tree_sha)
        self.assertEqual(
            built.candidate_ref, merge_train_construction_ref(self.candidate.candidate_ref)
        )
        self.assertNotIn(
            self.candidate.candidate_ref.removeprefix("refs/heads/"), self.transport.refs
        )
        self.assertEqual(
            self.transport.refs[built.candidate_ref.removeprefix("refs/heads/")],
            built.candidate_sha,
        )
        reuse = built.head_check_reuse
        assert reuse is not None
        self.assertEqual(reuse.head_sha, built.entries[0].head_sha)
        self.assertEqual(reuse.tree_sha, built.candidate_tree_sha)
        self.assertEqual(
            {check.name for check in reuse.required_checks}, set(self.transport.required_names)
        )
        self.assertTrue(all(check.sources == ("check_run",) for check in reuse.required_checks))
        # The durable record retains both structural provenance and the reuse proof.
        restored = MergeTrainBatchCandidate.model_validate_json(built.model_dump_json())
        self.assertEqual(restored, built)
        assert restored.structural_provenance is not None
        self.assertTrue(restored.structural_provenance.complete)

    def test_different_tree_publishes_full_candidate_ci(self) -> None:
        self.transport.candidate_tree = "tree-with-base-change"
        built = self.build()
        self.assertEqual(built.status, "ready_for_checks")
        self.assertIsNone(built.head_check_reuse)
        self.assertEqual(built.candidate_ref, self.candidate.candidate_ref)
        self.assertEqual(
            self.transport.refs[built.candidate_ref.removeprefix("refs/heads/")],
            built.candidate_sha,
        )
        self.assertNotIn(
            merge_train_construction_ref(built.candidate_ref).removeprefix("refs/heads/"),
            self.transport.refs,
        )

    def test_batch_runs_candidate_ci_even_if_terminal_tree_equals_a_member_head(self) -> None:
        self.candidate = _batch_candidate()
        built = self.build()
        self.assertEqual(built.candidate_tree_sha, built.entries[-1].head_tree_sha)
        self.assertEqual(built.status, "ready_for_checks")
        self.assertIsNone(built.head_check_reuse)
        self.assertIn(built.candidate_ref.removeprefix("refs/heads/"), self.transport.refs)

    def test_retarget_or_force_push_history_selects_full_ci_including_later_pages(self) -> None:
        for event in ("base_ref_changed", "base_ref_force_pushed"):
            with self.subTest(event=event):
                self.setUp()
                history: list[dict[str, object]] = [{"event": "commented"}] * 100
                self.transport.base_history = history + [{"event": event}]
                built = self.build()
                self.assertEqual(built.status, "ready_for_checks")
                self.assertIsNone(built.head_check_reuse)

    def test_unrelated_mergeability_changes_do_not_veto_identity_reuse(self) -> None:
        self.transport.change_unbound_fields = True
        self.assertEqual(self.build().status, "passed")

    def test_unproven_or_changed_evidence_runs_full_candidate_ci(self) -> None:
        scenarios: tuple[dict[str, object], ...] = (
            {"base_sha": "new-base"},
            {"head_sha": "new-head"},
            {"contains_base": False},
            {"check_head": "old-head"},
            {"status_head": "old-head"},
            {"check_conclusion": "failure"},
            {"check_conclusion": "cancelled"},
            {"check_status": "in_progress"},
            {"check_app": 99},
            {"observed_names": ("ci-gate",)},
            {"required_names": ("ci-gate", "security-gate", "new-required-check")},
            {"change_on_confirmation": True},
            {"unavailable_path": "/check-runs"},
        )
        for changes in scenarios:
            with self.subTest(changes=changes):
                self.setUp()
                for name, value in changes.items():
                    setattr(self.transport, name, value)
                built = self.build()
                self.assertEqual(built.status, "ready_for_checks")
                self.assertIsNone(built.head_check_reuse)
                self.assertIn(built.candidate_ref.removeprefix("refs/heads/"), self.transport.refs)

    def test_observation_rechecks_head_and_current_required_policy(self) -> None:
        built = self.build()
        self.transport.requests.clear()
        observed = self.client.observe_batch_candidate_checks(candidate=built)
        self.assertEqual(observed.status, "passed")
        self.assertTrue(any("/check-runs" in request.path for request in self.transport.requests))
        self.transport.required_names += ("new-check",)
        fallback = self.client.observe_batch_candidate_checks(candidate=observed)
        self.assertEqual(fallback.status, "ready_for_checks")
        self.assertEqual(fallback.required_checks_status, "pending")
        self.assertIsNone(fallback.head_check_reuse)
        self.assertEqual(fallback.candidate_ref, self.candidate.candidate_ref)
        self.assertNotIn(built.candidate_ref.removeprefix("refs/heads/"), self.transport.refs)

    def test_reuse_record_cannot_bind_another_tree_head_or_batch(self) -> None:
        built = self.build()
        for updates in (
            {"candidate_tree_sha": "wrong-tree"},
            {"candidate_sha": "wrong-commit"},
            {"entries": _batch_candidate().entries},
        ):
            with self.subTest(updates=updates):
                payload = deepcopy(built.model_dump(mode="json")) | updates
                with self.assertRaises(ValueError):
                    MergeTrainBatchCandidate.model_validate(payload)

    def test_durable_reuse_routes_to_landing_and_cleans_the_retained_ref(self) -> None:
        built = self.build()
        record = build_merge_train_batch_candidate_record(
            candidate=built, source="test:head-check-reuse", updated_at=built.updated_at
        )
        with TemporaryDirectory() as directory:
            store = FilesystemRecordStore(Path(directory))
            store.write_merge_train_batch_candidate_record(record)
            restored = store.list_merge_train_batch_candidate_records(repository=built.repository)
        self.assertEqual(restored, (record,))
        decision = decide_merge_train_controller_record_action(
            candidate_records=restored, landing_plan_records=(), stack_collapse_plan_records=()
        )
        self.assertEqual(decision.action, "plan_landing")
        plan = build_merge_train_batch_landing_plan(
            candidate=restored[0].candidate, merge_method="merge", created_at=built.updated_at
        )
        self.assertEqual(plan.candidate_ref, built.candidate_ref)
        self.assertEqual(plan.entries[0].expected_head_tree_sha, built.candidate_tree_sha)
        self.assertTrue(self.client.cleanup_batch_candidate_ref(landing_plan=plan))
        self.assertNotIn(built.candidate_ref.removeprefix("refs/heads/"), self.transport.refs)
