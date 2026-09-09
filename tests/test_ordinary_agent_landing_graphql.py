"""Bounded identity acquisition rejects changes before a landing can finalize."""

import json
import unittest
from collections.abc import Sequence
from copy import deepcopy
from typing import Any

from control_plane.contracts.merge_train_batch import MergeTrainBatchCandidate, MergeTrainBatchEntry
from control_plane.merge_train_github import RecordingMergeTrainGitHubTransport
from control_plane.ordinary_agent_github_transport import (
    DeadlineMergeTrainGitHubTransport,
    OrdinaryAgentProviderDeferred,
    OrdinaryAgentProviderEvidenceError,
)
from control_plane.ordinary_agent_landing_graphql import (
    OrdinaryLandingGraphQLObservation,
    confirm_landing_graphql,
    read_landing_graphql,
)


class LandingGraphQLTests(unittest.TestCase):
    def setUp(self) -> None:
        self.candidate = MergeTrainBatchCandidate(
            batch_id="batch",
            repository="example/project",
            base_branch="main",
            base_sha="base",
            policy_key="policy",
            policy_sha256="a" * 64,
            candidate_ref="refs/heads/train/batch",
            candidate_sha="candidate",
            candidate_tree_sha="combined-tree",
            created_at="2026-09-09T00:00:00Z",
            updated_at="2026-09-09T00:00:00Z",
            entries=tuple(
                MergeTrainBatchEntry(
                    pull_request_number=i + 1,
                    position=i + 1,
                    head_sha=f"head-{i}",
                    head_tree_sha=f"tree-{i}",
                )
                for i in range(2)
            ),
        )
        identity = {"databaseId": 123, "nameWithOwner": "example/project"}
        repository: dict[str, Any] = {
            **identity,
            "owner": {"databaseId": 456},
            "ref": {"name": "main", "target": {"oid": "base", "tree": {"oid": "base-tree"}}},
            "candidate": {"oid": "candidate", "tree": {"oid": "combined-tree"}},
        }
        for i in range(2):
            repository[f"head{i}"] = {"oid": f"head-{i}", "tree": {"oid": f"tree-{i}"}}
            repository[f"pr{i}"] = {
                "number": i + 1,
                "headRefOid": f"head-{i}",
                "baseRefOid": "base",
                "baseRefName": "main",
                "updatedAt": "2026-09-09T00:00:00Z",
                "state": "MERGED" if i == 0 else "OPEN",
                "mergeCommit": {"oid": "previous-merge"} if i == 0 else None,
                "headRef": None if i == 0 else {"name": "topic"},
                "headRepository": identity,
                "baseRepository": identity,
            }
        self.response: dict[str, Any] = {
            "data": {"rateLimit": {"cost": 1}, "repository": repository}
        }
        self.now = 0.0

    def transport(
        self, responses: Sequence[object]
    ) -> tuple[RecordingMergeTrainGitHubTransport, DeadlineMergeTrainGitHubTransport]:
        inner = RecordingMergeTrainGitHubTransport(responses=tuple(responses))
        return inner, DeadlineMergeTrainGitHubTransport(
            transport=inner,
            work_deadline=75,
            token_deadline=100,
            monotonic=lambda: self.now,
        )

    def read(
        self, transport: DeadlineMergeTrainGitHubTransport
    ) -> OrdinaryLandingGraphQLObservation:
        return read_landing_graphql(
            transport=transport,
            candidate=self.candidate,
            repository_id=123,
            repository_owner_id=456,
            base_sha="base",
            terminal_entries=frozenset({1}),
            utc_seconds=lambda: 1000,
        )

    def confirm(
        self,
        transport: DeadlineMergeTrainGitHubTransport,
        observation: OrdinaryLandingGraphQLObservation,
    ) -> None:
        confirm_landing_graphql(
            transport=transport,
            candidate=self.candidate,
            repository_id=123,
            repository_owner_id=456,
            base_sha="base",
            terminal_entries=frozenset({1}),
            observation=observation,
        )

    def test_deleted_terminal_branch_still_requires_immutable_head_and_preserves_age(self) -> None:
        inner, transport = self.transport([self.response, deepcopy(self.response)])
        observation = self.read(transport)
        self.now = 20
        self.confirm(transport, observation)
        self.assertEqual(observation.observed_at, 1000)
        self.assertEqual(transport.graphql_requests, 2)
        self.assertEqual(transport.graphql_points, 2)
        self.assertEqual(len(inner.requests), 2)
        self.assertIsNone(json.loads(observation.repository_json)["pr0"]["headRef"])
        broken = deepcopy(self.response)
        broken["data"]["repository"]["head0"] = None
        self.now = 0
        _, missing = self.transport([broken])
        with self.assertRaises(OrdinaryAgentProviderEvidenceError):
            self.read(missing)

    def test_final_confirmation_rejects_pr_or_base_drift(self) -> None:
        for field, value in (
            ("headRefOid", "different-head"),
            ("baseRefOid", "different-base"),
            ("updatedAt", "2026-09-09T00:00:01Z"),
            ("state", "CLOSED"),
            ("mergeCommit", {"oid": "new-merge"}),
            ("baseRefName", "release"),
        ):
            with self.subTest(field=field):
                changed = deepcopy(self.response)
                changed["data"]["repository"]["pr1"][field] = value
                _, transport = self.transport([self.response, changed])
                observation = self.read(transport)
                with self.assertRaises(OrdinaryAgentProviderEvidenceError):
                    self.confirm(transport, observation)
        changed = deepcopy(self.response)
        changed["data"]["repository"]["ref"]["target"]["oid"] = "new-main"
        _, transport = self.transport([self.response, changed])
        observation = self.read(transport)
        with self.assertRaises(OrdinaryAgentProviderEvidenceError):
            self.confirm(transport, observation)

    def test_confirmation_stops_before_spending_dispatch_time(self) -> None:
        inner, transport = self.transport([self.response])
        observation = self.read(transport)
        self.now = 30
        with self.assertRaises(OrdinaryAgentProviderDeferred):
            self.confirm(transport, observation)
        self.assertEqual(len(inner.requests), 1)

    def test_late_initial_read_does_not_spend_quota_on_an_unconfirmable_observation(self) -> None:
        inner, transport = self.transport([self.response])
        self.now = 20
        with self.assertRaises(OrdinaryAgentProviderDeferred):
            self.read(transport)
        self.assertEqual(inner.requests, [])

    def test_confirmation_does_not_require_detailed_only_fields(self) -> None:
        detailed = deepcopy(self.response)
        repository = detailed["data"]["repository"]
        repository["ref"]["branchProtectionRule"] = {"requiresStatusChecks": True}
        repository["candidate"]["statusCheckRollup"] = {"state": "SUCCESS"}
        repository["pr1"]["labels"] = {"nodes": [{"name": "queued"}]}
        repository["pr1"]["mergeable"] = "MERGEABLE"
        _, transport = self.transport([detailed, self.response])
        self.confirm(transport, self.read(transport))

    def test_active_deleted_branch_and_incomplete_final_response_fail_closed(self) -> None:
        changed = deepcopy(self.response)
        changed["data"]["repository"]["pr1"]["headRef"] = None
        _, transport = self.transport([changed])
        with self.assertRaises(OrdinaryAgentProviderEvidenceError):
            self.read(transport)
        changed = deepcopy(self.response)
        del changed["data"]["repository"]["pr1"]["mergeCommit"]
        _, transport = self.transport([self.response, changed])
        observation = self.read(transport)
        with self.assertRaises(OrdinaryAgentProviderEvidenceError):
            self.confirm(transport, observation)
