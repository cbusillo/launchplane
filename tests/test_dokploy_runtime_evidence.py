from __future__ import annotations

import unittest
from unittest.mock import patch

import click

from control_plane.contracts.privileged_operation_worker_heartbeat import (
    privileged_operation_worker_identity_sha256,
)
from control_plane.dokploy import runtime_evidence


class DokployRuntimeEvidenceTests(unittest.TestCase):
    def test_expected_image_requires_immutable_digest_reference(self) -> None:
        with self.assertRaisesRegex(click.ClickException, "immutable repository@sha256"):
            runtime_evidence.normalize_expected_image_reference("example:latest")

    def test_structured_event_requires_allow_list_membership(self) -> None:
        with self.assertRaisesRegex(click.ClickException, "allow-listed"):
            runtime_evidence.normalize_structured_event_name("arbitrary_event")

    def test_structured_event_accepts_bounded_worker_lifecycle_markers(self) -> None:
        for event_name in (
            "privileged_operation_worker_entrypoint_started",
            "privileged_operation_worker_entrypoint_probe_succeeded",
            "privileged_operation_worker_started",
            "privileged_operation_worker_store_build_started",
            "privileged_operation_worker_schema_probe_succeeded",
            "privileged_operation_worker_store_initialized",
            "privileged_operation_worker_first_poll_attempted",
            "privileged_operation_worker_poll_succeeded",
        ):
            with self.subTest(event_name=event_name):
                self.assertEqual(
                    runtime_evidence.normalize_structured_event_name(event_name),
                    event_name,
                )

    def test_only_successful_poll_is_activation_proof(self) -> None:
        for event_name in (
            "privileged_operation_worker_entrypoint_started",
            "privileged_operation_worker_entrypoint_probe_succeeded",
            "privileged_operation_worker_started",
            "privileged_operation_worker_store_build_started",
            "privileged_operation_worker_schema_probe_succeeded",
            "privileged_operation_worker_store_initialized",
            "privileged_operation_worker_first_poll_attempted",
        ):
            with self.subTest(event_name=event_name):
                self.assertFalse(runtime_evidence.structured_event_is_activation_proof(event_name))
        self.assertTrue(
            runtime_evidence.structured_event_is_activation_proof(
                "privileged_operation_worker_poll_succeeded"
            )
        )

    def test_fetch_compose_service_runtime_returns_bounded_image_and_state(self) -> None:
        requests: list[dict[str, object]] = []
        image_digest = "a" * 64
        image_id = "b" * 64

        def capture_request(**kwargs: object) -> object:
            requests.append(kwargs)
            if kwargs["path"] == "/api/docker.getContainersByAppNameMatch":
                return [
                    {
                        "containerId": "a" * 64,
                        "serviceName": "launchplane-privileged-operation-workers",
                        "state": "running",
                        "status": "Up 5 minutes",
                    }
                ]
            return {
                "Image": f"sha256:{image_id}",
                "Config": {
                    "Image": f"ghcr.io/example/launchplane@sha256:{image_digest}",
                    "Hostname": "a" * 12,
                    "Env": ["LAUNCHPLANE_DATABASE_URL=secret"],
                },
            }

        with patch(
            "control_plane.dokploy.api.dokploy_request",
            side_effect=capture_request,
        ):
            runtime = runtime_evidence.fetch_compose_service_runtime(
                host="https://dokploy.example.com",
                token="secret-token",
                compose_id="compose-123",
                app_name="launchplane",
                server_id="server-1",
                service_name="launchplane-privileged-operation-workers",
            )

        self.assertEqual(
            [request["path"] for request in requests],
            [
                "/api/docker.getContainersByAppNameMatch",
                "/api/docker.getConfig",
            ],
        )
        self.assertEqual(
            requests[1]["query"],
            {"containerId": "a" * 64, "serverId": "server-1"},
        )
        self.assertTrue(runtime["running"])
        self.assertTrue(runtime["image_reference_immutable"])
        self.assertEqual(
            runtime["immutable_image_reference"],
            f"ghcr.io/example/launchplane@sha256:{image_digest}",
        )
        self.assertEqual(runtime["image_id"], f"sha256:{image_id}")
        self.assertEqual(
            runtime["container_identity_sha256"],
            privileged_operation_worker_identity_sha256("a" * 12),
        )
        self.assertNotIn("Env", runtime)
        self.assertNotIn("secret", str(runtime))

    def test_fetch_compose_service_runtime_marks_mutable_reference_unverified(self) -> None:
        with patch(
            "control_plane.dokploy.api.dokploy_request",
            side_effect=[
                [
                    {
                        "containerId": "a" * 64,
                        "serviceName": "worker",
                        "state": "running",
                    }
                ],
                {
                    "Image": f"sha256:{'b' * 64}",
                    "Config": {"Image": "example:latest", "Hostname": "a" * 12},
                },
            ],
        ):
            runtime = runtime_evidence.fetch_compose_service_runtime(
                host="https://dokploy.example.com",
                token="secret-token",
                compose_id="compose-123",
                app_name="launchplane",
                service_name="worker",
            )

        self.assertFalse(runtime["image_reference_immutable"])
        self.assertEqual(runtime["immutable_image_reference"], "")

    def test_fetch_compose_service_runtime_selects_exact_container_name(self) -> None:
        with patch(
            "control_plane.dokploy.runtime_evidence.dokploy_api.dokploy_request",
            side_effect=[
                [
                    {"containerId": "c" * 64, "name": "launchplane_database_1"},
                    {"containerId": "b" * 64, "name": "launchplane_worker_1"},
                ],
                [
                    {
                        "Image": f"sha256:{'b' * 64}",
                        "Config": {
                            "Image": f"example@sha256:{'a' * 64}",
                            "Hostname": "b" * 12,
                        },
                    }
                ],
            ],
        ):
            runtime = runtime_evidence.fetch_compose_service_runtime(
                host="https://dokploy.example.com",
                token="secret-token",
                compose_id="compose-123",
                app_name="launchplane",
                service_name="worker",
            )

        self.assertEqual(runtime["service"], "worker")

    def test_fetch_compose_service_runtime_rejects_ambiguous_service(self) -> None:
        with (
            patch(
                "control_plane.dokploy.runtime_evidence.dokploy_api.dokploy_request",
                return_value=[
                    {"containerId": "worker-1", "name": "launchplane_worker_1"},
                    {"containerId": "worker-2", "name": "launchplane_worker_2"},
                ],
            ),
            self.assertRaisesRegex(
                runtime_evidence.DokployEvidenceProviderError,
                "service-select",
            ),
        ):
            runtime_evidence.fetch_compose_service_runtime(
                host="https://dokploy.example.com",
                token="secret-token",
                compose_id="compose-123",
                app_name="launchplane",
                service_name="worker",
            )

    def test_fetch_compose_service_runtime_identifies_config_stage(self) -> None:
        with (
            patch(
                "control_plane.dokploy.runtime_evidence.dokploy_api.dokploy_request",
                side_effect=[
                    [{"containerId": "d" * 64, "serviceName": "worker"}],
                    click.ClickException("provider secret detail"),
                ],
            ),
            self.assertRaisesRegex(
                runtime_evidence.DokployEvidenceProviderError,
                "container-config",
            ),
        ):
            runtime_evidence.fetch_compose_service_runtime(
                host="https://dokploy.example.com",
                token="secret-token",
                compose_id="compose-123",
                app_name="launchplane",
                service_name="worker",
            )

    def test_fetch_compose_service_runtime_identifies_container_list_stage(self) -> None:
        with (
            patch(
                "control_plane.dokploy.runtime_evidence.dokploy_api.dokploy_request",
                return_value={"unexpected": "shape"},
            ),
            self.assertRaisesRegex(
                runtime_evidence.DokployEvidenceProviderError,
                "container-list",
            ),
        ):
            runtime_evidence.fetch_compose_service_runtime(
                host="https://dokploy.example.com",
                token="secret-token",
                compose_id="compose-123",
                app_name="launchplane",
                service_name="worker",
            )

    def test_fetch_compose_service_runtime_identifies_image_stage(self) -> None:
        with (
            patch(
                "control_plane.dokploy.runtime_evidence.dokploy_api.dokploy_request",
                side_effect=[
                    [{"containerId": "d" * 64, "serviceName": "worker"}],
                    {
                        "Image": "",
                        "Config": {
                            "Image": "example:latest",
                            "Hostname": "d" * 12,
                        },
                    },
                ],
            ),
            self.assertRaisesRegex(
                runtime_evidence.DokployEvidenceProviderError,
                "image-identity",
            ),
        ):
            runtime_evidence.fetch_compose_service_runtime(
                host="https://dokploy.example.com",
                token="secret-token",
                compose_id="compose-123",
                app_name="launchplane",
                service_name="worker",
            )

    def test_fetch_compose_service_runtime_rejects_custom_hostname_identity(self) -> None:
        with (
            patch(
                "control_plane.dokploy.runtime_evidence.dokploy_api.dokploy_request",
                side_effect=[
                    [{"containerId": "e" * 64, "serviceName": "worker"}],
                    {
                        "Image": f"sha256:{'b' * 64}",
                        "Config": {
                            "Image": f"example@sha256:{'a' * 64}",
                            "Hostname": "custom-worker",
                        },
                    },
                ],
            ),
            self.assertRaisesRegex(
                runtime_evidence.DokployEvidenceProviderError,
                "container-identity",
            ),
        ):
            runtime_evidence.fetch_compose_service_runtime(
                host="https://dokploy.example.com",
                token="secret-token",
                compose_id="compose-123",
                app_name="launchplane",
                service_name="worker",
            )

    def test_fetch_compose_service_runtime_identifies_target_stage(self) -> None:
        with self.assertRaisesRegex(
            runtime_evidence.DokployEvidenceProviderError,
            "target-inspect",
        ):
            runtime_evidence.fetch_compose_service_runtime(
                host="https://dokploy.example.com",
                token="secret-token",
                compose_id="compose-123",
                app_name="",
                service_name="worker",
            )

    def test_count_structured_log_events_requires_exact_json_event(self) -> None:
        event_name = "privileged_operation_worker_poll_succeeded"

        matching_event_count = runtime_evidence.count_structured_log_events(
            (
                f'2026-08-24T00:00:00Z {{"event":"{event_name}","processed":0}} trailing',
                '{"event":"privileged_operation_worker_retry"}',
                f"plain text mentioning {event_name}",
                "not-json {broken",
            ),
            event_name=event_name,
        )

        self.assertEqual(matching_event_count, 1)

    def test_runtime_log_summary_returns_only_structural_counts_and_fixed_error_kind(self) -> None:
        summary = runtime_evidence.summarize_runtime_log_lines(
            (
                '2026-08-24T00:00:00Z {"event":"privileged_operation_worker_started"}',
                '2026-08-24T00:00:01Z {"status":"remote command failed"}',
                "Error response from daemon: configured logging driver does not support reading",
                "not-json {broken secret-value",
            )
        )

        self.assertEqual(
            summary,
            {
                "structured_event_line_count": 1,
                "json_line_count": 2,
                "non_json_line_count": 2,
                "provider_error_line_count": 1,
                "provider_error_kind": "unsupported_logging_driver",
            },
        )
        self.assertNotIn("secret-value", str(summary))

    def test_fetch_compose_container_logs_uses_selected_container_without_search(self) -> None:
        requests: list[dict[str, object]] = []

        def capture_request(**kwargs: object) -> object:
            requests.append(kwargs)
            return {
                "logs": (
                    '{"event":"privileged_operation_worker_poll_succeeded"}\nSERVICE_TOKEN=secret'
                )
            }

        with patch(
            "control_plane.dokploy.runtime_evidence.dokploy_api.dokploy_request",
            side_effect=capture_request,
        ):
            lines = runtime_evidence.fetch_compose_container_logs(
                host="https://dokploy.example.com",
                token="secret-token",
                compose_id="compose-123",
                container_id="worker-container",
                line_count=200,
            )

        self.assertEqual(requests[0]["path"], "/api/compose.readLogs")
        self.assertEqual(
            requests[0]["query"],
            {
                "composeId": "compose-123",
                "containerId": "worker-container",
                "tail": 200,
                "since": "1d",
            },
        )
        query = requests[0]["query"]
        assert isinstance(query, dict)
        self.assertNotIn("search", query)
        self.assertEqual(
            lines,
            (
                '{"event":"privileged_operation_worker_poll_succeeded"}',
                "SERVICE_TOKEN=[redacted]",
            ),
        )

    def test_fetch_compose_container_logs_uses_allowlisted_event_search(self) -> None:
        requests: list[dict[str, object]] = []

        def capture_request(**kwargs: object) -> object:
            requests.append(kwargs)
            return '{"event":"privileged_operation_worker_poll_succeeded"}'

        with patch(
            "control_plane.dokploy.runtime_evidence.dokploy_api.dokploy_request",
            side_effect=capture_request,
        ):
            lines = runtime_evidence.fetch_compose_container_logs(
                host="https://dokploy.example.com",
                token="secret-token",
                compose_id="compose-123",
                container_id="worker-container",
                line_count=200,
                search_text="privileged_operation_worker_poll_succeeded",
            )

        self.assertEqual(
            requests[0]["query"],
            {
                "composeId": "compose-123",
                "containerId": "worker-container",
                "tail": 200,
                "since": "1d",
                "search": "privileged_operation_worker_poll_succeeded",
            },
        )
        self.assertEqual(lines, ('{"event":"privileged_operation_worker_poll_succeeded"}',))


if __name__ == "__main__":
    unittest.main()
