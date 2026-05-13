import unittest

from pydantic import ValidationError

from control_plane.contracts.merge_train_batch import MergeTrainBatchCandidate
from control_plane.contracts.merge_train_batch import MergeTrainBatchEntry
from control_plane.contracts.merge_train_batch import build_merge_train_batch_candidate_ref
from control_plane.contracts.merge_train_batch import build_merge_train_batch_id
from control_plane.contracts.merge_train_batch import build_merge_train_batch_landing_plan


class MergeTrainBatchContractTests(unittest.TestCase):
    def test_batch_id_and_candidate_ref_are_deterministic(self) -> None:
        batch_id = build_merge_train_batch_id(
            repository="cbusillo/codex-skills",
            base_branch="main",
            base_sha="base-sha",
            entry_head_shas=("head-a", "head-b"),
        )

        self.assertEqual(
            batch_id,
            build_merge_train_batch_id(
                repository="cbusillo/codex-skills",
                base_branch="main",
                base_sha="base-sha",
                entry_head_shas=("head-a", "head-b"),
            ),
        )
        self.assertTrue(batch_id.startswith("merge-train-batch-cbusillo-codex-skills-main-"))
        self.assertEqual(
            build_merge_train_batch_candidate_ref(
                repository="cbusillo/codex-skills",
                base_branch="main",
                batch_id=batch_id,
            ),
            f"refs/heads/launchplane/train/cbusillo/codex-skills/main/{batch_id}",
        )

    def test_candidate_requires_contiguous_positions(self) -> None:
        with self.assertRaisesRegex(ValidationError, "positions must be contiguous"):
            _candidate(
                entries=(
                    _entry(1, position=1, head_sha="head-a"),
                    _entry(2, position=3, head_sha="head-b"),
                )
            )

    def test_candidate_rejects_duplicate_pull_requests(self) -> None:
        with self.assertRaisesRegex(ValidationError, "PR numbers must be unique"):
            _candidate(
                entries=(
                    _entry(1, position=1, head_sha="head-a"),
                    _entry(1, position=2, head_sha="head-b"),
                )
            )

    def test_landing_plan_requires_passed_candidate(self) -> None:
        candidate = _candidate(status="ready_for_checks", candidate_sha="candidate-sha")

        with self.assertRaisesRegex(ValueError, "requires passed candidate"):
            build_merge_train_batch_landing_plan(
                candidate=candidate,
                merge_method="merge",
                created_at="2026-05-13T23:00:00Z",
            )

    def test_landing_plan_preserves_pr_native_merge_invariants(self) -> None:
        candidate = _candidate(status="passed", candidate_sha="candidate-sha")

        plan = build_merge_train_batch_landing_plan(
            candidate=candidate,
            merge_method="merge",
            created_at="2026-05-13T23:00:00Z",
        )

        self.assertTrue(plan.plan_id.startswith(f"merge-train-landing-plan-{candidate.batch_id}-"))
        self.assertEqual(plan.batch_id, candidate.batch_id)
        self.assertEqual(plan.candidate_sha, "candidate-sha")
        self.assertEqual(
            [entry.pull_request_number for entry in plan.entries],
            [1, 2],
        )
        self.assertEqual(
            [entry.expected_head_sha for entry in plan.entries],
            ["head-a", "head-b"],
        )
        self.assertEqual({entry.expected_base_sha for entry in plan.entries}, {"base-sha"})
        self.assertEqual({entry.merge_method for entry in plan.entries}, {"merge"})


def _candidate(
    *,
    status: str = "planned",
    candidate_sha: str = "",
    entries: tuple[MergeTrainBatchEntry, ...] | None = None,
) -> MergeTrainBatchCandidate:
    resolved_entries = entries or (
        _entry(1, position=1, head_sha="head-a"),
        _entry(2, position=2, head_sha="head-b"),
    )
    batch_id = build_merge_train_batch_id(
        repository="cbusillo/codex-skills",
        base_branch="main",
        base_sha="base-sha",
        entry_head_shas=tuple(entry.head_sha for entry in resolved_entries),
    )
    return MergeTrainBatchCandidate(
        batch_id=batch_id,
        repository="cbusillo/codex-skills",
        base_branch="main",
        base_sha="base-sha",
        policy_key="cbusillo/codex-skills:main",
        policy_sha256="policy-sha",
        candidate_ref=build_merge_train_batch_candidate_ref(
            repository="cbusillo/codex-skills",
            base_branch="main",
            batch_id=batch_id,
        ),
        candidate_sha=candidate_sha,
        status=status,  # type: ignore[arg-type]
        entries=resolved_entries,
        created_at="2026-05-13T23:00:00Z",
        updated_at="2026-05-13T23:00:00Z",
    )


def _entry(pull_request_number: int, *, position: int, head_sha: str) -> MergeTrainBatchEntry:
    return MergeTrainBatchEntry(
        pull_request_number=pull_request_number,
        position=position,
        head_sha=head_sha,
        title=f"PR {pull_request_number}",
        url=f"https://github.com/cbusillo/codex-skills/pull/{pull_request_number}",
    )


if __name__ == "__main__":
    unittest.main()
