import unittest

from control_plane.contracts.merge_train_stack_collapse import (
    MergeTrainStackCollapsePlan,
    build_merge_train_stack_collapse_id,
    build_merge_train_stack_collapse_plan,
    build_merge_train_stack_collapse_plan_record,
    execute_merge_train_stack_collapse_plan,
    reconcile_merge_train_stack_children_after_root_landing,
)
from control_plane.merge_train import (
    MergeTrainDryRunSnapshot,
    MergeTrainPullRequestSnapshot,
    discover_merge_train_stack,
)


class MergeTrainStackCollapseContractTests(unittest.TestCase):
    def test_collapse_id_is_deterministic(self) -> None:
        first = build_merge_train_stack_collapse_id(
            repository="example/merge-train-repo",
            base_branch="main",
            root_pull_request_number=10,
            entry_head_shas=("head-10", "head-11"),
        )
        second = build_merge_train_stack_collapse_id(
            repository="example/merge-train-repo",
            base_branch="main",
            root_pull_request_number=10,
            entry_head_shas=("head-10", "head-11"),
        )

        self.assertEqual(first, second)
        self.assertTrue(first.startswith("merge-train-stack-collapse-example-merge-train-repo-main-"))

    def test_plan_uses_root_ready_to_merge_intent_and_leaf_to_root_mutations(self) -> None:
        discovery_result = discover_merge_train_stack(
            snapshot=MergeTrainDryRunSnapshot(
                repository="example/merge-train-repo",
                base_branch="main",
                pull_requests=(
                    _pull_request(10, head_ref="feature/root", base_ref="main"),
                    _pull_request(11, head_ref="feature/middle", base_ref="feature/root"),
                    _pull_request(12, head_ref="feature/leaf", base_ref="feature/middle"),
                ),
            ),
            root_pull_request_number=10,
        )

        plan = build_merge_train_stack_collapse_plan(
            discovery_result=discovery_result,
            policy_key="example/merge-train-repo:main",
            policy_sha256="policy-sha",
            created_at="2026-05-14T13:30:00Z",
        )

        self.assertEqual(plan.intent_source, "root_ready_to_merge")
        self.assertEqual(plan.status, "planned")
        self.assertEqual(plan.root_pull_request_number, 10)
        self.assertEqual(plan.root_initial_head_sha, "head-10")
        self.assertEqual([entry.pull_request_number for entry in plan.entries], [10, 11, 12])
        self.assertEqual(
            [
                (mutation.child_pull_request_number, mutation.parent_pull_request_number)
                for mutation in plan.mutations
            ],
            [(12, 11), (11, 10)],
        )
        self.assertEqual(plan.mutations[0].expected_parent_head_sha, "head-11")
        self.assertEqual(plan.mutations[1].expected_parent_head_sha, "head-10")

    def test_plan_requires_a_ready_stack_discovery_result(self) -> None:
        discovery_result = discover_merge_train_stack(
            snapshot=MergeTrainDryRunSnapshot(
                repository="example/merge-train-repo",
                base_branch="main",
                pull_requests=(
                    _pull_request(20, head_ref="feature/root", base_ref="main"),
                ),
            ),
            root_pull_request_number=20,
        )

        with self.assertRaisesRegex(ValueError, "requires a discovered stack"):
            build_merge_train_stack_collapse_plan(
                discovery_result=discovery_result,
                policy_key="example/merge-train-repo:main",
                policy_sha256="policy-sha",
                created_at="2026-05-14T13:30:00Z",
            )

    def test_plan_record_id_is_deterministic(self) -> None:
        plan = build_merge_train_stack_collapse_plan(
            discovery_result=discover_merge_train_stack(
                snapshot=MergeTrainDryRunSnapshot(
                    repository="example/merge-train-repo",
                    base_branch="main",
                    pull_requests=(
                        _pull_request(30, head_ref="feature/root", base_ref="main"),
                        _pull_request(31, head_ref="feature/child", base_ref="feature/root"),
                    ),
                ),
                root_pull_request_number=30,
            ),
            policy_key="example/merge-train-repo:main",
            policy_sha256="policy-sha",
            created_at="2026-05-14T13:30:00Z",
        )

        first = build_merge_train_stack_collapse_plan_record(
            plan=plan,
            source="test",
            updated_at="2026-05-14T13:31:00Z",
        )
        second = build_merge_train_stack_collapse_plan_record(
            plan=plan,
            source="test",
            updated_at="2026-05-14T13:31:00Z",
        )

        self.assertEqual(first.record_id, second.record_id)
        self.assertEqual(first.plan.collapse_id, plan.collapse_id)
        self.assertTrue(first.record_id.startswith("merge-train-stack-collapse-plan-"))

    def test_plan_record_id_canonicalizes_equivalent_utc_timestamps(self) -> None:
        plan = build_merge_train_stack_collapse_plan(
            discovery_result=discover_merge_train_stack(
                snapshot=MergeTrainDryRunSnapshot(
                    repository="example/merge-train-repo",
                    base_branch="main",
                    pull_requests=(
                        _pull_request(40, head_ref="feature/root", base_ref="main"),
                        _pull_request(41, head_ref="feature/child", base_ref="feature/root"),
                    ),
                ),
                root_pull_request_number=40,
            ),
            policy_key="example/merge-train-repo:main",
            policy_sha256="policy-sha",
            created_at="2026-05-14T13:30:00Z",
        )

        zulu_record = build_merge_train_stack_collapse_plan_record(
            plan=plan,
            source="test",
            updated_at="2026-05-14T13:31:00Z",
        )
        offset_record = build_merge_train_stack_collapse_plan_record(
            plan=plan,
            source="test",
            updated_at="2026-05-14T09:31:00-04:00",
        )

        self.assertEqual(zulu_record.record_id, offset_record.record_id)
        self.assertTrue(
            zulu_record.record_id.startswith(
                "merge-train-stack-collapse-plan-20260514T133100Z-"
            )
        )

    def test_execute_plan_mutates_each_child_into_its_parent_branch(self) -> None:
        plan = _collapse_plan()
        branch_client = _RecordingStackCollapseBranchClient(
            merge_commit_shas=("merge-32-31", "merge-31-30")
        )

        executed_plan = execute_merge_train_stack_collapse_plan(
            plan=plan,
            branch_client=branch_client,
            updated_at="2026-05-14T13:45:00Z",
        )

        self.assertEqual(executed_plan.status, "waiting_for_root_checks")
        self.assertEqual(
            [mutation.status for mutation in executed_plan.mutations],
            ["mutated", "mutated"],
        )
        self.assertEqual(
            [mutation.merge_commit_sha for mutation in executed_plan.mutations],
            ["merge-32-31", "merge-31-30"],
        )
        self.assertEqual(
            [
                disposition.expected_head_sha
                for disposition in executed_plan.child_dispositions
            ],
            ["merge-32-31", "head-32"],
        )
        self.assertEqual(
            branch_client.requests,
            [
                {
                    "repository": "example/merge-train-repo",
                    "child_head_sha": "head-32",
                    "expected_parent_head_sha": "head-31",
                    "parent_head_ref": "feature/child",
                    "collapse_id": plan.collapse_id,
                    "child_pull_request_number": 32,
                    "parent_pull_request_number": 31,
                },
                {
                    "repository": "example/merge-train-repo",
                    "child_head_sha": "merge-32-31",
                    "expected_parent_head_sha": "head-30",
                    "parent_head_ref": "feature/root",
                    "collapse_id": plan.collapse_id,
                    "child_pull_request_number": 31,
                    "parent_pull_request_number": 30,
                },
            ],
        )

    def test_execute_plan_blocks_at_first_failed_mutation(self) -> None:
        plan = _collapse_plan()
        branch_client = _RecordingStackCollapseBranchClient(
            merge_commit_shas=("merge-31-30",),
            failure=RuntimeError("parent branch moved"),
        )

        executed_plan = execute_merge_train_stack_collapse_plan(
            plan=plan,
            branch_client=branch_client,
            updated_at="2026-05-14T13:45:00Z",
        )

        self.assertEqual(executed_plan.status, "blocked")
        self.assertEqual(
            [mutation.status for mutation in executed_plan.mutations],
            ["mutated", "blocked"],
        )
        self.assertEqual(executed_plan.mutations[1].detail, "parent branch moved")

    def test_reconcile_children_after_root_landing_comments_labels_and_closes(self) -> None:
        plan = execute_merge_train_stack_collapse_plan(
            plan=_collapse_plan(),
            branch_client=_RecordingStackCollapseBranchClient(
                merge_commit_shas=("merge-32-31", "merge-31-30")
            ),
            updated_at="2026-05-14T13:45:00Z",
        )
        disposition_client = _RecordingStackChildDispositionClient()

        reconciled_plan = reconcile_merge_train_stack_children_after_root_landing(
            plan=plan,
            disposition_client=disposition_client,
            root_merge_commit_sha="root-merge-sha",
            label="stack-landed",
            updated_at="2026-05-14T13:50:00Z",
        )

        self.assertEqual(reconciled_plan.status, "ready_for_train")
        self.assertEqual(
            [disposition.status for disposition in reconciled_plan.child_dispositions],
            ["closed", "closed"],
        )
        self.assertEqual(
            [disposition.comment_url for disposition in reconciled_plan.child_dispositions],
            [
                "https://github.com/example/merge-train-repo/pull/31#issuecomment-1",
                "https://github.com/example/merge-train-repo/pull/32#issuecomment-2",
            ],
        )
        self.assertEqual(
            disposition_client.closed,
            [(31, "merge-32-31"), (32, "head-32")],
        )
        self.assertEqual(disposition_client.labels, [(31, "stack-landed"), (32, "stack-landed")])
        self.assertIn("root PR #30", disposition_client.comments[0][1])

    def test_reconcile_children_blocks_at_first_failed_close(self) -> None:
        plan = execute_merge_train_stack_collapse_plan(
            plan=_collapse_plan(),
            branch_client=_RecordingStackCollapseBranchClient(
                merge_commit_shas=("merge-32-31", "merge-31-30")
            ),
            updated_at="2026-05-14T13:45:00Z",
        )
        disposition_client = _RecordingStackChildDispositionClient(
            failure=RuntimeError("child head moved")
        )

        reconciled_plan = reconcile_merge_train_stack_children_after_root_landing(
            plan=plan,
            disposition_client=disposition_client,
            root_merge_commit_sha="root-merge-sha",
            label="stack-landed",
            updated_at="2026-05-14T13:50:00Z",
        )

        self.assertEqual(reconciled_plan.status, "blocked")
        self.assertEqual(
            [disposition.status for disposition in reconciled_plan.child_dispositions],
            ["blocked", "planned"],
        )
        self.assertEqual(reconciled_plan.child_dispositions[0].detail, "child head moved")


