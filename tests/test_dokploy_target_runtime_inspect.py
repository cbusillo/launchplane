from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from typing import cast

import click

from control_plane.dokploy import api as dokploy_api
from control_plane.dokploy import runtime_evidence as dokploy_runtime_evidence
from control_plane.contracts.privileged_operation_worker_heartbeat import (
    PrivilegedOperationWorkerHeartbeatRecord,
    privileged_operation_worker_identity_sha256,
)
from control_plane.dokploy_target_inspect import (
    DokployTargetInspectRequest,
    DokployTargetInspectStore,
    inspect_dokploy_target,
)

_UNUSED_STORE = cast(DokployTargetInspectStore, object())
_NOW = datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc)
_IMAGE_REFERENCE = f"ghcr.io/example/launchplane@sha256:{'a' * 64}"
_CONTAINER_HOSTNAME = "a" * 12
_CONTAINER_IDENTITY_SHA256 = privileged_operation_worker_identity_sha256(_CONTAINER_HOSTNAME)


def _heartbeat(
    *,
    runtime_identity: str = _CONTAINER_HOSTNAME,
    image_reference: str = _IMAGE_REFERENCE,
    last_poll_succeeded_at: datetime = _NOW,
) -> PrivilegedOperationWorkerHeartbeatRecord:
    return PrivilegedOperationWorkerHeartbeatRecord(
        worker_identity_sha256=privileged_operation_worker_identity_sha256(runtime_identity),
        image_reference=image_reference,
        poll_interval_seconds=15,
        last_poll_succeeded_at=last_poll_succeeded_at.isoformat(),
    )


class _HeartbeatStore:
    def __init__(self, *records: PrivilegedOperationWorkerHeartbeatRecord) -> None:
        self.records = records

    def list_privileged_operation_worker_heartbeat_records(
        self,
        *,
        worker_kind: str = "",
        limit: int | None = None,
    ) -> tuple[PrivilegedOperationWorkerHeartbeatRecord, ...]:
        del worker_kind
        if limit is None:
            return self.records
        return self.records[:limit]


_READY_STORE = cast(DokployTargetInspectStore, _HeartbeatStore(_heartbeat()))


def _fetch_target_payload(
    *, host: str, token: str, target_type: str, target_id: str
) -> dokploy_api.JsonObject:
    del host, token, target_type, target_id
    return {
        "id": "compose-123",
        "appName": "launchplane",
        "serverId": "server-123",
        "env": {"LAUNCHPLANE_DATABASE_URL": "secret"},
    }


def _fetch_service_runtime(
    *,
    host: str,
    token: str,
    compose_id: str,
    app_name: str,
    server_id: str,
    service_name: str,
) -> dokploy_api.JsonObject:
    del host, token, compose_id, app_name, server_id, service_name
    return {
        "container_id": "a" * 64,
        "container_identity_sha256": _CONTAINER_IDENTITY_SHA256,
        "state": "running",
        "status": "Up 5 minutes",
        "running": True,
        "configured_image": _IMAGE_REFERENCE,
        "image_id": f"sha256:{'b' * 64}",
        "immutable_image_reference": _IMAGE_REFERENCE,
        "image_reference_immutable": True,
    }


def _fetch_logs(
    *,
    host: str,
    token: str,
    compose_id: str,
    container_id: str,
    line_count: int,
    search_text: str,
) -> tuple[str, ...]:
    del host, token, compose_id, container_id, line_count
    if search_text:
        assert search_text == "privileged_operation_worker_poll_succeeded"
        return (
            '{"event":"privileged_operation_worker_poll_succeeded","processed":0,"statuses":[]}',
        )
    return (
        '{"event":"privileged_operation_worker_poll_succeeded","processed":0,"statuses":[]}',
        "LAUNCHPLANE_DATABASE_URL=secret",
    )


