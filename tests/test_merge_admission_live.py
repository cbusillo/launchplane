import unittest

from control_plane.merge_admission import MergeAdmissionDeniedError
from control_plane.merge_admission_live import LiveMergeAdmissionEvaluator
from control_plane.merge_train import (
    MergeTrainDryRunSnapshot,
    MergeTrainPullRequestSnapshot,
)
from control_plane.merge_train_github import RecordingMergeTrainGitHubTransport
from control_plane.tenant_admission_controller import TenantAdmissionControllerGitHubClient
from tests.merge_train_policy_fixtures import build_test_merge_train_policy_record
from tests.test_merge_admission_records import _guard_records
from tests.test_merge_readiness import BASE_SHA, HEAD_SHA, REPOSITORY


class _StaticSnapshotReader:
    def __init__(self, snapshot: MergeTrainDryRunSnapshot) -> None:
        self.snapshot = snapshot
        self.read_count = 0

    def read_merge_train_snapshot(
        self,
        *,
        repository: str,
        base_branch: str,
    ) -> MergeTrainDryRunSnapshot:
        self.read_count += 1
        if repository != self.snapshot.repository or base_branch != self.snapshot.base_branch:
            raise AssertionError("unexpected merge queue scope")
        return self.snapshot


class _UnusedRepositoryEvidenceProvider:
    def resolve(self, target: object) -> object:
        raise AssertionError(f"queue drift should fail before repository evidence: {target}")


def _queued_pull_request(
    *,
    number: int,
    head_sha: str,
    created_at: str,
) -> MergeTrainPullRequestSnapshot:
    return MergeTrainPullRequestSnapshot(
        number=number,
        created_at=created_at,
        labels=("ready-to-merge",),
        actor_role="repo_owner",
        head_sha=head_sha,
        base_sha=BASE_SHA,
        base_ref="main",
        mergeable="mergeable",
        required_checks_status="pass",
    )


class LiveMergeAdmissionEvaluatorTests(unittest.TestCase):
    def test_live_queue_is_rediscovered_and_inserted_pr_refuses_admission(self) -> None:
        candidate_record, landing_record, controller_state, _ = _guard_records()
        snapshot_reader = _StaticSnapshotReader(
            MergeTrainDryRunSnapshot(
                repository=REPOSITORY,
                base_branch="main",
                base_sha=BASE_SHA,
                pull_requests=(
                    _queued_pull_request(
                        number=2082,
                        head_sha="d" * 40,
                        created_at="2026-08-11T02:59:00Z",
                    ),
                    _queued_pull_request(
                        number=2083,
                        head_sha=HEAD_SHA,
                        created_at="2026-08-11T03:00:00Z",
                    ),
                ),
            )
        )
        policy_reads = 0

        def read_policy():  # type: ignore[no-untyped-def]
            nonlocal policy_reads
            policy_reads += 1
            return build_test_merge_train_policy_record(repository=REPOSITORY)

        evaluator = LiveMergeAdmissionEvaluator(
            store=object(),
            repository_evidence_provider=_UnusedRepositoryEvidenceProvider(),  # type: ignore[arg-type]
            technical_check_client=TenantAdmissionControllerGitHubClient(
                transport=RecordingMergeTrainGitHubTransport()
            ),
            policy_record_provider=read_policy,
            snapshot_reader=snapshot_reader,
        )
        entry = landing_record.landing_plan.entries[0]

        for _ in range(2):
            with self.assertRaisesRegex(MergeAdmissionDeniedError, "Live merge queue"):
                evaluator.evaluate(
                    candidate_record=candidate_record,
                    landing_plan_record=landing_record,
                    entry=entry,
                    observed_base_sha=BASE_SHA,
                    observed_base_tree_sha="4" * 40,
                    observed_head_sha=HEAD_SHA,
                    observed_head_tree_sha="2" * 40,
                    controller_state=controller_state,
                    stack_collapse_record=None,
                    evaluated_at="2026-08-11T03:01:00Z",
                )

        self.assertEqual(policy_reads, 2)
        self.assertEqual(snapshot_reader.read_count, 2)

    def test_active_policy_removal_is_rediscovered_and_refuses_admission(self) -> None:
        candidate_record, landing_record, controller_state, _ = _guard_records()
        snapshot_reader = _StaticSnapshotReader(
            MergeTrainDryRunSnapshot(
                repository=REPOSITORY,
                base_branch="main",
                base_sha=BASE_SHA,
                pull_requests=(
                    _queued_pull_request(
                        number=2083,
                        head_sha=HEAD_SHA,
                        created_at="2026-08-11T03:00:00Z",
                    ),
                ),
            )
        )
        evaluator = LiveMergeAdmissionEvaluator(
            store=object(),
            repository_evidence_provider=_UnusedRepositoryEvidenceProvider(),  # type: ignore[arg-type]
            technical_check_client=TenantAdmissionControllerGitHubClient(
                transport=RecordingMergeTrainGitHubTransport()
            ),
            policy_record_provider=lambda: build_test_merge_train_policy_record(
                repository="example/other-repository"
            ),
            snapshot_reader=snapshot_reader,
        )

        with self.assertRaisesRegex(MergeAdmissionDeniedError, "Active merge-train policy"):
            evaluator.evaluate(
                candidate_record=candidate_record,
                landing_plan_record=landing_record,
                entry=landing_record.landing_plan.entries[0],
                observed_base_sha=BASE_SHA,
                observed_base_tree_sha="4" * 40,
                observed_head_sha=HEAD_SHA,
                observed_head_tree_sha="2" * 40,
                controller_state=controller_state,
                stack_collapse_record=None,
                evaluated_at="2026-08-11T03:01:00Z",
            )

        self.assertEqual(snapshot_reader.read_count, 1)


if __name__ == "__main__":
    unittest.main()