def _pull_request(
    number: int,
    *,
    head_ref: str,
    base_ref: str,
) -> MergeTrainPullRequestSnapshot:
    return MergeTrainPullRequestSnapshot(
        number=number,
        url=f"https://github.com/example/merge-train-repo/pull/{number}",
        title=f"Pull request {number}",
        created_at=f"2026-05-14T13:{number % 60:02d}:00Z",
        labels=("ready-to-merge",),
        actor_role="repo_admin",
        head_sha=f"head-{number}",
        head_ref=head_ref,
        head_repository="example/merge-train-repo",
        base_sha=f"base-{number}",
        base_ref=base_ref,
        base_repository="example/merge-train-repo",
        mergeable="mergeable",
        required_checks_status="pass",
    )


def _collapse_plan() -> MergeTrainStackCollapsePlan:
    return build_merge_train_stack_collapse_plan(
        discovery_result=discover_merge_train_stack(
            snapshot=MergeTrainDryRunSnapshot(
                repository="example/merge-train-repo",
                base_branch="main",
                pull_requests=(
                    _pull_request(30, head_ref="feature/root", base_ref="main"),
                    _pull_request(31, head_ref="feature/child", base_ref="feature/root"),
                    _pull_request(32, head_ref="feature/leaf", base_ref="feature/child"),
                ),
            ),
            root_pull_request_number=30,
        ),
        policy_key="example/merge-train-repo:main",
        policy_sha256="policy-sha",
        created_at="2026-05-14T13:30:00Z",
    )