class DokployTargetRuntimeInspectTests(unittest.TestCase):
    def test_target_fetch_failure_identifies_target_stage(self) -> None:
        def fetch_target_failure(**kwargs: str) -> dokploy_api.JsonObject:
            del kwargs
            raise click.ClickException("provider TOKEN=secret")

        with self.assertRaisesRegex(
            dokploy_runtime_evidence.DokployEvidenceProviderError,
            "target-inspect",
        ):
            inspect_dokploy_target(
                record_store=_UNUSED_STORE,
                host="https://dokploy.example.invalid",
                token="token",
                request=DokployTargetInspectRequest(
                    target_type="compose",
                    target_id="compose-123",
                ),
                fetch_target_payload=fetch_target_failure,
            )

    def test_request_requires_service_for_runtime_event_evidence(self) -> None:
        with self.assertRaisesRegex(ValueError, "requires a compose service"):
            DokployTargetInspectRequest(
                target_type="compose",
                target_id="compose-123",
                event="privileged_operation_worker_poll_succeeded",
            )

    def test_request_requires_complete_runtime_proof_selectors(self) -> None:
        request = DokployTargetInspectRequest(
            target_type="compose",
            target_id="compose-123",
            service="worker",
            expected_image=f"example@sha256:{'a' * 64}",
        )
        self.assertEqual(request.event, "")
        with self.assertRaisesRegex(ValueError, "requires an expected image"):
            DokployTargetInspectRequest(
                target_type="compose",
                target_id="compose-123",
                service="worker",
                event="privileged_operation_worker_poll_succeeded",
            )

    def test_explicit_compose_target_returns_bounded_runtime_evidence(self) -> None:
        result = inspect_dokploy_target(
            record_store=_READY_STORE,
            host="https://dokploy.example.invalid",
            token="token",
            request=DokployTargetInspectRequest(
                target_type="compose",
                target_id="compose-123",
                service="launchplane-privileged-operation-workers",
                event="privileged_operation_worker_poll_succeeded",
                expected_image=f"ghcr.io/example/launchplane@sha256:{'a' * 64}",
            ),
            fetch_target_payload=_fetch_target_payload,
            fetch_compose_service_runtime=_fetch_service_runtime,
            fetch_compose_logs=_fetch_logs,
            now=lambda: _NOW,
        )

        runtime_evidence = result["runtime_evidence"]
        self.assertIsInstance(runtime_evidence, dict)
        assert isinstance(runtime_evidence, dict)
        structured_event = runtime_evidence["structured_event"]
        self.assertIsInstance(structured_event, dict)
        assert isinstance(structured_event, dict)
        self.assertTrue(runtime_evidence["running"])
        self.assertTrue(runtime_evidence["image_reference_immutable"])
        self.assertTrue(runtime_evidence["image_matches_expected"])
        self.assertTrue(runtime_evidence["proof_ready"])
        self.assertEqual(runtime_evidence["proof_source"], "worker_heartbeat_record")
        worker_heartbeat = runtime_evidence["worker_heartbeat"]
        assert isinstance(worker_heartbeat, dict)
        self.assertEqual(worker_heartbeat["status"], "ready")
        self.assertTrue(worker_heartbeat["identity_consistent"])
        self.assertTrue(worker_heartbeat["image_matches"])
        self.assertEqual(structured_event["matching_line_count"], 1)
        self.assertEqual(structured_event["candidate_line_count"], 1)
        self.assertEqual(
            structured_event["log_classification"],
            {
                "structured_event_line_count": 1,
                "json_line_count": 1,
                "non_json_line_count": 1,
                "provider_error_line_count": 0,
                "provider_error_kind": "",
            },
        )
        self.assertTrue(structured_event["observed"])
        self.assertTrue(structured_event["activation_proof_eligible"])
        self.assertNotIn(_CONTAINER_IDENTITY_SHA256, str(runtime_evidence))
        self.assertNotIn("worker_identity_sha256", str(runtime_evidence))
        self.assertNotIn("LAUNCHPLANE_DATABASE_URL", str(runtime_evidence))
        self.assertNotIn("secret", str(runtime_evidence))

    def test_diagnostic_worker_event_does_not_replace_heartbeat_proof(self) -> None:
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
                result = inspect_dokploy_target(
                    record_store=_READY_STORE,
                    host="https://dokploy.example.invalid",
                    token="token",
                    request=DokployTargetInspectRequest(
                        target_type="compose",
                        target_id="compose-123",
                        service="launchplane-privileged-operation-workers",
                        event=event_name,
                        expected_image=f"ghcr.io/example/launchplane@sha256:{'a' * 64}",
                    ),
                    fetch_target_payload=_fetch_target_payload,
                    fetch_compose_service_runtime=_fetch_service_runtime,
                    fetch_compose_logs=lambda **_kwargs: (f'{{"event":"{event_name}"}}',),
                    now=lambda: _NOW,
                )

                runtime_evidence = result["runtime_evidence"]
                assert isinstance(runtime_evidence, dict)
                structured_event = runtime_evidence["structured_event"]
                assert isinstance(structured_event, dict)
                self.assertTrue(structured_event["observed"])
                self.assertFalse(structured_event["activation_proof_eligible"])
                self.assertTrue(runtime_evidence["proof_ready"])

    def test_expected_image_mismatch_is_not_proof_ready(self) -> None:
        result = inspect_dokploy_target(
            record_store=_READY_STORE,
            host="https://dokploy.example.invalid",
            token="token",
            request=DokployTargetInspectRequest(
                target_type="compose",
                target_id="compose-123",
                service="launchplane-privileged-operation-workers",
                event="privileged_operation_worker_poll_succeeded",
                expected_image=f"ghcr.io/example/launchplane@sha256:{'c' * 64}",
            ),
            fetch_target_payload=_fetch_target_payload,
            fetch_compose_service_runtime=_fetch_service_runtime,
            fetch_compose_logs=_fetch_logs,
            now=lambda: _NOW,
        )

        runtime_evidence = result["runtime_evidence"]
        assert isinstance(runtime_evidence, dict)
        self.assertFalse(runtime_evidence["image_matches_expected"])
        self.assertFalse(runtime_evidence["proof_ready"])

    def test_provider_error_line_remains_diagnostic(self) -> None:
        def fetch_provider_error_and_event(
            *, search_text: str, **_kwargs: object
        ) -> tuple[str, ...]:
            if search_text:
                return ('{"event":"privileged_operation_worker_poll_succeeded"}',)
            return (
                "Error response from daemon: configured logging driver does not support reading",
            )

        result = inspect_dokploy_target(
            record_store=_READY_STORE,
            host="https://dokploy.example.invalid",
            token="token",
            request=DokployTargetInspectRequest(
                target_type="compose",
                target_id="compose-123",
                service="launchplane-privileged-operation-workers",
                event="privileged_operation_worker_poll_succeeded",
                expected_image=f"ghcr.io/example/launchplane@sha256:{'a' * 64}",
            ),
            fetch_target_payload=_fetch_target_payload,
            fetch_compose_service_runtime=_fetch_service_runtime,
            fetch_compose_logs=fetch_provider_error_and_event,
            now=lambda: _NOW,
        )

        runtime_evidence = result["runtime_evidence"]
        assert isinstance(runtime_evidence, dict)
        structured_event = runtime_evidence["structured_event"]
        assert isinstance(structured_event, dict)
        classification = structured_event["log_classification"]
        assert isinstance(classification, dict)
        self.assertTrue(structured_event["observed"])
        self.assertEqual(classification["provider_error_line_count"], 1)
        self.assertEqual(classification["provider_error_kind"], "unsupported_logging_driver")
        self.assertTrue(runtime_evidence["proof_ready"])

    def test_non_running_service_is_not_proof_ready(self) -> None:
        def fetch_stopped_runtime(**kwargs: str) -> dokploy_api.JsonObject:
            del kwargs
            runtime = _fetch_service_runtime(
                host="",
                token="",
                compose_id="",
                app_name="",
                server_id="",
                service_name="",
            )
            runtime["state"] = "exited"
            runtime["running"] = False
            return runtime

        result = inspect_dokploy_target(
            record_store=_READY_STORE,
            host="https://dokploy.example.invalid",
            token="token",
            request=DokployTargetInspectRequest(
                target_type="compose",
                target_id="compose-123",
                service="launchplane-privileged-operation-workers",
                event="privileged_operation_worker_poll_succeeded",
                expected_image=f"ghcr.io/example/launchplane@sha256:{'a' * 64}",
            ),
            fetch_target_payload=_fetch_target_payload,
            fetch_compose_service_runtime=fetch_stopped_runtime,
            fetch_compose_logs=_fetch_logs,
            now=lambda: _NOW,
        )

        runtime_evidence = result["runtime_evidence"]
        assert isinstance(runtime_evidence, dict)
        self.assertFalse(runtime_evidence["running"])
        self.assertFalse(runtime_evidence["proof_ready"])

    def test_heartbeat_proof_fails_closed_for_invalid_runtime_states(self) -> None:
        cases = (
            ("missing", _HeartbeatStore()),
            ("identity_mismatch", _HeartbeatStore(_heartbeat(runtime_identity="other-worker"))),
            (
                "stale",
                _HeartbeatStore(_heartbeat(last_poll_succeeded_at=_NOW - timedelta(seconds=121))),
            ),
            (
                "future_timestamp",
                _HeartbeatStore(_heartbeat(last_poll_succeeded_at=_NOW + timedelta(seconds=61))),
            ),
            (
                "image_mismatch",
                _HeartbeatStore(
                    _heartbeat(image_reference=f"ghcr.io/example/launchplane@sha256:{'c' * 64}")
                ),
            ),
        )

        for expected_status, store in cases:
            with self.subTest(expected_status=expected_status):
                result = inspect_dokploy_target(
                    record_store=cast(DokployTargetInspectStore, store),
                    host="https://dokploy.example.invalid",
                    token="token",
                    request=DokployTargetInspectRequest(
                        target_type="compose",
                        target_id="compose-123",
                        service="launchplane-privileged-operation-workers",
                        event="privileged_operation_worker_poll_succeeded",
                        expected_image=_IMAGE_REFERENCE,
                    ),
                    fetch_target_payload=_fetch_target_payload,
                    fetch_compose_service_runtime=_fetch_service_runtime,
                    fetch_compose_logs=_fetch_logs,
                    now=lambda: _NOW,
                )

                runtime_evidence = result["runtime_evidence"]
                assert isinstance(runtime_evidence, dict)
                heartbeat = runtime_evidence["worker_heartbeat"]
                assert isinstance(heartbeat, dict)
                self.assertEqual(heartbeat["status"], expected_status)
                self.assertFalse(runtime_evidence["proof_ready"])

    def test_unrelated_fresh_heartbeat_does_not_override_exact_identity_match(self) -> None:
        result = inspect_dokploy_target(
            record_store=cast(
                DokployTargetInspectStore,
                _HeartbeatStore(
                    _heartbeat(),
                    _heartbeat(runtime_identity="replacement-worker"),
                ),
            ),
            host="https://dokploy.example.invalid",
            token="token",
            request=DokployTargetInspectRequest(
                target_type="compose",
                target_id="compose-123",
                service="launchplane-privileged-operation-workers",
                expected_image=_IMAGE_REFERENCE,
            ),
            fetch_target_payload=_fetch_target_payload,
            fetch_compose_service_runtime=_fetch_service_runtime,
            fetch_compose_logs=lambda **_kwargs: (),
            now=lambda: _NOW,
        )

        runtime_evidence = result["runtime_evidence"]
        assert isinstance(runtime_evidence, dict)
        heartbeat = runtime_evidence["worker_heartbeat"]
        assert isinstance(heartbeat, dict)
        self.assertEqual(heartbeat["status"], "ready")
        self.assertEqual(heartbeat["fresh_worker_count"], 2)
        self.assertTrue(runtime_evidence["proof_ready"])

    def test_application_target_rejects_runtime_service_evidence(self) -> None:
        with self.assertRaisesRegex(ValueError, "supports compose targets only"):
            inspect_dokploy_target(
                record_store=_UNUSED_STORE,
                host="https://dokploy.example.invalid",
                token="token",
                request=DokployTargetInspectRequest(
                    target_type="application",
                    target_id="application-123",
                    service="worker",
                    event="privileged_operation_worker_poll_succeeded",
                    expected_image=f"example@sha256:{'a' * 64}",
                ),
                fetch_target_payload=_fetch_target_payload,
            )

    def test_log_fetch_failure_is_bounded_diagnostic(self) -> None:
        def fetch_log_failure(**kwargs: str | int) -> tuple[str, ...]:
            del kwargs
            raise dokploy_runtime_evidence.DokployEvidenceProviderError("runtime-log-read")

        result = inspect_dokploy_target(
            record_store=_READY_STORE,
            host="https://dokploy.example.invalid",
            token="token",
            request=DokployTargetInspectRequest(
                target_type="compose",
                target_id="compose-123",
                service="launchplane-privileged-operation-workers",
                event="privileged_operation_worker_poll_succeeded",
                expected_image=_IMAGE_REFERENCE,
            ),
            fetch_target_payload=_fetch_target_payload,
            fetch_compose_service_runtime=_fetch_service_runtime,
            fetch_compose_logs=fetch_log_failure,
            now=lambda: _NOW,
        )

        runtime_evidence = result["runtime_evidence"]
        assert isinstance(runtime_evidence, dict)
        structured_event = runtime_evidence["structured_event"]
        assert isinstance(structured_event, dict)
        self.assertEqual(structured_event["log_read_status"], "unavailable")
        self.assertFalse(structured_event["observed"])
        self.assertTrue(runtime_evidence["proof_ready"])


if __name__ == "__main__":
    unittest.main()
