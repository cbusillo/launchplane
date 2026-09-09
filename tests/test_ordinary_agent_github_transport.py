from __future__ import annotations

import unittest

from control_plane.merge_train_github import RecordingMergeTrainGitHubTransport
from control_plane.ordinary_agent_github_transport import (
    DeadlineMergeTrainGitHubTransport,
    OrdinaryAgentProviderDeferred,
    OrdinaryAgentProviderEvidenceError,
    require_complete_connection,
    require_complete_graphql_data,
)


class OrdinaryAgentGitHubTransportTests(unittest.TestCase):
    def test_deadline_denies_before_provider_request(self) -> None:
        inner = RecordingMergeTrainGitHubTransport(responses=({"data": {}},))
        transport = DeadlineMergeTrainGitHubTransport(
            transport=inner,
            work_deadline=20,
            token_deadline=100,
            monotonic=lambda: 6,
        )
        with self.assertRaisesRegex(OrdinaryAgentProviderDeferred, "provider_attempt_deadline"):
            transport.request(method="POST", path="/graphql", body={"query": "query {}"})
        self.assertEqual(inner.requests, [])

    def test_partial_graphql_response_never_becomes_completed_evidence(self) -> None:
        transport = DeadlineMergeTrainGitHubTransport(
            transport=RecordingMergeTrainGitHubTransport(),
            work_deadline=100,
            token_deadline=100,
            monotonic=lambda: 0,
        )
        with self.assertRaisesRegex(OrdinaryAgentProviderEvidenceError, "graphql_field_error"):
            require_complete_graphql_data(
                {
                    "data": {"repository": {}, "rateLimit": {"cost": 1}},
                    "errors": [{"type": "FORBIDDEN"}],
                },
                transport=transport,
            )

    def test_connection_rejects_silent_truncation(self) -> None:
        with self.assertRaisesRegex(OrdinaryAgentProviderEvidenceError, "checks_truncated"):
            require_complete_connection(
                {
                    "nodes": [{"name": "first"}],
                    "totalCount": 2,
                    "pageInfo": {"hasNextPage": True},
                },
                label="checks",
            )

    def test_query_cost_breach_is_a_closed_terminal_error(self) -> None:
        transport = DeadlineMergeTrainGitHubTransport(
            transport=RecordingMergeTrainGitHubTransport(),
            work_deadline=100,
            token_deadline=100,
            monotonic=lambda: 0,
        )
        with self.assertRaisesRegex(
            OrdinaryAgentProviderEvidenceError, "snapshot_query_cost_exceeded"
        ):
            require_complete_graphql_data(
                {"data": {"repository": {}, "rateLimit": {"cost": 11}}},
                transport=transport,
            )


if __name__ == "__main__":
    unittest.main()
