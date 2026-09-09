import unittest
from unittest.mock import Mock

from control_plane.contracts.merge_train_batch import (
    MergeTrainBatchCandidate,
    MergeTrainBatchEntry,
    build_ordinary_merge_train_candidate_ref,
)
from control_plane.contracts.merge_train_effect import (
    CandidateHeadMergeOutcome,
    MergeTrainSemanticEffectExecutor,
)
from control_plane.contracts.ordinary_agent import OrdinaryAgentPullRequest
from control_plane.contracts.ordinary_agent_session_lifecycle import (
    OrdinaryAgentFiniteRequestRecord,
)
from control_plane.contracts.ordinary_agent_snapshot import (
    OrdinaryAgentCommitIdentity,
    OrdinaryAgentMergeTrainSnapshotResult,
    OrdinaryAgentProtectionEvidence,
    OrdinaryAgentProviderRequestCounts,
    OrdinaryAgentPullRequestHeadIdentity,
)
from control_plane.merge_train import MergeTrainDryRunSnapshot, MergeTrainPullRequestSnapshot
from control_plane.merge_train_github import MergeTrainGitHubStaleHeadError
from control_plane.ordinary_agent_merge_train_client import OrdinaryAgentMergeTrainClient
from tests.support.ordinary_agent_lifecycle import TARGET


class OrdinaryAgentMergeTrainClientTests(unittest.TestCase):
    def test_restart_resumes_next_entry_without_reset_or_duplicate_merge(self) -> None:
        now = "2026-01-01T00:00:00Z"
        request = OrdinaryAgentFiniteRequestRecord(
            request_id="job-one",
            idempotency_key="job-one",
            principal_id="agent_one",
            session_id="session_one",
            lease_id="lease_one",
            target=TARGET,
            base_sha="a" * 40,
            pull_requests=(
                OrdinaryAgentPullRequest(number=12, head_sha="b" * 40),
                OrdinaryAgentPullRequest(number=13, head_sha="c" * 40),
            ),
            permitted_stack_edit_pull_requests=(),
            refresh_allowance_total=0,
            admitted_at=10,
            expires_at=100,
        )
        snapshot = MergeTrainDryRunSnapshot(
            repository=TARGET.repository,
            base_branch=TARGET.base_branch,
            base_sha=request.base_sha,
            pull_requests=tuple(
                MergeTrainPullRequestSnapshot(number=number, head_sha=head, created_at=now)
                for number, head in ((12, "b" * 40), (13, "c" * 40), (99, "d" * 40))
            ),
        )
        evidence = OrdinaryAgentMergeTrainSnapshotResult(
            snapshot=snapshot,
            base_identity=OrdinaryAgentCommitIdentity(sha=request.base_sha, tree_sha="base-tree"),
            head_identities=tuple(
                OrdinaryAgentPullRequestHeadIdentity(
                    pull_request_number=item.number,
                    identity=OrdinaryAgentCommitIdentity(
                        sha=item.head_sha, tree_sha=f"tree-{item.number}"
                    ),
                )
                for item in snapshot.pull_requests
            ),
            protection=OrdinaryAgentProtectionEvidence(
                source="evaluated_rules", evaluated_rules_sha256="a" * 64, required_checks=()
            ),
            counts=OrdinaryAgentProviderRequestCounts(
                rest_core_requests=1, graphql_requests=1, graphql_points=1
            ),
            snapshot_sha256="b" * 64,
        )
        executor = Mock(spec=MergeTrainSemanticEffectExecutor)
        executor.merge_candidate_head.side_effect = (
            CandidateHeadMergeOutcome("e" * 40, "first-tree", (request.base_sha, "b" * 40)),
            CandidateHeadMergeOutcome(None, "first-tree", ()),
        )

        def client() -> OrdinaryAgentMergeTrainClient:
            return OrdinaryAgentMergeTrainClient(
                request=request,
                effect_executor=executor,
                snapshot=lambda: evidence,
                candidate_check=Mock(),
            )

        first = client()
        candidate = MergeTrainBatchCandidate(
            batch_id="batch",
            repository=TARGET.repository,
            base_branch=TARGET.base_branch,
            base_sha=request.base_sha,
            policy_key="test-policy",
            policy_sha256="a" * 64,
            candidate_ref=build_ordinary_merge_train_candidate_ref(
                binding=first.binding, batch_id="batch"
            ),
            entries=tuple(
                MergeTrainBatchEntry(
                    pull_request_number=item.number, position=index, head_sha=item.head_sha
                )
                for index, item in enumerate(request.pull_requests, 1)
            ),
            created_at=now,
            updated_at=now,
        )
        self.assertEqual(
            tuple(
                item.number
                for item in first.read_merge_train_snapshot(
                    repository=TARGET.repository, base_branch=TARGET.base_branch
                ).pull_requests
            ),
            (12, 13),
        )
        planned_ref = candidate.candidate_ref
        for expected_merges in (0, 1, 2):
            candidate = client().build_batch_candidate(candidate=candidate)
            candidate = MergeTrainBatchCandidate.model_validate_json(candidate.model_dump_json())
            self.assertEqual(executor.merge_candidate_head.call_count, expected_merges)
            self.assertEqual(candidate.candidate_ref, planned_ref)
        executor.prepare_candidate_ref.assert_called_once()
        self.assertEqual(candidate.status, "ready_for_checks")
        assert candidate.structural_provenance is not None
        self.assertTrue(candidate.structural_provenance.complete)
        self.assertEqual(
            tuple(step.kind for step in candidate.structural_provenance.steps),
            ("merge_commit", "no_op_already_contained"),
        )
        client().build_batch_candidate(candidate=candidate)
        self.assertEqual(executor.merge_candidate_head.call_count, 2)
        other = first.binding.model_copy(update={"request_id": "job-two"})
        with self.assertRaises(MergeTrainGitHubStaleHeadError):
            client().build_batch_candidate(
                candidate=candidate.model_copy(
                    update={
                        "candidate_ref": build_ordinary_merge_train_candidate_ref(
                            binding=other, batch_id="batch"
                        )
                    }
                )
            )
        self.assertEqual(executor.merge_candidate_head.call_count, 2)
        with self.assertRaises(PermissionError):
            first.transport.request(method="GET", path="/unscoped")
