from __future__ import annotations

import unittest
from datetime import datetime, timezone
from control_plane.contracts.ordinary_agent_effect import OrdinaryAgentProviderWaitRecord

from control_plane.merge_train_github import RecordingMergeTrainGitHubTransport
from control_plane.ordinary_agent_github_transport import (
    DeadlineMergeTrainGitHubTransport,
    OrdinaryAgentProviderDeferred,
    OrdinaryAgentProviderEvidenceError,
    require_installation_provider_ready,
    require_complete_connection,
    require_complete_graphql_data,
)


class OrdinaryAgentGitHubTransportTests(unittest.TestCase):
    def test_deferral_reports_latest_deadline_across_app_and_installation(self) -> None:
        from control_plane.contracts.ordinary_agent_effect import OrdinaryAgentProviderQuotaKey

        keys = []

        def read(*, quota_key: OrdinaryAgentProviderQuotaKey) -> OrdinaryAgentProviderWaitRecord:
            keys.append(quota_key)
            return OrdinaryAgentProviderWaitRecord(
                quota_key=quota_key,
                observed_at=100,
                retry_not_before=900 if quota_key.authority_kind == "installation" else 200,
                classification="primary_rate_limit",
            )

        with self.assertRaises(OrdinaryAgentProviderDeferred) as raised:
            require_installation_provider_ready(
                app_id=1,
                installation_id=2,
                resource_classes=("core", "secondary"),
                read_provider_wait=read,
                utc_now=lambda: datetime.fromtimestamp(100, timezone.utc),
            )
        self.assertEqual(raised.exception.retry_not_before, 900)
        self.assertEqual(len(keys), 4)

    def test_slow_entry_read_preserves_final_confirmation_and_dispatch_time(self) -> None:
        now = [0.0]
        inner = RecordingMergeTrainGitHubTransport(responses=({"files": []},))
        transport = DeadlineMergeTrainGitHubTransport(
            transport=inner,
            work_deadline=75,
            token_deadline=100,
            monotonic=lambda: now[0],
        )
        transport.request(method="GET", path="/files", minimum_remaining_seconds=61)
        now[0] = 15
        with self.assertRaises(OrdinaryAgentProviderDeferred):
            transport.request(method="GET", path="/commits", minimum_remaining_seconds=61)
        self.assertEqual(len(inner.requests), 1)
        self.assertEqual(transport.rest_core_requests, 1)
        transport.require_remaining(46)
        now[0] = 30
        with self.assertRaises(OrdinaryAgentProviderDeferred):
            transport.request(method="POST", path="/graphql", minimum_remaining_seconds=46)
        self.assertEqual(transport.graphql_requests, 0)

    def test_phase_reserve_uses_earlier_token_expiry_and_cannot_weaken_default(self) -> None:
        inner = RecordingMergeTrainGitHubTransport()
        transport = DeadlineMergeTrainGitHubTransport(
            transport=inner,
            work_deadline=75,
            token_deadline=60,
            monotonic=lambda: 0,
        )
        with self.assertRaises(OrdinaryAgentProviderDeferred):
            transport.request(method="GET", path="/files", minimum_remaining_seconds=61)
        for invalid in (0, 14, float("nan"), float("inf")):
            with self.subTest(minimum=invalid), self.assertRaises(ValueError):
                transport.request(method="GET", path="/files", minimum_remaining_seconds=invalid)
        self.assertEqual(inner.requests, [])

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
        self.assertEqual(transport.graphql_requests + transport.rest_core_requests, 0)

    def test_failed_provider_calls_remain_in_request_accounting(self) -> None:
        inner = RecordingMergeTrainGitHubTransport(
            responses=(TimeoutError("response lost"), TimeoutError("response lost"))
        )
        transport = DeadlineMergeTrainGitHubTransport(
            transport=inner,
            work_deadline=100,
            token_deadline=100,
            monotonic=lambda: 0,
        )
        for method, path in (("POST", "/graphql"), ("GET", "/repos/example/project")):
            with self.assertRaises(TimeoutError):
                transport.request(method=method, path=path)
        self.assertEqual(transport.graphql_requests, 1)
        self.assertEqual(transport.rest_core_requests, 1)
        self.assertEqual(len(inner.requests), 2)

    def test_partial_graphql_response_never_becomes_completed_evidence(self) -> None:
        transport = DeadlineMergeTrainGitHubTransport(
            transport=RecordingMergeTrainGitHubTransport(),
            work_deadline=100,
            token_deadline=100,
            monotonic=lambda: 0,
        )
        for cost in (1, 11, -1):
            with (
                self.subTest(cost=cost),
                self.assertRaisesRegex(OrdinaryAgentProviderEvidenceError, "graphql_field_error"),
            ):
                require_complete_graphql_data(
                    {
                        "data": {"repository": {}, "rateLimit": {"cost": cost}},
                        "errors": [{"type": "FORBIDDEN"}],
                    },
                    transport=transport,
                )
        self.assertEqual(transport.graphql_points, 12)

    def test_graphql_quota_response_reports_wait_without_becoming_valid_evidence(self) -> None:
        transport = DeadlineMergeTrainGitHubTransport(
            transport=RecordingMergeTrainGitHubTransport(),
            work_deadline=100,
            token_deadline=100,
            monotonic=lambda: 0,
        )
        with self.assertRaisesRegex(OrdinaryAgentProviderEvidenceError, "provider_wait"):
            require_complete_graphql_data(
                {"data": None, "errors": [{"type": "RATE_LIMITED"}]}, transport=transport
            )
        self.assertEqual(transport.graphql_points, 0)

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
        self.assertEqual(transport.graphql_points, 11)


if __name__ == "__main__":
    unittest.main()
