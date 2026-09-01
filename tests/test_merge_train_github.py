import unittest
from email.message import Message
from unittest.mock import patch
from urllib.error import HTTPError

from control_plane.contracts.merge_train_batch import MergeTrainBatchCandidate
from control_plane.contracts.merge_train_batch import MergeTrainBatchCandidateRecord
from control_plane.contracts.merge_train_batch import MergeTrainBatchEntry
from control_plane.contracts.merge_train_batch import MergeTrainBatchLandingPlan
from control_plane.contracts.merge_train_batch import MergeTrainBatchLandingPlanRecord
from control_plane.contracts.merge_train_batch import build_merge_train_batch_candidate_ref
from control_plane.contracts.merge_train_batch import build_merge_train_batch_id
from control_plane.contracts.merge_train_batch import build_merge_train_batch_landing_plan
from control_plane.contracts.merge_train_structural_provenance import (
    MergeTrainStructuralDeltaFingerprint,
    MergeTrainStructuralEntryObservation,
    MergeTrainStructuralEvaluationInput,
)
from control_plane.merge_train_github import GitHubMergeTrainClient
from control_plane.merge_train_github import GitHubMergeTrainSnapshotReader
from control_plane.merge_train_github import MergeTrainGitHubError
from control_plane.merge_train_github import MergeTrainGitHubStaleHeadError
from control_plane.merge_train_github import RecordingMergeTrainGitHubTransport
from control_plane.merge_train_github import UrllibMergeTrainGitHubTransport
from control_plane.merge_train_structural_provenance import (
    evaluate_merge_train_structural_candidate,
)


class _PermissiveMergeAdmissionGuard:
    def __init__(self) -> None:
        self.admit_calls: list[dict[str, object]] = []
        self.landed_calls: list[dict[str, object]] = []
        self.reconcile_required_calls: list[dict[str, object]] = []

    def admit(self, **kwargs: object) -> object:
        self.admit_calls.append(kwargs)
        return object()

    def record_landed(self, **kwargs: object) -> None:
        self.landed_calls.append(kwargs)

    def record_provider_failure(self, **_: object) -> None:
        return None

    def record_reconcile_required(self, **kwargs: object) -> None:
        self.reconcile_required_calls.append(kwargs)

    def reconcile_existing_landed(self, **_: object) -> None:
        return None

    def reconcile_existing_no_effect(self, **_: object) -> None:
        return None

    def update_landing_plan(self, _: MergeTrainBatchLandingPlan) -> None:
        return None


