from __future__ import annotations

import unittest
from unittest.mock import Mock

from control_plane.contracts.ordinary_agent_effect import (
    OrdinaryAgentControllerFence,
    OrdinaryAgentSnapshotAttemptRecord,
)
from control_plane.contracts.ordinary_agent_snapshot import (
    OrdinaryAgentCommitIdentity,
    OrdinaryAgentMergeTrainSnapshotResult,
    OrdinaryAgentProtectionEvidence,
    OrdinaryAgentProviderRequestCounts,
    OrdinaryAgentPullRequestHeadIdentity,
)
from control_plane.merge_train import MergeTrainDryRunSnapshot, MergeTrainPullRequestSnapshot
from control_plane.ordinary_agent_merge_train_snapshot import (
    acquire_ordinary_agent_merge_train_snapshot,
)


class OrdinaryAgentMergeTrainSnapshotTests(unittest.TestCase):
    def test_completed_snapshot_replay_makes_no_custody_or_provider_call(self) -> None:
        result = _snapshot_result()
        store = Mock()
        store.reserve_ordinary_agent_snapshot_attempt.return_value = (
            OrdinaryAgentSnapshotAttemptRecord(
                attempt_id="snapshot_one",
                request_id="request_one",
                binding_revision=1,
                scope_sha256="a" * 64,
                principal_id="principal_one",
                credential_id="credential_one",
                credential_version=1,
                purpose="snapshot",
                attempt_ordinal=1,
                controller_fence=_fence(),
                state="completed",
                created_at=1,
                updated_at=2,
                result=result,
            )
        )
        reader = Mock(side_effect=AssertionError("provider reader must not run on replay"))

        observed = acquire_ordinary_agent_merge_train_snapshot(
            store=store,
            custody_store=Mock(),
            secret_store=Mock(),
            request_id="request_one",
            expected_binding_revision=1,
            controller_fence=_fence(),
            reader=reader,
        )

        self.assertIs(observed, result)
        store.reserve_ordinary_agent_read_custody_attempt.assert_not_called()
        reader.assert_not_called()


def _fence() -> OrdinaryAgentControllerFence:
    return OrdinaryAgentControllerFence(
        controller_key="example/repo:main",
        lease_owner="worker_one",
        lease_acquired_at="2026-09-09T10:00:00Z",
    )


def _snapshot_result() -> OrdinaryAgentMergeTrainSnapshotResult:
    snapshot = MergeTrainDryRunSnapshot(
        repository="example/repo",
        base_branch="main",
        base_sha="b" * 40,
        pull_requests=(
            MergeTrainPullRequestSnapshot(
                number=7,
                created_at="2026-09-09T10:00:00Z",
                head_sha="c" * 40,
            ),
        ),
    )
    return OrdinaryAgentMergeTrainSnapshotResult(
        snapshot=snapshot,
        base_identity=OrdinaryAgentCommitIdentity(sha="b" * 40, tree_sha="d" * 40),
        head_identities=(
            OrdinaryAgentPullRequestHeadIdentity(
                pull_request_number=7,
                identity=OrdinaryAgentCommitIdentity(sha="c" * 40, tree_sha="e" * 40),
            ),
        ),
        protection=OrdinaryAgentProtectionEvidence(
            source="evaluated_rules",
            evaluated_rules_sha256="f" * 64,
            required_checks=(),
        ),
        counts=OrdinaryAgentProviderRequestCounts(
            rest_core_requests=1,
            graphql_requests=1,
            graphql_points=1,
        ),
        snapshot_sha256="0" * 64,
    )


if __name__ == "__main__":
    unittest.main()
