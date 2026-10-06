import base64
import unittest
from unittest.mock import patch

from control_plane.merge_train import (
    MergeTrainDryRunSnapshot,
    MergeTrainPullRequestSnapshot,
    build_merge_train_dry_run_result,
    discover_merge_train_stack,
)
from control_plane.merge_train_codeowners import individual_landing_snapshots
from control_plane.merge_train_github import (
    GitHubMergeTrainSnapshotReader,
    MergeTrainGitHubError,
    RecordingMergeTrainGitHubTransport,
)
from control_plane.contracts.merge_train_batch import build_merge_train_batch_candidate
from control_plane.merge_train_controller_run_once import _conflict_probe_queue
from tests.merge_train_policy_fixtures import build_test_merge_train_policy
from tests.support.merge_train import labeled_by


def _pr(
    number: int, *, individual: bool = False, behind: bool = False
) -> MergeTrainPullRequestSnapshot:
    return MergeTrainPullRequestSnapshot(
        number=number,
        created_at=f"2026-10-01T00:00:0{number}Z",
        head_sha=f"head-{number}",
        labels=("ready-to-merge",),
        label_actors=labeled_by(("ready-to-merge",)),
        actor_role="repo_admin",
        base_ref="main",
        head_ref=f"work-{number}",
        head_repository="example/repo",
        base_repository="example/repo",
        mergeable="mergeable",
        required_checks_status="pass",
        branch_update_required=behind,
        requires_individual_landing=individual,
    )


def _owners(content: str) -> dict[str, object]:
    return {"encoding": "base64", "content": base64.b64encode(content.encode()).decode()}


