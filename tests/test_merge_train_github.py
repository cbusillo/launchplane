import unittest
from email.message import Message
from unittest.mock import patch
from urllib.error import HTTPError

from control_plane.contracts.merge_train_batch import MergeTrainBatchCandidate
from control_plane.contracts.merge_train_batch import MergeTrainBatchEntry
from control_plane.contracts.merge_train_batch import build_merge_train_batch_candidate_ref
from control_plane.contracts.merge_train_batch import build_merge_train_batch_id
from control_plane.merge_train_github import GitHubMergeTrainClient
from control_plane.merge_train_github import GitHubMergeTrainSnapshotReader
from control_plane.merge_train_github import MergeTrainGitHubError
from control_plane.merge_train_github import MergeTrainGitHubStaleHeadError
from control_plane.merge_train_github import RecordingMergeTrainGitHubTransport
from control_plane.merge_train_github import UrllibMergeTrainGitHubTransport


class GitHubMergeTrainClientTests(unittest.TestCase):
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

    def test_build_batch_candidate_creates_ref_and_merges_heads_in_order(self) -> None:
        candidate = _batch_candidate()
        transport = RecordingMergeTrainGitHubTransport(
            responses=(
                {"ref": candidate.candidate_ref, "object": {"sha": "base-main"}},
                {"sha": "candidate-after-1"},
                {"sha": "candidate-after-2"},
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
                    "POST",
                    "/repos/example/merge-train-repo/merges",
                    {
                        "base": "launchplane/train/example/merge-train-repo/main/"
                        f"{candidate.batch_id}",
                        "head": "head-2",
                        "commit_message": f"Launchplane merge train {candidate.batch_id}: merge PR #2",
                    },
                ),
            ],
        )

    def test_build_batch_candidate_resets_existing_ref(self) -> None:
        candidate = _batch_candidate()
        transport = RecordingMergeTrainGitHubTransport(
            responses=(
                MergeTrainGitHubError("reference exists", status_code=409),
                {"ref": candidate.candidate_ref, "object": {"sha": "base-main"}},
                {"sha": "candidate-after-1"},
                {"sha": "candidate-after-2"},
            )
        )

        GitHubMergeTrainClient(transport=transport).build_batch_candidate(candidate=candidate)

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
                {"sha": "candidate-after-1"},
                MergeTrainGitHubStaleHeadError("conflict", status_code=409),
            )
        )

        with self.assertRaises(MergeTrainGitHubStaleHeadError):
            GitHubMergeTrainClient(transport=transport).build_batch_candidate(candidate=candidate)

        self.assertEqual(
            [request.method for request in transport.requests], ["POST", "POST", "POST"]
        )

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
    def test_snapshot_reader_builds_pull_request_snapshots_from_github(self) -> None:
        transport = RecordingMergeTrainGitHubTransport(
            responses=(
                [
                    _github_pull_request(42, mergeable=None),
                ],
                _github_pull_request(42, mergeable=True),
                {"permission": "admin"},
                {"state": "success"},
                {"check_runs": [_check_run("completed", "success")]},
            )
        )
        reader = GitHubMergeTrainSnapshotReader(transport=transport)

        snapshot = reader.read_merge_train_snapshot(
            repository="cbusillo/sellyouroutboard", base_branch="main"
        )

        self.assertEqual(snapshot.repository, "cbusillo/sellyouroutboard")
        self.assertEqual(snapshot.base_branch, "main")
        self.assertEqual(len(snapshot.pull_requests), 1)
        pull_request = snapshot.pull_requests[0]
        self.assertEqual(pull_request.number, 42)
        self.assertEqual(pull_request.labels, ("ready-to-merge",))
        self.assertEqual(pull_request.actor_role, "repo_admin")
        self.assertEqual(pull_request.head_sha, "head-42")
        self.assertEqual(pull_request.base_sha, "base-42")
        self.assertEqual(pull_request.mergeable, "mergeable")
        self.assertEqual(pull_request.required_checks_status, "pass")
        self.assertFalse(pull_request.branch_update_required)
        self.assertEqual(
            [request.path for request in transport.requests],
            [
                "/repos/cbusillo/sellyouroutboard/pulls?state=open&base=main&sort=created&direction=asc&per_page=100&page=1",
                "/repos/cbusillo/sellyouroutboard/pulls/42",
                "/repos/cbusillo/sellyouroutboard/collaborators/cbusillo/permission",
                "/repos/cbusillo/sellyouroutboard/commits/head-42/status",
                "/repos/cbusillo/sellyouroutboard/commits/head-42/check-runs?per_page=100&page=1",
            ],
        )

    def test_snapshot_reader_downgrades_missing_collaborator_permission_to_unknown(
        self,
    ) -> None:
        transport = RecordingMergeTrainGitHubTransport(
            responses=(
                [_github_pull_request(15, author_association="CONTRIBUTOR")],
                _github_pull_request(15, author_association="CONTRIBUTOR"),
                MergeTrainGitHubError("permission not found", status_code=404),
                {"state": "success"},
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
                [_github_pull_request(16)],
                _github_pull_request(16),
                {"permission": "admin"},
                {"state": "success"},
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

    def test_snapshot_reader_paginates_pull_requests(self) -> None:
        first_page = [_github_pull_request(number) for number in range(1, 101)]
        second_page = [_github_pull_request(101)]
        responses: list[object] = [first_page, second_page]
        for number in range(1, 102):
            responses.extend(
                [
                    _github_pull_request(number),
                    {"permission": "admin"},
                    {"state": "success"},
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
                [_github_pull_request(12, author_association="OWNER")],
                _github_pull_request(12, author_association="OWNER"),
                {"state": "success"},
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
                [_github_pull_request(13)],
                _github_pull_request(13, mergeable=None, mergeable_state="behind"),
                {"permission": "admin"},
                {"state": "success"},
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
                [_github_pull_request(14)],
                _github_pull_request(14),
                {"permission": "admin"},
                {"state": "pending", "total_count": 0},
                {"check_runs": [_check_run("completed", "success")]},
            )
        )

        snapshot = GitHubMergeTrainSnapshotReader(transport=transport).read_merge_train_snapshot(
            repository="cbusillo/sellyouroutboard", base_branch="main"
        )

        self.assertEqual(snapshot.pull_requests[0].required_checks_status, "pass")

    def test_snapshot_reader_fails_closed_on_missing_required_shape(self) -> None:
        transport = RecordingMergeTrainGitHubTransport(responses=([{"number": 1}],))

        with self.assertRaisesRegex(MergeTrainGitHubError, "head"):
            GitHubMergeTrainSnapshotReader(transport=transport).read_merge_train_snapshot(
                repository="cbusillo/sellyouroutboard", base_branch="main"
            )


def _github_pull_request(
    number: int,
    *,
    mergeable: bool | None = True,
    mergeable_state: str = "clean",
    author_association: str = "COLLABORATOR",
) -> dict[str, object]:
    return {
        "number": number,
        "html_url": f"https://github.com/cbusillo/sellyouroutboard/pull/{number}",
        "title": f"Pull request {number}",
        "state": "open",
        "draft": False,
        "created_at": f"2026-05-08T10:{number % 60:02d}:00Z",
        "labels": [{"name": "ready-to-merge"}],
        "user": {"login": "cbusillo"},
        "author_association": author_association,
        "head": {"sha": f"head-{number}"},
        "base": {"sha": f"base-{number}"},
        "mergeable": mergeable,
        "mergeable_state": mergeable_state,
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


if __name__ == "__main__":
    unittest.main()