class GitHubMergeTrainClientTests(unittest.TestCase):
    def setUp(self) -> None:
        original = GitHubMergeTrainClient.land_batch_candidate

        def guarded_land_batch_candidate(
            client: GitHubMergeTrainClient,
            *,
            landing_plan: MergeTrainBatchLandingPlan,
            admission_guard: object | None = None,
            recorded_at: str = "",
            provider_checkpoint: object | None = None,
            checkpoint: object | None = None,
        ) -> MergeTrainBatchLandingPlan:
            return original(
                client,
                landing_plan=landing_plan,
                admission_guard=(
                    admission_guard or _PermissiveMergeAdmissionGuard()  # type: ignore[arg-type]
                ),
                recorded_at=recorded_at or "2026-08-11T00:00:00Z",
                provider_checkpoint=provider_checkpoint,  # type: ignore[arg-type]
                checkpoint=checkpoint,  # type: ignore[arg-type]
            )

        patcher = patch.object(
            GitHubMergeTrainClient,
            "land_batch_candidate",
            new=guarded_land_batch_candidate,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_add_pull_request_label_uses_issue_labels_endpoint(self) -> None:
        transport = RecordingMergeTrainGitHubTransport()
        client = GitHubMergeTrainClient(transport=transport)

        client.add_pull_request_label(
            repository="cbusillo/sellyouroutboard",
            pull_request_number=42,
            label="merge-blocked",
        )

        self.assertEqual(len(transport.requests), 1)
        request = transport.requests[0]
        self.assertEqual(request.method, "POST")
        self.assertEqual(request.path, "/repos/cbusillo/sellyouroutboard/issues/42/labels")
        self.assertEqual(request.body, {"labels": ["merge-blocked"]})

    def test_update_pull_request_branch_uses_expected_head_sha(self) -> None:
        transport = RecordingMergeTrainGitHubTransport()
        client = GitHubMergeTrainClient(transport=transport)

        client.update_pull_request_branch(
            repository="cbusillo/sellyouroutboard",
            pull_request_number=42,
            expected_head_sha="head-42",
        )

        self.assertEqual(len(transport.requests), 1)
        request = transport.requests[0]
        self.assertEqual(request.method, "PUT")
        self.assertEqual(request.path, "/repos/cbusillo/sellyouroutboard/pulls/42/update-branch")
        self.assertEqual(request.body, {"expected_head_sha": "head-42"})

    def test_merge_pull_request_uses_sha_guard_and_policy_method(self) -> None:
        transport = RecordingMergeTrainGitHubTransport(responses=({"sha": "merge-sha-42"},))
        client = GitHubMergeTrainClient(transport=transport)

        merge_commit_sha = client.merge_pull_request(
            repository="cbusillo/sellyouroutboard",
            pull_request_number=42,
            head_sha="head-42",
            merge_method="merge",
        )

        self.assertEqual(merge_commit_sha, "merge-sha-42")
        self.assertEqual(len(transport.requests), 1)
        request = transport.requests[0]
        self.assertEqual(request.method, "PUT")
        self.assertEqual(request.path, "/repos/cbusillo/sellyouroutboard/pulls/42/merge")
        self.assertEqual(request.body, {"sha": "head-42", "merge_method": "merge"})

    def test_merge_pull_request_requires_merge_response_sha(self) -> None:
        transport = RecordingMergeTrainGitHubTransport(responses=({"merged": True},))
        client = GitHubMergeTrainClient(transport=transport)

        with self.assertRaisesRegex(MergeTrainGitHubError, "merge commit SHA"):
            client.merge_pull_request(
                repository="cbusillo/sellyouroutboard",
                pull_request_number=42,
                head_sha="head-42",
                merge_method="merge",
            )

    def test_comment_pull_request_posts_issue_comment(self) -> None:
        transport = RecordingMergeTrainGitHubTransport(
            responses=({"html_url": "https://github.com/example/repo/pull/11#issuecomment-1"},)
        )

        comment_url = GitHubMergeTrainClient(transport=transport).comment_pull_request(
            repository="example/merge-train-repo",
            pull_request_number=11,
            body="landed through root",
        )

        self.assertEqual(comment_url, "https://github.com/example/repo/pull/11#issuecomment-1")
        self.assertEqual(transport.requests[0].method, "POST")
        self.assertEqual(
            transport.requests[0].path,
            "/repos/example/merge-train-repo/issues/11/comments",
        )
        self.assertEqual(transport.requests[0].body, {"body": "landed through root"})

    def test_find_pull_request_comment_url_returns_matching_comment(self) -> None:
        transport = RecordingMergeTrainGitHubTransport(
            responses=(
                [
                    {
                        "body": "other comment",
                        "html_url": "https://github.com/example/repo/pull/11#issuecomment-1",
                    },
                    {
                        "body": "Launchplane landed this stacked PR through root PR #10.",
                        "html_url": "https://github.com/example/repo/pull/11#issuecomment-2",
                    },
                ],
            )
        )

        comment_url = GitHubMergeTrainClient(transport=transport).find_pull_request_comment_url(
            repository="example/merge-train-repo",
            pull_request_number=11,
            body_contains="root PR #10",
        )

        self.assertEqual(comment_url, "https://github.com/example/repo/pull/11#issuecomment-2")

    def test_pull_request_has_label_reads_pr_labels(self) -> None:
        transport = RecordingMergeTrainGitHubTransport(
            responses=({"labels": [{"name": "stack-landed"}]},)
        )

        has_label = GitHubMergeTrainClient(transport=transport).pull_request_has_label(
            repository="example/merge-train-repo",
            pull_request_number=11,
            label="stack-landed",
        )

        self.assertTrue(has_label)

    def test_pull_request_is_closed_checks_head_sha(self) -> None:
        transport = RecordingMergeTrainGitHubTransport(
            responses=({"state": "closed", "head": {"sha": "child-head"}},)
        )

        is_closed = GitHubMergeTrainClient(transport=transport).pull_request_is_closed(
            repository="example/merge-train-repo",
            pull_request_number=11,
            expected_head_sha="child-head",
        )

        self.assertTrue(is_closed)

    def test_pull_request_is_merged_returns_merge_commit_sha(self) -> None:
        transport = RecordingMergeTrainGitHubTransport(
            responses=(
                {
                    "merged": True,
                    "merge_commit_sha": "merge-commit",
                    "head": {"sha": "head-11"},
                },
            )
        )

        merge_commit_sha = GitHubMergeTrainClient(transport=transport).pull_request_is_merged(
            repository="example/merge-train-repo",
            pull_request_number=11,
            expected_head_sha="head-11",
        )

        self.assertEqual(merge_commit_sha, "merge-commit")

    def test_branch_contains_commit_uses_compare_endpoint(self) -> None:
        transport = RecordingMergeTrainGitHubTransport(responses=({"status": "ahead"},))

        contains_commit = GitHubMergeTrainClient(transport=transport).branch_contains_commit(
            repository="example/merge-train-repo",
            branch_ref="feature/root",
            commit_sha="merge-31-30",
        )

        self.assertTrue(contains_commit)
        self.assertEqual(
            transport.requests[0].path,
            "/repos/example/merge-train-repo/compare/merge-31-30...feature%2Froot",
        )

    def test_branch_head_sha_reads_branch_endpoint(self) -> None:
        transport = RecordingMergeTrainGitHubTransport(
            responses=(_github_branch(sha="parent-after-child"),)
        )

        branch_head_sha = GitHubMergeTrainClient(transport=transport).branch_head_sha(
            repository="example/merge-train-repo",
            branch_ref="feature/root",
        )

        self.assertEqual(branch_head_sha, "parent-after-child")

    def test_candidate_ref_exists_uses_git_ref_endpoint(self) -> None:
        transport = RecordingMergeTrainGitHubTransport(responses=({"ref": "refs/heads/x"},))

        exists = GitHubMergeTrainClient(transport=transport).candidate_ref_exists(
            repository="example/merge-train-repo",
            reference="refs/heads/launchplane/train/example/merge-train-repo/main/batch-1",
        )

        self.assertTrue(exists)
        self.assertIn(
            "/repos/example/merge-train-repo/git/ref/heads/launchplane/train/example/merge-train-repo/main/batch-1",
            transport.requests[0].path,
        )

    def test_close_pull_request_checks_head_sha_before_closing(self) -> None:
        transport = RecordingMergeTrainGitHubTransport(
            responses=(
                {"head": {"sha": "child-head"}},
                {"state": "closed", "head": {"sha": "child-head"}},
            )
        )

        GitHubMergeTrainClient(transport=transport).close_pull_request(
            repository="example/merge-train-repo",
            pull_request_number=11,
            expected_head_sha="child-head",
        )

        self.assertEqual(
            [(request.method, request.path, request.body) for request in transport.requests],
            [
                ("GET", "/repos/example/merge-train-repo/pulls/11", None),
                ("PATCH", "/repos/example/merge-train-repo/pulls/11", {"state": "closed"}),
            ],
        )

    def test_close_pull_request_rejects_child_head_moved_during_close(self) -> None:
        transport = RecordingMergeTrainGitHubTransport(
            responses=(
                {"head": {"sha": "child-head"}},
                {"state": "closed", "head": {"sha": "moved-child-head"}},
            )
        )

        with self.assertRaises(MergeTrainGitHubStaleHeadError):
            GitHubMergeTrainClient(transport=transport).close_pull_request(
                repository="example/merge-train-repo",
                pull_request_number=11,
                expected_head_sha="child-head",
            )

        self.assertEqual([request.method for request in transport.requests], ["GET", "PATCH"])

    def test_close_pull_request_rejects_open_patch_response(self) -> None:
        transport = RecordingMergeTrainGitHubTransport(
            responses=(
                {"head": {"sha": "child-head"}},
                {"state": "open", "head": {"sha": "child-head"}},
            )
        )

        with self.assertRaisesRegex(MergeTrainGitHubStaleHeadError, "did not remain closed"):
            GitHubMergeTrainClient(transport=transport).close_pull_request(
                repository="example/merge-train-repo",
                pull_request_number=11,
                expected_head_sha="child-head",
            )

        self.assertEqual([request.method for request in transport.requests], ["GET", "PATCH"])

    def test_close_pull_request_rejects_moved_child_head(self) -> None:
        transport = RecordingMergeTrainGitHubTransport(
            responses=({"head": {"sha": "moved-child-head"}},)
        )

        with self.assertRaises(MergeTrainGitHubStaleHeadError):
            GitHubMergeTrainClient(transport=transport).close_pull_request(
                repository="example/merge-train-repo",
                pull_request_number=11,
                expected_head_sha="child-head",
            )

        self.assertEqual(len(transport.requests), 1)

    def test_merge_stack_child_into_parent_checks_parent_sha_then_merges_child_head(self) -> None:
        transport = RecordingMergeTrainGitHubTransport(
            responses=(
                _github_branch(sha="parent-head"),
                {"sha": "parent-after-child"},
            )
        )

        merge_commit_sha = GitHubMergeTrainClient(
            transport=transport
        ).merge_stack_child_into_parent(
            repository="example/merge-train-repo",
            child_head_sha="child-head",
            expected_parent_head_sha="parent-head",
            parent_head_ref="feature/root",
            protected_base_ref="main",
            collapse_id="collapse-123",
            child_pull_request_number=11,
            parent_pull_request_number=10,
        )

        self.assertEqual(merge_commit_sha, "parent-after-child")
        self.assertEqual(
            [(request.method, request.path, request.body) for request in transport.requests],
            [
                (
                    "GET",
                    "/repos/example/merge-train-repo/branches/feature%2Froot",
                    None,
                ),
                (
                    "POST",
                    "/repos/example/merge-train-repo/merges",
                    {
                        "base": "feature/root",
                        "head": "child-head",
                        "commit_message": (
                            "Launchplane stack collapse collapse-123: merge PR #11 into PR #10"
                        ),
                    },
                ),
            ],
        )

    def test_merge_stack_child_into_parent_rejects_stale_parent_branch(self) -> None:
        transport = RecordingMergeTrainGitHubTransport(
            responses=(_github_branch(sha="moved-parent-head"),)
        )

        with self.assertRaises(MergeTrainGitHubStaleHeadError):
            GitHubMergeTrainClient(transport=transport).merge_stack_child_into_parent(
                repository="example/merge-train-repo",
                child_head_sha="child-head",
                expected_parent_head_sha="parent-head",
                parent_head_ref="feature/root",
                protected_base_ref="main",
                collapse_id="collapse-123",
                child_pull_request_number=11,
                parent_pull_request_number=10,
            )

        self.assertEqual(len(transport.requests), 1)
        self.assertEqual(
            transport.requests[0].path,
            "/repos/example/merge-train-repo/branches/feature%2Froot",
        )

    def test_find_stack_child_merge_commit_adopts_exact_provider_effect(self) -> None:
        transport = RecordingMergeTrainGitHubTransport(
            responses=(
                _github_branch(sha="parent-after-child"),
                {
                    "sha": "parent-after-child",
                    "commit": {
                        "message": (
                            "Launchplane stack collapse collapse-123: merge PR #11 into PR #10"
                        )
                    },
                    "parents": [{"sha": "parent-head"}, {"sha": "child-head"}],
                },
            )
        )

        merge_commit_sha = GitHubMergeTrainClient(
            transport=transport
        ).find_stack_child_merge_commit(
            repository="example/merge-train-repo",
            child_head_sha="child-head",
            expected_parent_head_sha="parent-head",
            parent_head_ref="feature/root",
            collapse_id="collapse-123",
            child_pull_request_number=11,
            parent_pull_request_number=10,
        )

        self.assertEqual(merge_commit_sha, "parent-after-child")
        self.assertEqual(transport.requests[-1].method, "GET")
        self.assertEqual(
            transport.requests[-1].path,
            "/repos/example/merge-train-repo/commits/parent-after-child",
        )

    def test_find_stack_child_merge_commit_rejects_unrelated_branch_advance(self) -> None:
        transport = RecordingMergeTrainGitHubTransport(
            responses=(
                _github_branch(sha="unrelated-head"),
                {
                    "sha": "unrelated-head",
                    "commit": {"message": "unrelated change"},
                    "parents": [{"sha": "parent-head"}],
                },
            )
        )

        with self.assertRaises(MergeTrainGitHubStaleHeadError):
            GitHubMergeTrainClient(transport=transport).find_stack_child_merge_commit(
                repository="example/merge-train-repo",
                child_head_sha="child-head",
                expected_parent_head_sha="parent-head",
                parent_head_ref="feature/root",
                collapse_id="collapse-123",
                child_pull_request_number=11,
                parent_pull_request_number=10,
            )

    def test_merge_stack_child_into_parent_rejects_configured_protected_base(self) -> None:
        with self.assertRaisesRegex(MergeTrainGitHubError, "protected base branch"):
            GitHubMergeTrainClient(
                transport=RecordingMergeTrainGitHubTransport()
            ).merge_stack_child_into_parent(
                repository="example/merge-train-repo",
                child_head_sha="child-head",
                expected_parent_head_sha="parent-head",
                parent_head_ref="release/2026",
                protected_base_ref="release/2026",
                collapse_id="collapse-123",
                child_pull_request_number=11,
                parent_pull_request_number=10,
            )

    def test_merge_stack_child_into_parent_allows_main_when_custom_base_is_protected(
        self,
    ) -> None:
        transport = RecordingMergeTrainGitHubTransport(
            responses=(
                _github_branch(sha="parent-head"),
                {"sha": "parent-after-child"},
            )
        )

        merge_commit_sha = GitHubMergeTrainClient(
            transport=transport
        ).merge_stack_child_into_parent(
            repository="example/merge-train-repo",
            child_head_sha="child-head",
            expected_parent_head_sha="parent-head",
            parent_head_ref="main",
            protected_base_ref="release/2026",
            collapse_id="collapse-123",
            child_pull_request_number=11,
            parent_pull_request_number=10,
        )

        self.assertEqual(merge_commit_sha, "parent-after-child")

    def test_build_batch_candidate_creates_ref_and_merges_heads_in_order(self) -> None:
        candidate = _batch_candidate()
        transport = RecordingMergeTrainGitHubTransport(
            responses=(
                {"ref": candidate.candidate_ref, "object": {"sha": "base-main"}},
                _git_commit("base-main", "tree-base"),
                _git_commit("head-1", "tree-head-1"),
                _git_commit("head-2", "tree-head-2"),
                _merge_commit("candidate-after-1", "tree-candidate-1"),
                _github_branch(sha="candidate-after-1", tree_sha="tree-candidate-1"),
                _git_commit(
                    "candidate-after-1",
                    "tree-candidate-1",
                    parents=("base-main", "head-1"),
                ),
                _merge_commit("candidate-after-2", "tree-candidate-2"),
                _github_branch(sha="candidate-after-2", tree_sha="tree-candidate-2"),
                _git_commit(
                    "candidate-after-2",
                    "tree-candidate-2",
                    parents=("candidate-after-1", "head-2"),
                ),
            )
        )

        built_candidate = GitHubMergeTrainClient(transport=transport).build_batch_candidate(
            candidate=candidate
        )

        self.assertEqual(built_candidate.status, "ready_for_checks")
        self.assertEqual(built_candidate.candidate_sha, "candidate-after-2")
        self.assertEqual(
            [(request.method, request.path, request.body) for request in transport.requests],
            [
                (
                    "POST",
                    "/repos/example/merge-train-repo/git/refs",
                    {"ref": candidate.candidate_ref, "sha": "base-main"},
                ),
                (
                    "GET",
                    "/repos/example/merge-train-repo/git/commits/base-main",
                    None,
                ),
                (
                    "GET",
                    "/repos/example/merge-train-repo/git/commits/head-1",
                    None,
                ),
                (
                    "GET",
                    "/repos/example/merge-train-repo/git/commits/head-2",
                    None,
                ),
                (
                    "POST",
                    "/repos/example/merge-train-repo/merges",
                    {
                        "base": "launchplane/train/example/merge-train-repo/main/"
                        f"{candidate.batch_id}",
                        "head": "head-1",
                        "commit_message": f"Launchplane merge train {candidate.batch_id}: merge PR #1",
                    },
                ),
                (
                    "GET",
                    "/repos/example/merge-train-repo/branches/launchplane%2Ftrain%2Fexample%2Fmerge-train-repo%2Fmain%2F"
                    f"{candidate.batch_id}",
                    None,
                ),
                (
                    "GET",
                    "/repos/example/merge-train-repo/git/commits/candidate-after-1",
                    None,
                ),
                (
                    "POST",
                    "/repos/example/merge-train-repo/merges",
                    {
                        "base": "launchplane/train/example/merge-train-repo/main/"
                        f"{candidate.batch_id}",
                        "head": "head-2",
                        "commit_message": f"Launchplane merge train {candidate.batch_id}: merge PR #2",
                    },
                ),
                (
                    "GET",
                    "/repos/example/merge-train-repo/branches/launchplane%2Ftrain%2Fexample%2Fmerge-train-repo%2Fmain%2F"
                    f"{candidate.batch_id}",
                    None,
                ),
                (
                    "GET",
                    "/repos/example/merge-train-repo/git/commits/candidate-after-2",
                    None,
                ),
            ],
        )

    def test_build_batch_candidate_resets_existing_ref(self) -> None:
        candidate = _batch_candidate()
        for status_code in (409, 422):
            with self.subTest(status_code=status_code):
                transport = RecordingMergeTrainGitHubTransport(
                    responses=(
                        MergeTrainGitHubError("reference exists", status_code=status_code),
                        {"ref": candidate.candidate_ref, "object": {"sha": "base-main"}},
                        _git_commit("base-main", "tree-base"),
                        _git_commit("head-1", "tree-head-1"),
                        _git_commit("head-2", "tree-head-2"),
                        _merge_commit("candidate-after-1", "tree-candidate-1"),
                        _github_branch(sha="candidate-after-1", tree_sha="tree-candidate-1"),
                        _git_commit(
                            "candidate-after-1",
                            "tree-candidate-1",
                            parents=("base-main", "head-1"),
                        ),
                        _merge_commit("candidate-after-2", "tree-candidate-2"),
                        _github_branch(sha="candidate-after-2", tree_sha="tree-candidate-2"),
                        _git_commit(
                            "candidate-after-2",
                            "tree-candidate-2",
                            parents=("candidate-after-1", "head-2"),
                        ),
                    )
                )

                GitHubMergeTrainClient(transport=transport).build_batch_candidate(
                    candidate=candidate
                )

                self.assertEqual(transport.requests[1].method, "PATCH")
                self.assertEqual(
                    transport.requests[1].path,
                    "/repos/example/merge-train-repo/git/refs/heads/launchplane/train/example/merge-train-repo/main/"
                    f"{candidate.batch_id}",
                )
                self.assertEqual(transport.requests[1].body, {"sha": "base-main", "force": True})

    def test_build_batch_candidate_reports_conflict_without_landing_prs(self) -> None:
        candidate = _batch_candidate()
        transport = RecordingMergeTrainGitHubTransport(
            responses=(
                {"ref": candidate.candidate_ref, "object": {"sha": "base-main"}},
                _git_commit("base-main", "tree-base"),
                _git_commit("head-1", "tree-head-1"),
                _git_commit("head-2", "tree-head-2"),
                _merge_commit("candidate-after-1", "tree-candidate-1"),
                _github_branch(sha="candidate-after-1", tree_sha="tree-candidate-1"),
                _git_commit(
                    "candidate-after-1",
                    "tree-candidate-1",
                    parents=("base-main", "head-1"),
                ),
                MergeTrainGitHubStaleHeadError("conflict", status_code=409),
            )
        )

        with self.assertRaises(MergeTrainGitHubStaleHeadError):
            GitHubMergeTrainClient(transport=transport).build_batch_candidate(candidate=candidate)

        self.assertEqual(
            [request.method for request in transport.requests],
            ["POST", "GET", "GET", "GET", "POST", "GET", "GET", "POST"],
        )

    def test_build_batch_candidate_records_github_204_as_no_op_step(self) -> None:
        candidate = _batch_candidate()
        transport = RecordingMergeTrainGitHubTransport(
            responses=(
                {"ref": candidate.candidate_ref, "object": {"sha": "base-main"}},
                _git_commit("base-main", "tree-base"),
                _git_commit("head-1", "tree-head-1"),
                _git_commit("head-2", "tree-head-2"),
                None,
                _github_branch(sha="base-main", tree_sha="tree-base"),
                _git_commit("base-main", "tree-base"),
                {"status": "ahead"},
                _merge_commit("candidate-after-2", "tree-candidate-2"),
                _github_branch(sha="candidate-after-2", tree_sha="tree-candidate-2"),
                _git_commit(
                    "candidate-after-2",
                    "tree-candidate-2",
                    parents=("base-main", "head-2"),
                ),
            )
        )

        built = GitHubMergeTrainClient(transport=transport).build_batch_candidate(
            candidate=candidate
        )

        self.assertIsNotNone(built.structural_provenance)
        assert built.structural_provenance is not None
        first_step = built.structural_provenance.steps[0]
        self.assertEqual(first_step.kind, "no_op_already_contained")
        self.assertEqual(first_step.result_sha, first_step.parent_sha)
        self.assertEqual(first_step.result_tree_sha, first_step.parent_tree_sha)

    def test_build_batch_candidate_rejects_unexplained_merge_parent(self) -> None:
        candidate = _batch_candidate()
        transport = RecordingMergeTrainGitHubTransport(
            responses=(
                {"ref": candidate.candidate_ref, "object": {"sha": "base-main"}},
                _git_commit("base-main", "tree-base"),
                _git_commit("head-1", "tree-head-1"),
                _git_commit("head-2", "tree-head-2"),
                {"sha": "candidate-after-1", "commit": {"tree": {"sha": "untrusted"}}},
                _github_branch(sha="candidate-after-1", tree_sha="untrusted"),
                _git_commit(
                    "candidate-after-1",
                    "tree-candidate-1",
                    parents=("base-main", "unexplained-commit", "head-1"),
                ),
            )
        )

        with self.assertRaisesRegex(MergeTrainGitHubStaleHeadError, "parents"):
            GitHubMergeTrainClient(transport=transport).build_batch_candidate(candidate=candidate)

    def test_observe_batch_candidate_checks_marks_passed_candidate(self) -> None:
        candidate = _batch_candidate().model_copy(
            update={"candidate_sha": "candidate-sha", "status": "ready_for_checks"}
        )
        transport = RecordingMergeTrainGitHubTransport(
            responses=(
                _combined_status(),
                {"check_runs": [_check_run("completed", "success")]},
            )
        )

        observed_candidate = GitHubMergeTrainClient(
            transport=transport
        ).observe_batch_candidate_checks(candidate=candidate)

        self.assertEqual(observed_candidate.status, "passed")
        self.assertEqual(observed_candidate.required_checks_status, "pass")
        self.assertEqual(
            [request.path for request in transport.requests],
            [
                "/repos/example/merge-train-repo/commits/candidate-sha/status?per_page=100&page=1",
                "/repos/example/merge-train-repo/commits/candidate-sha/check-runs?per_page=100&page=1",
            ],
        )

    def test_observe_batch_candidate_checks_leaves_pending_candidate_unlandable(self) -> None:
        candidate = _batch_candidate().model_copy(
            update={"candidate_sha": "candidate-sha", "status": "ready_for_checks"}
        )
        transport = RecordingMergeTrainGitHubTransport(
            responses=(
                _combined_status(state="pending"),
                {"check_runs": [_check_run("queued", None)]},
            )
        )

        observed_candidate = GitHubMergeTrainClient(
            transport=transport
        ).observe_batch_candidate_checks(candidate=candidate)

        self.assertEqual(observed_candidate.status, "ready_for_checks")
        self.assertEqual(observed_candidate.required_checks_status, "pending")

    def test_observe_batch_candidate_checks_excludes_launchplane_advisory_runs(self) -> None:
        candidate = _batch_candidate().model_copy(
            update={"candidate_sha": "candidate-sha", "status": "ready_for_checks"}
        )
        baseline_transport = RecordingMergeTrainGitHubTransport(
            responses=(
                _combined_status(),
                {"check_runs": [{"name": "ci-gate", **_check_run("completed", "success")}]},
            )
        )
        transport = RecordingMergeTrainGitHubTransport(
            responses=(
                _combined_status(),
                {
                    "check_runs": [
                        {"name": "ci-gate", **_check_run("completed", "success")},
                        {
                            "name": "launchplane/engineering-review",
                            **_check_run("queued", None),
                        },
                        {
                            "name": "launchplane/owner-acceptance",
                            **_check_run("in_progress", None),
                        },
                    ]
                },
            )
        )

        observed_candidate = GitHubMergeTrainClient(
            transport=transport
        ).observe_batch_candidate_checks(candidate=candidate)
        baseline_candidate = GitHubMergeTrainClient(
            transport=baseline_transport
        ).observe_batch_candidate_checks(candidate=candidate)

        self.assertEqual(observed_candidate, baseline_candidate)

    def test_observe_batch_candidate_checks_excludes_legacy_launchplane_status(self) -> None:
        candidate = _batch_candidate().model_copy(
            update={"candidate_sha": "candidate-sha", "status": "ready_for_checks"}
        )
        baseline_transport = RecordingMergeTrainGitHubTransport(
            responses=(
                _combined_status(),
                {"check_runs": [_check_run("completed", "success")]},
            )
        )
        transport = RecordingMergeTrainGitHubTransport(
            responses=(
                {
                    "state": "failure",
                    "total_count": 2,
                    "statuses": [
                        {"context": "ci-status", "state": "success"},
                        {
                            "context": "launchplane/engineering-review-shadow",
                            "state": "failure",
                        },
                    ],
                },
                {"check_runs": [_check_run("completed", "success")]},
            )
        )

        observed_candidate = GitHubMergeTrainClient(
            transport=transport
        ).observe_batch_candidate_checks(candidate=candidate)
        baseline_candidate = GitHubMergeTrainClient(
            transport=baseline_transport
        ).observe_batch_candidate_checks(candidate=candidate)

        self.assertEqual(observed_candidate, baseline_candidate)

    def test_land_batch_candidate_merges_original_prs_in_order(self) -> None:
        landing_plan = _landing_plan()
        checkpoints: list[tuple[str, int, int]] = []
        provider_checkpoints: list[tuple[int, int]] = []
        transport = RecordingMergeTrainGitHubTransport(
            responses=(
                _github_branch(sha="base-main"),
                *_normal_landing_responses(1, "base-main", "merge-sha-1"),
                _github_branch(sha="merge-sha-1"),
                *_normal_landing_responses(2, "merge-sha-1", "merge-sha-2"),
                _github_branch(sha="merge-sha-2"),
            )
        )

        landed_plan = GitHubMergeTrainClient(transport=transport).land_batch_candidate(
            landing_plan=landing_plan,
            checkpoint=lambda progress, entry, phase: checkpoints.append(
                (
                    phase,
                    entry.pull_request_number,
                    sum(item.status == "merged" for item in progress.entries),
                )
            ),
            provider_checkpoint=lambda progress, entry: provider_checkpoints.append(
                (
                    entry.pull_request_number,
                    sum(item.status == "merged" for item in progress.entries),
                )
            ),
        )

        self.assertEqual([entry.status for entry in landed_plan.entries], ["merged", "merged"])
        self.assertEqual(
            [entry.merge_commit_sha for entry in landed_plan.entries],
            ["merge-sha-1", "merge-sha-2"],
        )
        self.assertEqual(
            [(request.method, request.path, request.body) for request in transport.requests],
            [
                ("GET", "/repos/example/merge-train-repo/branches/main", None),
                ("GET", "/repos/example/merge-train-repo/git/commits/head-1", None),
                ("GET", "/repos/example/merge-train-repo/pulls/1", None),
                (
                    "PUT",
                    "/repos/example/merge-train-repo/pulls/1/merge",
                    {"sha": "head-1", "merge_method": "merge"},
                ),
                ("GET", "/repos/example/merge-train-repo/git/commits/merge-sha-1", None),
                ("GET", "/repos/example/merge-train-repo/branches/main", None),
                ("GET", "/repos/example/merge-train-repo/branches/main", None),
                ("GET", "/repos/example/merge-train-repo/git/commits/head-2", None),
                ("GET", "/repos/example/merge-train-repo/pulls/2", None),
                (
                    "PUT",
                    "/repos/example/merge-train-repo/pulls/2/merge",
                    {"sha": "head-2", "merge_method": "merge"},
                ),
                ("GET", "/repos/example/merge-train-repo/git/commits/merge-sha-2", None),
                ("GET", "/repos/example/merge-train-repo/branches/main", None),
                ("GET", "/repos/example/merge-train-repo/branches/main", None),
            ],
        )
        self.assertEqual(
            checkpoints,
            [
                ("merge_entry", 1, 0),
                ("entry_merged", 1, 1),
                ("merge_entry", 2, 1),
                ("entry_merged", 2, 2),
            ],
        )
        self.assertEqual(provider_checkpoints, [(1, 0), (2, 1)])

    def test_land_batch_candidate_records_candidate_no_op_as_skipped(self) -> None:
        landing_plan = _landing_plan()
        first = type(landing_plan.entries[0]).model_validate(
            {
                **landing_plan.entries[0].model_dump(mode="python"),
                "recorded_candidate_parent_sha": "base-main",
                "recorded_candidate_parent_tree_sha": "tree-base",
                "recorded_candidate_result_sha": "base-main",
                "recorded_candidate_result_tree_sha": "tree-base",
            }
        )
        landing_plan = MergeTrainBatchLandingPlan.model_validate(
            {
                **landing_plan.model_dump(mode="python"),
                "entries": (first, landing_plan.entries[1]),
                "landing_plan_sha256": "",
            }
        )
        transport = RecordingMergeTrainGitHubTransport(
            responses=(
                _github_branch(sha="base-main"),
                *_no_op_landing_responses(1, "base-main", "identical"),
                _github_branch(sha="base-main"),
                *_normal_landing_responses(2, "base-main", "merge-sha-2"),
                _github_branch(sha="merge-sha-2"),
            )
        )

        landed = GitHubMergeTrainClient(transport=transport).land_batch_candidate(
            landing_plan=landing_plan
        )

        self.assertEqual([entry.status for entry in landed.entries], ["skipped", "merged"])
        self.assertEqual(landed.entries[0].merge_commit_sha, "base-main")
        self.assertEqual(landed.entries[0].recorded_rolling_base_tree_sha, "tree-base")
        self.assertNotIn(
            "/pulls/1/merge",
            "\n".join(request.path for request in transport.requests),
        )

    def test_failed_no_op_recovery_does_not_persist_admission(self) -> None:
        landing_plan = _landing_plan()
        first = type(landing_plan.entries[0]).model_validate(
            {
                **landing_plan.entries[0].model_dump(mode="python"),
                "recorded_candidate_parent_sha": "base-main",
                "recorded_candidate_parent_tree_sha": "tree-base",
                "recorded_candidate_result_sha": "base-main",
                "recorded_candidate_result_tree_sha": "tree-base",
            }
        )
        landing_plan = MergeTrainBatchLandingPlan.model_validate(
            {
                **landing_plan.model_dump(mode="python"),
                "entries": (first, landing_plan.entries[1]),
                "landing_plan_sha256": "",
            }
        )
        guard = _PermissiveMergeAdmissionGuard()
        client = GitHubMergeTrainClient(
            transport=RecordingMergeTrainGitHubTransport(
                responses=(_github_branch(sha="base-main"),)
            )
        )

        with patch.object(
            client,
            "_recover_no_op_landing_entry",
            side_effect=MergeTrainGitHubStaleHeadError("no-op evidence drifted"),
        ):
            with self.assertRaises(MergeTrainGitHubStaleHeadError):
                client.land_batch_candidate(
                    landing_plan=landing_plan,
                    admission_guard=guard,  # type: ignore[arg-type]
                    recorded_at="2026-08-11T03:01:00Z",
                )

        self.assertEqual(guard.admit_calls, [])

    def test_land_batch_candidate_recovers_planned_no_op_after_later_merge(self) -> None:
        landing_plan = _landing_plan()
        first = type(landing_plan.entries[0]).model_validate(
            {
                **landing_plan.entries[0].model_dump(mode="python"),
                "recorded_candidate_parent_sha": "base-main",
                "recorded_candidate_parent_tree_sha": "tree-base",
                "recorded_candidate_result_sha": "base-main",
                "recorded_candidate_result_tree_sha": "tree-base",
            }
        )
        landing_plan = MergeTrainBatchLandingPlan.model_validate(
            {
                **landing_plan.model_dump(mode="python"),
                "entries": (first, landing_plan.entries[1]),
                "landing_plan_sha256": "",
            }
        )
        transport = RecordingMergeTrainGitHubTransport(
            responses=(
                _github_branch(sha="merge-sha-2"),
                *_no_op_landing_responses(1, "base-main", "ahead"),
                _github_branch(sha="merge-sha-2"),
                *_already_merged_responses(2, "base-main", "merge-sha-2", "identical"),
                _github_branch(sha="merge-sha-2"),
            )
        )

        landed = GitHubMergeTrainClient(transport=transport).land_batch_candidate(
            landing_plan=landing_plan
        )

        self.assertEqual([entry.status for entry in landed.entries], ["skipped", "merged"])
        self.assertEqual(landed.entries[0].merge_commit_sha, "base-main")
        self.assertEqual(landed.entries[1].merge_commit_sha, "merge-sha-2")

    def test_land_batch_candidate_revalidates_persisted_no_op_on_retry(self) -> None:
        landing_plan = _landing_plan()
        first = type(landing_plan.entries[0]).model_validate(
            {
                **landing_plan.entries[0].model_dump(mode="python"),
                "status": "skipped",
                "recorded_candidate_parent_sha": "base-main",
                "recorded_candidate_parent_tree_sha": "tree-base",
                "recorded_candidate_result_sha": "base-main",
                "recorded_candidate_result_tree_sha": "tree-base",
                "recorded_rolling_base_sha": "base-main",
                "recorded_rolling_base_tree_sha": "tree-base",
                "landed_head_sha": "head-1",
                "landed_head_tree_sha": "tree-head-1",
                "merge_commit_sha": "base-main",
                "merge_commit_tree_sha": "tree-base",
            }
        )
        landing_plan = MergeTrainBatchLandingPlan.model_validate(
            {
                **landing_plan.model_dump(mode="python"),
                "entries": (first, landing_plan.entries[1]),
                "landing_plan_sha256": "",
            }
        )
        transport = RecordingMergeTrainGitHubTransport(
            responses=(
                _github_branch(sha="merge-sha-2"),
                *_no_op_landing_responses(1, "base-main", "ahead"),
                _github_branch(sha="merge-sha-2"),
                *_already_merged_responses(2, "base-main", "merge-sha-2", "identical"),
                _github_branch(sha="merge-sha-2"),
            )
        )

        landed = GitHubMergeTrainClient(transport=transport).land_batch_candidate(
            landing_plan=landing_plan
        )

        self.assertEqual(landed.entries[0], first)
        self.assertEqual([entry.status for entry in landed.entries], ["skipped", "merged"])

    def test_actual_landing_evidence_drives_recorded_rolling_evaluator(self) -> None:
        candidate = _batch_candidate()
        build_transport = RecordingMergeTrainGitHubTransport(
            responses=(
                {"ref": candidate.candidate_ref, "object": {"sha": "base-main"}},
                _git_commit("base-main", "tree-base"),
                _git_commit("head-1", "tree-head-1"),
                _git_commit("head-2", "tree-head-2"),
                {"sha": "candidate-after-1"},
                _github_branch(sha="candidate-after-1", tree_sha="tree-candidate-1"),
                _git_commit(
                    "candidate-after-1",
                    "tree-candidate-1",
                    parents=("base-main", "head-1"),
                ),
                {"sha": "candidate-after-2"},
                _github_branch(sha="candidate-after-2", tree_sha="tree-candidate-2"),
                _git_commit(
                    "candidate-after-2",
                    "tree-candidate-2",
                    parents=("candidate-after-1", "head-2"),
                ),
            )
        )
        built = GitHubMergeTrainClient(transport=build_transport).build_batch_candidate(
            candidate=candidate
        )
        passed = MergeTrainBatchCandidate.model_validate(
            {
                **built.model_dump(mode="python"),
                "status": "passed",
                "required_checks_status": "pass",
            }
        )
        landing_plan = build_merge_train_batch_landing_plan(
            candidate=passed,
            merge_method="merge",
            created_at="2026-08-11T05:00:00Z",
        )
        landing_transport = RecordingMergeTrainGitHubTransport(
            responses=(
                _github_branch(sha="base-main"),
                *_normal_landing_responses(1, "base-main", "merge-sha-1"),
                _github_branch(sha="merge-sha-1"),
                *_normal_landing_responses(2, "merge-sha-1", "merge-sha-2"),
                _github_branch(sha="merge-sha-2"),
            )
        )
        landed = GitHubMergeTrainClient(transport=landing_transport).land_batch_candidate(
            landing_plan=landing_plan
        )
        candidate_record = MergeTrainBatchCandidateRecord(
            record_id="candidate-record",
            source="test",
            updated_at="2026-08-11T05:00:00Z",
            candidate=passed,
        )
        landing_record = MergeTrainBatchLandingPlanRecord(
            record_id="landing-record",
            source="test",
            updated_at="2026-08-11T05:01:00Z",
            landing_plan=landed,
        )
        observations = tuple(_structural_observation(entry) for entry in passed.entries)
        evaluation = MergeTrainStructuralEvaluationInput(
            repository=passed.repository,
            base_branch=passed.base_branch,
            target_pull_request_number=2,
            target_queue_position=2,
            observed_base_sha="merge-sha-1",
            observed_base_tree_sha="tree-candidate-1",
            policy_key=passed.policy_key,
            policy_sha256=passed.policy_sha256,
            active_candidate_sha256=passed.candidate_sha256,
            active_landing_plan_sha256=landed.landing_plan_sha256,
            entries=observations,
        )

        result = evaluate_merge_train_structural_candidate(
            evaluation=evaluation,
            candidate_record=candidate_record,
            landing_plan_record=landing_record,
        )

        self.assertEqual(result.status, "recorded_rolling")
        self.assertIn("structural_rolling_chain_recorded", result.reason_codes)

    def test_land_batch_candidate_rejects_retargeted_pull_request(self) -> None:
        landing_plan = _landing_plan()
        transport = RecordingMergeTrainGitHubTransport(
            responses=(
                _github_branch(sha="base-main"),
                _git_commit("head-1", "tree-head-1"),
                _landing_pull_request(
                    1,
                    base_sha="base-main",
                    base_ref="release/2026",
                ),
            )
        )

        with self.assertRaisesRegex(MergeTrainGitHubStaleHeadError, "different base branch"):
            GitHubMergeTrainClient(transport=transport).land_batch_candidate(
                landing_plan=landing_plan
            )

        self.assertEqual([request.method for request in transport.requests], ["GET", "GET", "GET"])

    def test_land_batch_candidate_rejects_pull_request_base_sha_movement(self) -> None:
        landing_plan = _landing_plan()
        transport = RecordingMergeTrainGitHubTransport(
            responses=(
                _github_branch(sha="base-main"),
                _git_commit("head-1", "tree-head-1"),
                _landing_pull_request(1, base_sha="unexpected-base"),
            )
        )

        with self.assertRaisesRegex(MergeTrainGitHubStaleHeadError, "base moved"):
            GitHubMergeTrainClient(transport=transport).land_batch_candidate(
                landing_plan=landing_plan
            )

        self.assertEqual([request.method for request in transport.requests], ["GET", "GET", "GET"])

    def test_land_batch_candidate_accepts_descendant_movement_after_final_merge(self) -> None:
        landing_plan = _landing_plan()
        transport = RecordingMergeTrainGitHubTransport(
            responses=(
                _github_branch(sha="base-main"),
                *_normal_landing_responses(1, "base-main", "merge-sha-1"),
                _github_branch(sha="merge-sha-1"),
                *_normal_landing_responses(2, "merge-sha-1", "merge-sha-2"),
                _github_branch(sha="later-base-sha"),
                {"status": "ahead"},
            )
        )

        landed_plan = GitHubMergeTrainClient(transport=transport).land_batch_candidate(
            landing_plan=landing_plan
        )

        self.assertEqual([entry.status for entry in landed_plan.entries], ["merged", "merged"])
        self.assertEqual(
            transport.requests[-1].path,
            "/repos/example/merge-train-repo/compare/merge-sha-2...main",
        )

    def test_landed_outcome_uses_actual_base_identity_and_containment_proof(self) -> None:
        original_plan = _landing_plan()
        landing_plan = MergeTrainBatchLandingPlan.model_validate(
            {
                **original_plan.model_dump(mode="python"),
                "entries": (original_plan.entries[0],),
                "landing_plan_sha256": "",
            }
        )
        guard = _PermissiveMergeAdmissionGuard()
        transport = RecordingMergeTrainGitHubTransport(
            responses=(
                _github_branch(sha="base-main"),
                _git_commit("head-1", "tree-head-1"),
                _landing_pull_request(1, base_sha="base-main"),
                {"sha": "merge-sha-1"},
                _git_commit(
                    "merge-sha-1",
                    "tree-candidate-1",
                    parents=("base-main", "head-1"),
                ),
                _github_branch(sha="later-base-sha"),
                {"status": "ahead"},
                _github_branch(sha="later-base-sha"),
                {"status": "ahead"},
            )
        )

        GitHubMergeTrainClient(transport=transport).land_batch_candidate(
            landing_plan=landing_plan,
            admission_guard=guard,  # type: ignore[arg-type]
            recorded_at="2026-08-11T03:01:00Z",
        )

        self.assertEqual(len(guard.landed_calls), 1)
        landed_call = guard.landed_calls[0]
        self.assertEqual(landed_call["observed_base_sha"], "later-base-sha")
        self.assertEqual(landed_call["observed_base_tree_sha"], "tree-later-base-sha")
        self.assertIs(landed_call["base_contains_merge_commit"], True)

    def test_missing_post_merge_containment_requires_reconciliation(self) -> None:
        original_plan = _landing_plan()
        landing_plan = MergeTrainBatchLandingPlan.model_validate(
            {
                **original_plan.model_dump(mode="python"),
                "entries": (original_plan.entries[0],),
                "landing_plan_sha256": "",
            }
        )
        guard = _PermissiveMergeAdmissionGuard()
        transport = RecordingMergeTrainGitHubTransport(
            responses=(
                _github_branch(sha="base-main"),
                _git_commit("head-1", "tree-head-1"),
                _landing_pull_request(1, base_sha="base-main"),
                {"sha": "merge-sha-1"},
                _git_commit(
                    "merge-sha-1",
                    "tree-candidate-1",
                    parents=("base-main", "head-1"),
                ),
                _github_branch(sha="rewritten-base-sha"),
                {"status": "diverged"},
            )
        )

        with self.assertRaisesRegex(MergeTrainGitHubStaleHeadError, "does not contain"):
            GitHubMergeTrainClient(transport=transport).land_batch_candidate(
                landing_plan=landing_plan,
                admission_guard=guard,  # type: ignore[arg-type]
                recorded_at="2026-08-11T03:01:00Z",
            )

        self.assertEqual(guard.landed_calls, [])
        self.assertEqual(len(guard.reconcile_required_calls), 1)
        self.assertEqual(
            guard.reconcile_required_calls[0]["reason"],
            "landing_evidence_contradicted",
        )

    def test_land_batch_candidate_rejects_divergence_after_final_merge(self) -> None:
        landing_plan = _landing_plan()
        transport = RecordingMergeTrainGitHubTransport(
            responses=(
                _github_branch(sha="base-main"),
                *_normal_landing_responses(1, "base-main", "merge-sha-1"),
                _github_branch(sha="merge-sha-1"),
                *_normal_landing_responses(2, "merge-sha-1", "merge-sha-2"),
                _github_branch(sha="rewritten-base-sha"),
                {"status": "diverged"},
            )
        )

        with self.assertRaisesRegex(MergeTrainGitHubStaleHeadError, "outside"):
            GitHubMergeTrainClient(transport=transport).land_batch_candidate(
                landing_plan=landing_plan
            )

    def test_cleanup_batch_candidate_ref_deletes_candidate_ref(self) -> None:
        landing_plan = _landing_plan()
        transport = RecordingMergeTrainGitHubTransport()

        deleted = GitHubMergeTrainClient(transport=transport).cleanup_batch_candidate_ref(
            landing_plan=landing_plan
        )

        self.assertTrue(deleted)
        self.assertEqual(
            (transport.requests[-1].method, transport.requests[-1].path),
            ("DELETE", _candidate_ref_path(landing_plan)),
        )

    def test_cleanup_batch_candidate_ref_tolerates_already_deleted_candidate_ref(self) -> None:
        landing_plan = _landing_plan()
        transport = RecordingMergeTrainGitHubTransport(
            responses=(MergeTrainGitHubError("candidate ref missing", status_code=404),)
        )

        deleted = GitHubMergeTrainClient(transport=transport).cleanup_batch_candidate_ref(
            landing_plan=landing_plan
        )

        self.assertFalse(deleted)
        self.assertEqual(
            (transport.requests[-1].method, transport.requests[-1].path),
            ("DELETE", _candidate_ref_path(landing_plan)),
        )

    def test_cleanup_batch_candidate_ref_fails_closed_on_delete_error(self) -> None:
        landing_plan = _landing_plan()
        transport = RecordingMergeTrainGitHubTransport(
            responses=(MergeTrainGitHubError("candidate ref delete failed", status_code=500),)
        )

        with self.assertRaisesRegex(MergeTrainGitHubError, "delete failed"):
            GitHubMergeTrainClient(transport=transport).cleanup_batch_candidate_ref(
                landing_plan=landing_plan
            )

        self.assertEqual(
            (transport.requests[-1].method, transport.requests[-1].path),
            ("DELETE", _candidate_ref_path(landing_plan)),
        )

    def test_land_batch_candidate_skips_persisted_merged_entries_on_retry(self) -> None:
        first_entry = (
            _landing_plan()
            .entries[0]
            .model_copy(update={"status": "merged", "merge_commit_sha": "merge-sha-1"})
        )
        landing_plan = _landing_plan().model_copy(
            update={"entries": (first_entry, _landing_plan().entries[1])}
        )
        transport = RecordingMergeTrainGitHubTransport(
            responses=(
                _github_branch(sha="merge-sha-1"),
                _landing_pull_request(
                    1,
                    base_sha="base-main",
                    state="closed",
                    merged=True,
                    merge_commit_sha="merge-sha-1",
                ),
                _git_commit("head-1", "tree-head-1"),
                _git_commit(
                    "merge-sha-1",
                    "tree-candidate-1",
                    parents=("base-main", "head-1"),
                ),
                _git_commit("base-main", "tree-base"),
                {"status": "identical"},
                _github_branch(sha="merge-sha-1"),
                _git_commit("head-2", "tree-head-2"),
                _landing_pull_request(2, base_sha="merge-sha-1"),
                {"sha": "merge-sha-2"},
                _git_commit(
                    "merge-sha-2",
                    "tree-candidate-2",
                    parents=("merge-sha-1", "head-2"),
                ),
                _github_branch(sha="merge-sha-2"),
                _github_branch(sha="merge-sha-2"),
            )
        )

        landed_plan = GitHubMergeTrainClient(transport=transport).land_batch_candidate(
            landing_plan=landing_plan
        )

        self.assertEqual(
            [entry.merge_commit_sha for entry in landed_plan.entries],
            ["merge-sha-1", "merge-sha-2"],
        )
        self.assertEqual(
            landed_plan.entries[0].recorded_rolling_base_sha,
            "base-main",
        )

    def test_land_batch_candidate_reconciles_all_already_merged_entries(self) -> None:
        first_entry = (
            _landing_plan()
            .entries[0]
            .model_copy(update={"status": "merged", "merge_commit_sha": "merge-sha-1"})
        )
        second_entry = (
            _landing_plan()
            .entries[1]
            .model_copy(update={"status": "merged", "merge_commit_sha": "merge-sha-2"})
        )
        landing_plan = _landing_plan().model_copy(update={"entries": (first_entry, second_entry)})
        transport = RecordingMergeTrainGitHubTransport(
            responses=(
                _github_branch(sha="merge-sha-2"),
                _landing_pull_request(
                    1,
                    base_sha="base-main",
                    state="closed",
                    merged=True,
                    merge_commit_sha="merge-sha-1",
                ),
                _git_commit("head-1", "tree-head-1"),
                _git_commit(
                    "merge-sha-1",
                    "tree-candidate-1",
                    parents=("base-main", "head-1"),
                ),
                _git_commit("base-main", "tree-base"),
                {"status": "ahead"},
                _github_branch(sha="merge-sha-2"),
                _landing_pull_request(
                    2,
                    base_sha="merge-sha-1",
                    state="closed",
                    merged=True,
                    merge_commit_sha="merge-sha-2",
                ),
                _git_commit("head-2", "tree-head-2"),
                _git_commit(
                    "merge-sha-2",
                    "tree-candidate-2",
                    parents=("merge-sha-1", "head-2"),
                ),
                _git_commit("merge-sha-1", "tree-candidate-1"),
                {"status": "identical"},
                _github_branch(sha="merge-sha-2"),
            )
        )

        landed_plan = GitHubMergeTrainClient(transport=transport).land_batch_candidate(
            landing_plan=landing_plan
        )

        self.assertEqual(
            [entry.merge_commit_sha for entry in landed_plan.entries],
            ["merge-sha-1", "merge-sha-2"],
        )
        self.assertTrue(all(entry.landed_head_tree_sha for entry in landed_plan.entries))

    def test_land_batch_candidate_revalidates_persisted_merged_entry_base(self) -> None:
        first_entry = (
            _landing_plan()
            .entries[0]
            .model_copy(update={"status": "merged", "merge_commit_sha": "merge-sha-1"})
        )
        landing_plan = _landing_plan().model_copy(
            update={"entries": (first_entry, _landing_plan().entries[1])}
        )
        transport = RecordingMergeTrainGitHubTransport(
            responses=(
                _github_branch(sha="unexpected-base"),
                _landing_pull_request(
                    1,
                    base_sha="base-main",
                    state="closed",
                    merged=True,
                    merge_commit_sha="merge-sha-1",
                ),
                _git_commit("head-1", "tree-head-1"),
                _git_commit(
                    "merge-sha-1",
                    "tree-candidate-1",
                    parents=("base-main", "head-1"),
                ),
                _git_commit("base-main", "tree-base"),
                {"status": "behind"},
            )
        )

        with self.assertRaisesRegex(MergeTrainGitHubStaleHeadError, "not contained"):
            GitHubMergeTrainClient(transport=transport).land_batch_candidate(
                landing_plan=landing_plan
            )

        self.assertEqual(transport.requests[-1].method, "GET")

    def test_land_batch_candidate_reconciles_partially_persisted_plan_at_final_base(
        self,
    ) -> None:
        first_entry = (
            _landing_plan()
            .entries[0]
            .model_copy(update={"status": "merged", "merge_commit_sha": "merge-sha-1"})
        )
        landing_plan = _landing_plan().model_copy(
            update={"entries": (first_entry, _landing_plan().entries[1])}
        )
        transport = RecordingMergeTrainGitHubTransport(
            responses=(
                _github_branch(sha="merge-sha-2"),
                *_already_merged_responses(1, "base-main", "merge-sha-1", "ahead"),
                _github_branch(sha="merge-sha-2"),
                *_already_merged_responses(2, "merge-sha-1", "merge-sha-2", "identical"),
                _github_branch(sha="merge-sha-2"),
            )
        )

        landed_plan = GitHubMergeTrainClient(transport=transport).land_batch_candidate(
            landing_plan=landing_plan
        )

        self.assertEqual([entry.status for entry in landed_plan.entries], ["merged", "merged"])
        self.assertEqual(
            [entry.merge_commit_sha for entry in landed_plan.entries],
            ["merge-sha-1", "merge-sha-2"],
        )
        self.assertTrue(all(entry.merge_commit_tree_sha for entry in landed_plan.entries))

    def test_land_batch_candidate_recovers_already_merged_pr_after_partial_landing(
        self,
    ) -> None:
        landing_plan = _landing_plan()
        transport = RecordingMergeTrainGitHubTransport(
            responses=(
                _github_branch(sha="merge-sha-1"),
                *_already_merged_responses(1, "base-main", "merge-sha-1", "identical"),
                _github_branch(sha="merge-sha-1"),
                *_normal_landing_responses(2, "merge-sha-1", "merge-sha-2"),
                _github_branch(sha="merge-sha-2"),
            )
        )

        landed_plan = GitHubMergeTrainClient(transport=transport).land_batch_candidate(
            landing_plan=landing_plan
        )

        self.assertEqual([entry.status for entry in landed_plan.entries], ["merged", "merged"])
        self.assertEqual(
            [entry.merge_commit_sha for entry in landed_plan.entries],
            ["merge-sha-1", "merge-sha-2"],
        )
        self.assertEqual(landed_plan.entries[0].recorded_rolling_base_sha, "base-main")

    def test_land_batch_candidate_reconciles_all_planned_entries_already_merged(
        self,
    ) -> None:
        landing_plan = _landing_plan()
        transport = RecordingMergeTrainGitHubTransport(
            responses=(
                _github_branch(sha="merge-sha-2"),
                *_already_merged_responses(1, "base-main", "merge-sha-1", "ahead"),
                _github_branch(sha="merge-sha-2"),
                *_already_merged_responses(2, "merge-sha-1", "merge-sha-2", "identical"),
                _github_branch(sha="merge-sha-2"),
            )
        )

        landed_plan = GitHubMergeTrainClient(transport=transport).land_batch_candidate(
            landing_plan=landing_plan
        )

        self.assertEqual([entry.status for entry in landed_plan.entries], ["merged", "merged"])
        self.assertEqual(
            [entry.merge_commit_sha for entry in landed_plan.entries],
            ["merge-sha-1", "merge-sha-2"],
        )
        self.assertTrue(all(entry.recorded_rolling_base_tree_sha for entry in landed_plan.entries))

    def test_land_batch_candidate_rejects_already_merged_pr_with_different_head(
        self,
    ) -> None:
        landing_plan = _landing_plan()
        transport = RecordingMergeTrainGitHubTransport(
            responses=(
                _github_branch(sha="merge-sha-1"),
                _github_pull_request(
                    1,
                    state="closed",
                    merged=True,
                    head_sha="unexpected-head",
                    merge_commit_sha="merge-sha-1",
                ),
            )
        )

        with self.assertRaisesRegex(MergeTrainGitHubStaleHeadError, "different head SHA"):
            GitHubMergeTrainClient(transport=transport).land_batch_candidate(
                landing_plan=landing_plan
            )

        self.assertEqual([request.method for request in transport.requests], ["GET", "GET"])

    def test_land_batch_candidate_rejects_base_movement_between_prs(self) -> None:
        landing_plan = _landing_plan()
        transport = RecordingMergeTrainGitHubTransport(
            responses=(
                _github_branch(sha="base-main"),
                *_normal_landing_responses(1, "base-main", "merge-sha-1"),
                _github_branch(sha="unexpected-base"),
                _landing_pull_request(2, base_sha="unexpected-base"),
            )
        )

        with self.assertRaisesRegex(MergeTrainGitHubStaleHeadError, "outside"):
            GitHubMergeTrainClient(transport=transport).land_batch_candidate(
                landing_plan=landing_plan
            )

        self.assertEqual(
            [request.method for request in transport.requests],
            ["GET", "GET", "GET", "PUT", "GET", "GET", "GET", "GET"],
        )
        self.assertNotIn("DELETE", [request.method for request in transport.requests])

    def test_land_batch_candidate_rejects_moved_base_branch(self) -> None:
        landing_plan = _landing_plan()
        transport = RecordingMergeTrainGitHubTransport(
            responses=(_github_branch(sha="new-base-main"), _github_pull_request(1))
        )

        with self.assertRaisesRegex(MergeTrainGitHubStaleHeadError, "outside"):
            GitHubMergeTrainClient(transport=transport).land_batch_candidate(
                landing_plan=landing_plan
            )

        self.assertEqual(len(transport.requests), 2)

    def test_repository_must_be_owner_name(self) -> None:
        client = GitHubMergeTrainClient(transport=RecordingMergeTrainGitHubTransport())

        with self.assertRaisesRegex(ValueError, "owner/name"):
            client.add_pull_request_label(
                repository="cbusillo",
                pull_request_number=42,
                label="merge-blocked",
            )

    def test_transport_maps_conflict_to_stale_head_error(self) -> None:
        transport = UrllibMergeTrainGitHubTransport(token="token")
        http_error = HTTPError(
            url="https://api.github.com/repos/cbusillo/repo/pulls/1/merge",
            code=409,
            msg="Conflict",
            hdrs=Message(),
            fp=None,
        )

        with patch("control_plane.merge_train_github.urlopen", side_effect=http_error):
            with self.assertRaises(MergeTrainGitHubStaleHeadError) as caught:
                transport.request(method="PUT", path="/repos/cbusillo/repo/pulls/1/merge")

        self.assertEqual(caught.exception.status_code, 409)


class GitHubMergeTrainSnapshotReaderTests(unittest.TestCase):
    def test_snapshot_reader_builds_pull_request_snapshots_from_base_rooted_graph(self) -> None:
        transport = RecordingMergeTrainGitHubTransport(
            responses=(
                _github_branch(),
                [
                    _github_pull_request(42, mergeable=None, head_ref="feature-root"),
                    _github_pull_request(43, base_ref="feature-root", head_ref="feature-child"),
                    _github_pull_request(44, base_ref="other-base", head_ref="unrelated"),
                ],
                _github_pull_request(42, mergeable=True),
                {"permission": "admin"},
                _combined_status(),
                {"check_runs": [_check_run("completed", "success")]},
                _github_pull_request(43, base_ref="feature-root", head_ref="feature-child"),
                {"permission": "admin"},
                _combined_status(),
                {"check_runs": [_check_run("completed", "success")]},
            )
        )
        reader = GitHubMergeTrainSnapshotReader(transport=transport)

        snapshot = reader.read_merge_train_snapshot(
            repository="cbusillo/sellyouroutboard", base_branch="main"
        )

        self.assertEqual(snapshot.repository, "cbusillo/sellyouroutboard")
        self.assertEqual(snapshot.base_branch, "main")
        self.assertEqual(len(snapshot.pull_requests), 2)
        pull_request = snapshot.pull_requests[0]
        self.assertEqual(pull_request.number, 42)
        self.assertEqual(pull_request.labels, ("ready-to-merge",))
        self.assertEqual(pull_request.actor_id, 1042)
        self.assertEqual(pull_request.actor_role, "repo_admin")
        self.assertEqual(pull_request.head_sha, "head-42")
        self.assertEqual(pull_request.head_ref, "feature-42")
        self.assertEqual(pull_request.head_repository, "cbusillo/sellyouroutboard")
        self.assertEqual(pull_request.base_sha, "base-42")
        self.assertEqual(pull_request.base_ref, "main")
        self.assertEqual(pull_request.base_repository, "cbusillo/sellyouroutboard")
        self.assertEqual(pull_request.mergeable, "mergeable")
        self.assertEqual(pull_request.required_checks_status, "pass")
        self.assertFalse(pull_request.branch_update_required)
        self.assertEqual(snapshot.pull_requests[1].number, 43)
        self.assertEqual(snapshot.pull_requests[1].base_ref, "feature-root")
        self.assertEqual(snapshot.base_sha, "base-main-current")
        self.assertEqual(
            [request.path for request in transport.requests],
            [
                "/repos/cbusillo/sellyouroutboard/branches/main",
                "/repos/cbusillo/sellyouroutboard/pulls?state=open&sort=created&direction=asc&per_page=100&page=1",
                "/repos/cbusillo/sellyouroutboard/pulls/42",
                "/repos/cbusillo/sellyouroutboard/collaborators/cbusillo/permission",
                "/repos/cbusillo/sellyouroutboard/commits/head-42/status?per_page=100&page=1",
                "/repos/cbusillo/sellyouroutboard/commits/head-42/check-runs?per_page=100&page=1",
                "/repos/cbusillo/sellyouroutboard/pulls/43",
                "/repos/cbusillo/sellyouroutboard/collaborators/cbusillo/permission",
                "/repos/cbusillo/sellyouroutboard/commits/head-43/status?per_page=100&page=1",
                "/repos/cbusillo/sellyouroutboard/commits/head-43/check-runs?per_page=100&page=1",
            ],
        )
        self.assertNotIn("/pulls/44", "\n".join(request.path for request in transport.requests))

    def test_snapshot_reader_downgrades_missing_collaborator_permission_to_unknown(
        self,
    ) -> None:
        transport = RecordingMergeTrainGitHubTransport(
            responses=(
                _github_branch(),
                [_github_pull_request(15, author_association="CONTRIBUTOR")],
                _github_pull_request(15, author_association="CONTRIBUTOR"),
                MergeTrainGitHubError("permission not found", status_code=404),
                _combined_status(),
                {"check_runs": [_check_run("completed", "success")]},
            )
        )

        snapshot = GitHubMergeTrainSnapshotReader(transport=transport).read_merge_train_snapshot(
            repository="cbusillo/sellyouroutboard", base_branch="main"
        )

        self.assertEqual(snapshot.pull_requests[0].actor_role, "unknown")

    def test_snapshot_reader_paginates_check_runs_before_computing_status(self) -> None:
        transport = RecordingMergeTrainGitHubTransport(
            responses=(
                _github_branch(),
                [_github_pull_request(16)],
                _github_pull_request(16),
                {"permission": "admin"},
                _combined_status(),
                {
                    "total_count": 101,
                    "check_runs": [_check_run("completed", "success") for _ in range(100)],
                },
                {"total_count": 101, "check_runs": [_check_run("completed", "failure")]},
            )
        )

        snapshot = GitHubMergeTrainSnapshotReader(transport=transport).read_merge_train_snapshot(
            repository="cbusillo/sellyouroutboard", base_branch="main"
        )

        self.assertEqual(snapshot.pull_requests[0].required_checks_status, "fail")
        self.assertEqual(
            [request.path for request in transport.requests if "check-runs" in request.path],
            [
                "/repos/cbusillo/sellyouroutboard/commits/head-16/check-runs?per_page=100&page=1",
                "/repos/cbusillo/sellyouroutboard/commits/head-16/check-runs?per_page=100&page=2",
            ],
        )

    def test_snapshot_reader_paginates_commit_statuses_before_computing_status(self) -> None:
        transport = RecordingMergeTrainGitHubTransport(
            responses=(
                _github_branch(),
                [_github_pull_request(17)],
                _github_pull_request(17),
                {"permission": "admin"},
                {
                    "state": "pending",
                    "total_count": 101,
                    "statuses": [
                        {"context": f"ci-status-{index}", "state": "success"}
                        for index in range(100)
                    ],
                },
                {
                    "state": "failure",
                    "total_count": 101,
                    "statuses": [
                        {
                            "context": "late-failing-status",
                            "state": "failure",
                        }
                    ],
                },
                {"check_runs": [_check_run("completed", "success")]},
            )
        )

        snapshot = GitHubMergeTrainSnapshotReader(transport=transport).read_merge_train_snapshot(
            repository="cbusillo/sellyouroutboard", base_branch="main"
        )

        self.assertEqual(snapshot.pull_requests[0].required_checks_status, "fail")
        self.assertEqual(
            [request.path for request in transport.requests if "/status?" in request.path],
            [
                "/repos/cbusillo/sellyouroutboard/commits/head-17/status?per_page=100&page=1",
                "/repos/cbusillo/sellyouroutboard/commits/head-17/status?per_page=100&page=2",
            ],
        )

    def test_snapshot_reader_paginates_pull_requests(self) -> None:
        first_page = [_github_pull_request(number) for number in range(1, 101)]
        second_page = [_github_pull_request(101)]
        responses: list[object] = [_github_branch(), first_page, second_page]
        for number in range(1, 102):
            responses.extend(
                [
                    _github_pull_request(number),
                    {"permission": "admin"},
                    _combined_status(),
                    {"check_runs": [_check_run("completed", "success")]},
                ]
            )
        transport = RecordingMergeTrainGitHubTransport(responses=tuple(responses))

        snapshot = GitHubMergeTrainSnapshotReader(transport=transport).read_merge_train_snapshot(
            repository="cbusillo/sellyouroutboard", base_branch="main"
        )

        self.assertEqual(len(snapshot.pull_requests), 101)
        self.assertEqual(snapshot.pull_requests[0].number, 1)
        self.assertEqual(snapshot.pull_requests[-1].number, 101)

    def test_snapshot_reader_maps_owner_association_without_permission_lookup(self) -> None:
        transport = RecordingMergeTrainGitHubTransport(
            responses=(
                _github_branch(),
                [_github_pull_request(12, author_association="OWNER")],
                _github_pull_request(12, author_association="OWNER"),
                _combined_status(),
                {"check_runs": [_check_run("completed", "success")]},
            )
        )

        snapshot = GitHubMergeTrainSnapshotReader(transport=transport).read_merge_train_snapshot(
            repository="cbusillo/sellyouroutboard", base_branch="main"
        )

        self.assertEqual(snapshot.pull_requests[0].actor_role, "repo_owner")
        self.assertNotIn("collaborators", "\n".join(request.path for request in transport.requests))

    def test_snapshot_reader_combines_pending_checks_without_mutation(self) -> None:
        transport = RecordingMergeTrainGitHubTransport(
            responses=(
                _github_branch(),
                [_github_pull_request(13)],
                _github_pull_request(13, mergeable=None, mergeable_state="behind"),
                {"permission": "admin"},
                _combined_status(),
                {"check_runs": [_check_run("queued", None)]},
            )
        )

        snapshot = GitHubMergeTrainSnapshotReader(transport=transport).read_merge_train_snapshot(
            repository="cbusillo/sellyouroutboard", base_branch="main"
        )

        pull_request = snapshot.pull_requests[0]
        self.assertEqual(pull_request.mergeable, "unknown")
        self.assertEqual(pull_request.required_checks_status, "pending")
        self.assertTrue(pull_request.branch_update_required)
        self.assertTrue(all(request.method == "GET" for request in transport.requests))

    def test_snapshot_reader_uses_check_runs_when_legacy_statuses_are_absent(self) -> None:
        transport = RecordingMergeTrainGitHubTransport(
            responses=(
                _github_branch(),
                [_github_pull_request(14)],
                _github_pull_request(14),
                {"permission": "admin"},
                _combined_status(statuses=()),
                {"check_runs": [_check_run("completed", "success")]},
            )
        )

        snapshot = GitHubMergeTrainSnapshotReader(transport=transport).read_merge_train_snapshot(
            repository="cbusillo/sellyouroutboard", base_branch="main"
        )

        self.assertEqual(snapshot.pull_requests[0].required_checks_status, "pass")

    def test_snapshot_reader_fails_closed_on_missing_required_shape(self) -> None:
        transport = RecordingMergeTrainGitHubTransport(
            responses=(_github_branch(), [{"number": 1}])
        )

        with self.assertRaisesRegex(MergeTrainGitHubError, "base"):
            GitHubMergeTrainSnapshotReader(transport=transport).read_merge_train_snapshot(
                repository="cbusillo/sellyouroutboard", base_branch="main"
            )


def _github_pull_request(
    number: int,
    *,
    state: str = "open",
    merged: bool = False,
    merge_commit_sha: str | None = None,
    mergeable: bool | None = True,
    mergeable_state: str = "clean",
    author_association: str = "COLLABORATOR",
    head_sha: str | None = None,
    head_ref: str | None = None,
    base_sha: str | None = None,
    base_ref: str = "main",
    repository: str = "cbusillo/sellyouroutboard",
    head_repository: str = "",
    base_repository: str = "",
) -> dict[str, object]:
    normalized_head_repository = head_repository or repository
    normalized_base_repository = base_repository or repository
    return {
        "number": number,
        "html_url": f"https://github.com/cbusillo/sellyouroutboard/pull/{number}",
        "title": f"Pull request {number}",
        "state": state,
        "merged": merged,
        "merge_commit_sha": merge_commit_sha,
        "draft": False,
        "created_at": f"2026-05-08T10:{number % 60:02d}:00Z",
        "labels": [{"name": "ready-to-merge"}],
        "user": {"id": 1000 + number, "login": "cbusillo"},
        "author_association": author_association,
        "head": {
            "sha": head_sha or f"head-{number}",
            "ref": head_ref or f"feature-{number}",
            "repo": {"full_name": normalized_head_repository},
        },
        "base": {
            "sha": base_sha or f"base-{number}",
            "ref": base_ref,
            "repo": {"full_name": normalized_base_repository},
        },
        "mergeable": mergeable,
        "mergeable_state": mergeable_state,
    }


def _landing_pull_request(
    number: int,
    *,
    base_sha: str,
    state: str = "open",
    merged: bool = False,
    merge_commit_sha: str | None = None,
    head_sha: str | None = None,
    base_ref: str = "main",
) -> dict[str, object]:
    return _github_pull_request(
        number,
        state=state,
        merged=merged,
        merge_commit_sha=merge_commit_sha,
        head_sha=head_sha,
        base_sha=base_sha,
        base_ref=base_ref,
        repository="example/merge-train-repo",
    )


def _normal_landing_responses(number: int, parent_sha: str, merge_sha: str) -> tuple[object, ...]:
    return (
        _git_commit(f"head-{number}", f"tree-head-{number}"),
        _landing_pull_request(number, base_sha=parent_sha),
        {"sha": merge_sha},
        _git_commit(
            merge_sha,
            f"tree-candidate-{number}",
            parents=(parent_sha, f"head-{number}"),
        ),
        _github_branch(sha=merge_sha),
    )


def _already_merged_responses(
    number: int, parent_sha: str, merge_sha: str, compare_status: str
) -> tuple[object, ...]:
    parent_tree_sha = "tree-base" if parent_sha == "base-main" else f"tree-candidate-{number - 1}"
    return (
        _landing_pull_request(
            number,
            base_sha=parent_sha,
            state="closed",
            merged=True,
            merge_commit_sha=merge_sha,
        ),
        _git_commit(f"head-{number}", f"tree-head-{number}"),
        _git_commit(
            merge_sha,
            f"tree-candidate-{number}",
            parents=(parent_sha, f"head-{number}"),
        ),
        _git_commit(parent_sha, parent_tree_sha),
        {"status": compare_status},
    )


def _no_op_landing_responses(
    number: int, parent_sha: str, compare_status: str
) -> tuple[object, ...]:
    parent_tree_sha = "tree-base" if parent_sha == "base-main" else f"tree-{parent_sha}"
    return (
        _landing_pull_request(number, base_sha=parent_sha),
        _git_commit(f"head-{number}", f"tree-head-{number}"),
        _git_commit(parent_sha, parent_tree_sha),
        {"status": compare_status},
        {"status": compare_status},
    )


def _structural_observation(
    entry: MergeTrainBatchEntry,
) -> MergeTrainStructuralEntryObservation:
    delta = MergeTrainStructuralDeltaFingerprint(
        head_sha=entry.head_sha,
        head_tree_sha=entry.head_tree_sha,
        changed_paths=(f"src/pr-{entry.pull_request_number}.py",),
        affected_subjects=(),
    )
    return MergeTrainStructuralEntryObservation(
        position=entry.position,
        pull_request_number=entry.pull_request_number,
        head_sha=entry.head_sha,
        head_tree_sha=entry.head_tree_sha,
        reviewed_delta=delta,
        current_delta=delta,
    )


def _github_branch(
    *, sha: str = "base-main-current", tree_sha: str | None = None
) -> dict[str, object]:
    resolved_tree_sha = tree_sha or {
        "base-main": "tree-base",
        "merge-sha-1": "tree-candidate-1",
        "merge-sha-2": "tree-candidate-2",
    }.get(sha, f"tree-{sha}")
    return {"commit": {"sha": sha, "commit": {"tree": {"sha": resolved_tree_sha}}}}


def _git_commit(sha: str, tree_sha: str, *, parents: tuple[str, ...] = ()) -> dict[str, object]:
    return {
        "sha": sha,
        "tree": {"sha": tree_sha},
        "parents": [{"sha": parent} for parent in parents],
    }


def _merge_commit(sha: str, tree_sha: str) -> dict[str, object]:
    return {"sha": sha, "commit": {"tree": {"sha": tree_sha}}}


def _combined_status(
    *,
    state: str = "success",
    statuses: tuple[dict[str, object], ...] | None = None,
) -> dict[str, object]:
    resolved_statuses = (
        statuses if statuses is not None else ({"context": "ci-status", "state": state},)
    )
    return {
        "state": state if resolved_statuses else "pending",
        "total_count": len(resolved_statuses),
        "statuses": list(resolved_statuses),
    }


def _check_run(status: str, conclusion: str | None) -> dict[str, object]:
    payload: dict[str, object] = {"status": status}
    if conclusion is not None:
        payload["conclusion"] = conclusion
    return payload


def _batch_candidate() -> MergeTrainBatchCandidate:
    repository = "example/merge-train-repo"
    base_branch = "main"
    base_sha = "base-main"
    entries = (
        MergeTrainBatchEntry(pull_request_number=1, position=1, head_sha="head-1"),
        MergeTrainBatchEntry(pull_request_number=2, position=2, head_sha="head-2"),
    )
    batch_id = build_merge_train_batch_id(
        repository=repository,
        base_branch=base_branch,
        base_sha=base_sha,
        entry_head_shas=tuple(entry.head_sha for entry in entries),
    )
    return MergeTrainBatchCandidate(
        batch_id=batch_id,
        repository=repository,
        base_branch=base_branch,
        base_sha=base_sha,
        policy_key=f"{repository}:{base_branch}",
        policy_sha256="policy-sha",
        candidate_ref=build_merge_train_batch_candidate_ref(
            repository=repository,
            base_branch=base_branch,
            batch_id=batch_id,
        ),
        entries=entries,
        created_at="2026-05-13T23:00:00Z",
        updated_at="2026-05-13T23:00:00Z",
    )


def _landing_plan() -> MergeTrainBatchLandingPlan:
    candidate = _batch_candidate().model_copy(
        update={"status": "passed", "candidate_sha": "candidate-sha"}
    )
    return build_merge_train_batch_landing_plan(
        candidate=candidate,
        merge_method="merge",
        created_at="2026-05-14T01:10:00Z",
    )


def _candidate_ref_path(landing_plan: MergeTrainBatchLandingPlan) -> str:
    return (
        "/repos/example/merge-train-repo/git/refs/heads/launchplane/train/"
        f"example/merge-train-repo/main/{landing_plan.batch_id}"
    )


if __name__ == "__main__":
    unittest.main()
