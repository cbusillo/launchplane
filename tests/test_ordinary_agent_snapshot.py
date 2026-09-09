from __future__ import annotations

import unittest

from pydantic import ValidationError

from control_plane.contracts.ordinary_agent_snapshot import (
    OrdinaryAgentCommitIdentity,
    OrdinaryAgentMergeTrainSnapshotResult,
    OrdinaryAgentProtectionEvidence,
    OrdinaryAgentProviderRequestCounts,
    OrdinaryAgentPullRequestHeadIdentity,
)
from control_plane.merge_train import MergeTrainDryRunSnapshot, MergeTrainPullRequestSnapshot


class OrdinaryAgentSnapshotContractTests(unittest.TestCase):
    def test_snapshot_evidence_rejects_a_head_identity_from_another_snapshot(self) -> None:
        snapshot = MergeTrainDryRunSnapshot(
            repository="example/repo",
            base_branch="main",
            base_sha="a" * 40,
            pull_requests=(
                MergeTrainPullRequestSnapshot(
                    number=7,
                    created_at="2026-09-09T10:00:00Z",
                    head_sha="b" * 40,
                ),
            ),
        )
        with self.assertRaisesRegex(ValidationError, "head identities"):
            OrdinaryAgentMergeTrainSnapshotResult(
                snapshot=snapshot,
                base_identity=OrdinaryAgentCommitIdentity(
                    sha="a" * 40, tree_sha="c" * 40
                ),
                head_identities=(
                    OrdinaryAgentPullRequestHeadIdentity(
                        pull_request_number=7,
                        identity=OrdinaryAgentCommitIdentity(
                            sha="d" * 40, tree_sha="e" * 40
                        ),
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