class CodeOwnerLandingTests(unittest.TestCase):
    def test_reader_routes_owned_changes_before_build_and_keeps_original_head(self) -> None:
        prs = (_pr(1), _pr(2), _pr(3), _pr(4))
        transport = RecordingMergeTrainGitHubTransport(
            responses=(
                {"commit": {"sha": "base-sha"}},
                [
                    {
                        "number": pr.number,
                        "head": {"ref": f"work-{pr.number}", "repo": {"full_name": "example/repo"}},
                        "base": {"ref": "main", "repo": {"full_name": "example/repo"}},
                    }
                    for pr in prs
                ],
                _owners("/DIRECTION.md @director\n/.github/CODEOWNERS @director\n"),
                [{"filename": "app.py"}],
                {"head": {"sha": "head-1"}},
                [{"filename": "DIRECTION.md"}],
                {"head": {"sha": "head-2"}},
                [{"filename": ".github/CODEOWNERS"}],
                {"head": {"sha": "head-3"}},
                [{"filename": "other.py"}],
                {"head": {"sha": "head-4"}},
            )
        )
        with (
            patch.object(GitHubMergeTrainSnapshotReader, "_pull_request_snapshot", side_effect=prs),
            patch.object(
                GitHubMergeTrainSnapshotReader, "_with_review_conversations", return_value=prs
            ),
        ):
            snapshot = GitHubMergeTrainSnapshotReader(
                transport=transport
            ).read_merge_train_snapshot(repository="example/repo", base_branch="main")
        self.assertEqual(
            [pr.requires_individual_landing for pr in snapshot.pull_requests],
            [False, True, True, False],
        )
        policy = build_test_merge_train_policy(repository="example/repo")
        remaining = snapshot.pull_requests
        batches = []
        while remaining:
            result = build_merge_train_dry_run_result(
                policy=policy,
                snapshot=snapshot.model_copy(update={"pull_requests": remaining}),
                batch_landing=True,
            )
            candidate = build_merge_train_batch_candidate(
                dry_run_result=result,
                base_sha="base-sha",
                policy_sha256="policy-sha",
                created_at="2026-10-01T00:00:00Z",
            )
            batches.append(tuple(entry.pull_request_number for entry in candidate.entries))
            self.assertEqual(
                candidate.entries[0].head_sha,
                result.selected_pr.head_sha if result.selected_pr else "",
            )
            remaining = tuple(pr for pr in remaining if pr.number not in result.queue_order)
        self.assertEqual(batches, [(1,), (2,), (3,), (4,)])
        self.assertTrue(all(request.method == "GET" for request in transport.requests))

    def test_ordinary_prefix_batches_and_owned_first_keeps_strict_refresh(self) -> None:
        for prs, expected, action in (
            ((_pr(1), _pr(2), _pr(3, individual=True)), (1, 2), "merge"),
            ((_pr(1, individual=True, behind=True), _pr(2)), (1,), "update_branch"),
        ):
            with self.subTest(expected=expected):
                result = build_merge_train_dry_run_result(
                    policy=build_test_merge_train_policy(repository="example/repo"),
                    snapshot=MergeTrainDryRunSnapshot(
                        repository="example/repo", base_branch="main", pull_requests=prs
                    ),
                    batch_landing=True,
                )
                self.assertEqual(result.queue_order, expected)
                self.assertEqual(result.intended_next_action, action)
                self.assertEqual(
                    tuple(pr.number for pr in _conflict_probe_queue(result)),
                    expected if len(expected) > 1 else (),
                )

    def test_owned_rename_origin_and_generic_patterns_route_individually(self) -> None:
        for pattern, file in (
            (
                "/policy/",
                {
                    "filename": "elsewhere.txt",
                    "status": "renamed",
                    "previous_filename": "policy/approval.md",
                },
            ),
            ("*.md", {"filename": "nested/change.md"}),
            ("/docs/**/guide.md", {"filename": "docs/guide.md"}),
            ("/custom.txt", {"filename": "custom.txt"}),
            ("/", {"filename": "app.py"}),
        ):
            with self.subTest(pattern=pattern):
                transport = RecordingMergeTrainGitHubTransport(
                    responses=(_owners(f"{pattern} @team\n"), [file], {"head": {"sha": "head-1"}})
                )
                (pr,) = individual_landing_snapshots(
                    transport=transport,
                    repository_path="example/repo",
                    base_sha="base",
                    pull_requests=(_pr(1),),
                )
                self.assertTrue(pr.requires_individual_landing)

    def test_stacks_with_owned_roots_or_children_leave_original_prs_open(self) -> None:
        for owned_number in (1, 2):
            with self.subTest(owned_number=owned_number):
                prs = (
                    _pr(1, individual=owned_number == 1),
                    _pr(2, individual=owned_number == 2).model_copy(update={"base_ref": "work-1"}),
                )
                result = discover_merge_train_stack(
                    policy=build_test_merge_train_policy(repository="example/repo"),
                    snapshot=MergeTrainDryRunSnapshot(
                        repository="example/repo", base_branch="main", pull_requests=prs
                    ),
                    root_pull_request_number=1,
                )
                self.assertEqual(result.status, "not_stacked")
                self.assertEqual(result.stack_order, (1,))

    def test_incomplete_or_moving_file_evidence_is_individual_without_stopping_other_prs(
        self,
    ) -> None:
        for files, confirmation in (
            ({}, {}),
            ([{"filename": "app.py"}], {"head": {"sha": "new-head"}}),
            ([{"filename": "app.py", "status": "renamed"}], {}),
        ):
            with self.subTest(files=files):
                responses: list[object] = [_owners("/DIRECTION.md @owner"), files]
                if isinstance(files, list) and files[0].get("status") != "renamed":
                    responses.append(confirmation)
                responses.extend(([{"filename": "app.py"}], {"head": {"sha": "head-2"}}))
                prs = individual_landing_snapshots(
                    transport=RecordingMergeTrainGitHubTransport(responses=tuple(responses)),
                    repository_path="example/repo",
                    base_sha="base",
                    pull_requests=(_pr(1), _pr(2)),
                )
                self.assertTrue(prs[0].requires_individual_landing)
                self.assertFalse(prs[1].requires_individual_landing)

    def test_unlabelled_eligible_change_still_lands_individually(self) -> None:
        prs = (_pr(1).model_copy(update={"labels": (), "label_actors": ()}), _pr(2))
        routed = individual_landing_snapshots(
            transport=RecordingMergeTrainGitHubTransport(
                responses=(
                    _owners("/DIRECTION.md @owner"),
                    [{"filename": "DIRECTION.md"}],
                    {"head": {"sha": "head-1"}},
                    [{"filename": "app.py"}],
                    {"head": {"sha": "head-2"}},
                )
            ),
            repository_path="example/repo",
            base_sha="base",
            pull_requests=prs,
        )
        self.assertTrue(routed[0].requires_individual_landing)
        policy = build_test_merge_train_policy(repository="example/repo")
        policy.policies[0].enqueue.label_required = False
        result = build_merge_train_dry_run_result(
            policy=policy,
            snapshot=MergeTrainDryRunSnapshot(
                repository="example/repo", base_branch="main", pull_requests=routed
            ),
            batch_landing=True,
        )
        self.assertEqual(result.queue_order, (1,))
        self.assertEqual(result.intended_next_action, "merge")

    def test_provider_failure_does_not_change_candidate_routing(self) -> None:
        for status in (None, 502, 403):
            with self.subTest(status=status), self.assertRaises(MergeTrainGitHubError):
                individual_landing_snapshots(
                    transport=RecordingMergeTrainGitHubTransport(
                        responses=(
                            _owners("/DIRECTION.md @owner"),
                            MergeTrainGitHubError("provider read failed", status_code=status),
                        )
                    ),
                    repository_path="example/repo",
                    base_sha="base",
                    pull_requests=(_pr(1),),
                )

    def test_missing_ownership_uses_next_location_but_denied_read_stops(self) -> None:
        transport = RecordingMergeTrainGitHubTransport(
            responses=(
                MergeTrainGitHubError("absent", status_code=404),
                _owners("/custom.txt @owner"),
                [{"filename": "custom.txt"}],
                {"head": {"sha": "head-1"}},
            )
        )
        (pr,) = individual_landing_snapshots(
            transport=transport,
            repository_path="example/repo",
            base_sha="base",
            pull_requests=(_pr(1),),
        )
        self.assertTrue(pr.requires_individual_landing)
        self.assertIn("/contents/CODEOWNERS?ref=base", transport.requests[1].path)
        for response in (
            MergeTrainGitHubError("denied", status_code=403),
            {"encoding": "none"},
        ):
            with self.subTest(response=response), self.assertRaises(MergeTrainGitHubError):
                individual_landing_snapshots(
                    transport=RecordingMergeTrainGitHubTransport(responses=(response, {})),
                    repository_path="example/repo",
                    base_sha="base",
                    pull_requests=(_pr(1),),
                )