class _RecordingStackCollapseBranchClient:
    def __init__(
        self, *, merge_commit_shas: tuple[str, ...], failure: Exception | None = None
    ) -> None:
        self.merge_commit_shas = list(merge_commit_shas)
        self.failure = failure
        self.requests: list[dict[str, object]] = []

    def merge_stack_child_into_parent(
        self,
        *,
        repository: str,
        child_head_sha: str,
        expected_parent_head_sha: str,
        parent_head_ref: str,
        collapse_id: str,
        child_pull_request_number: int,
        parent_pull_request_number: int,
    ) -> str:
        self.requests.append(
            {
                "repository": repository,
                "child_head_sha": child_head_sha,
                "expected_parent_head_sha": expected_parent_head_sha,
                "parent_head_ref": parent_head_ref,
                "collapse_id": collapse_id,
                "child_pull_request_number": child_pull_request_number,
                "parent_pull_request_number": parent_pull_request_number,
            }
        )
        if self.merge_commit_shas:
            return self.merge_commit_shas.pop(0)
        if self.failure is not None:
            raise self.failure
        raise RuntimeError("unexpected stack collapse branch mutation")


class _RecordingStackChildDispositionClient:
    def __init__(self, *, failure: Exception | None = None) -> None:
        self.failure = failure
        self.comments: list[tuple[int, str]] = []
        self.labels: list[tuple[int, str]] = []
        self.closed: list[tuple[int, str]] = []

    def comment_pull_request(
        self, *, repository: str, pull_request_number: int, body: str
    ) -> str:
        self.comments.append((pull_request_number, body))
        return (
            f"https://github.com/{repository}/pull/{pull_request_number}"
            f"#issuecomment-{len(self.comments)}"
        )

    def add_pull_request_label(
        self, *, repository: str, pull_request_number: int, label: str
    ) -> None:
        self.labels.append((pull_request_number, label))

    def close_pull_request(
        self, *, repository: str, pull_request_number: int, expected_head_sha: str
    ) -> None:
        if self.failure is not None:
            raise self.failure
        self.closed.append((pull_request_number, expected_head_sha))


if __name__ == "__main__":
    unittest.main()
