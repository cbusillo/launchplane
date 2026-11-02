import base64
from contextlib import redirect_stdout
import io
from http.client import IncompleteRead
import json
import time
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock, Mock, patch
from urllib.error import URLError

from control_plane.dokploy import cutover_probes
from control_plane.dokploy.cutover_host import BoundOdooCutoverHost, readiness_container_state
from control_plane.dokploy.cutover_probes import CutoverProbes, PrivateHealthFailed
from control_plane.dokploy.cutover_transport import CutoverHostTransport
from control_plane.dokploy.native_copy_stream import NativeSourceUnavailable, NativeProgramFailed
from tests.support.prepared_public_fixture import WebsiteFixture
from tests.test_odoo_cutover import release_plan


class CutoverReadinessDiagnosticTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = self.enterContext(TemporaryDirectory())
        fixture = WebsiteFixture(Path(temporary))
        self.addCleanup(fixture.engine.dispose)
        self.plan = release_plan(fixture)

    def test_private_http_reports_connection_failure_without_request_or_exception_text(
        self,
    ) -> None:
        for reason in (ConnectionRefusedError("secret"), TimeoutError("secret")):
            failure = self.private_http_failure(Mock(open=Mock(side_effect=URLError(reason))))
            self.assertEqual(
                "connection_refused" if isinstance(reason, ConnectionRefusedError) else "timeout",
                failure.kind,
            )

    def test_http_protocol_and_body_limit_failures_remain_distinct_from_exec_failure(self) -> None:
        for result, category in (
            (IncompleteRead(b"secret"), "protocol_error"),
            (TimeoutError("secret"), "timeout"),
            (
                b"secret" * (cutover_probes.PRIVATE_RESOURCE_BYTES // len(b"secret") + 1),
                "resource_exceeds_bound",
            ),
        ):
            with self.subTest(category=category):
                response = MagicMock()
                response.__enter__.return_value = response
                response.read.side_effect = result if isinstance(result, Exception) else None
                response.read.return_value = result
                failure = self.private_http_failure(Mock(open=Mock(return_value=response)))
                self.assertEqual(failure.kind, category)

    def private_http_failure(self, opener: Mock) -> PrivateHealthFailed:
        output = io.StringIO()
        with (
            patch(
                "sys.stdin",
                io.StringIO(
                    json.dumps(
                        {
                            "url": "http://private.invalid/secret",
                            "host": "secret.invalid",
                            "database": "secret",
                        }
                    )
                ),
            ),
            patch("urllib.request.build_opener", return_value=opener),
            redirect_stdout(output),
        ):
            exec(cutover_probes.HTTP_OBSERVATION_PROGRAM, {})
        self.assertNotIn("secret", output.getvalue())
        probes = CutoverProbes(
            self.plan, Mock(return_value=output.getvalue()), Mock(return_value="runner")
        )
        with self.assertRaises(PrivateHealthFailed) as failure:
            probes.private_health(self.plan.new)
        return failure.exception

    def test_http_status_and_invalid_or_foreign_health_body_remain_distinct_failures(self) -> None:
        for status, body, category in (
            (503, b"secret upstream body", "http_status"),
            (200, b"secret invalid json", "invalid_health"),
            (200, b"{}", "invalid_health"),
            (
                200,
                json.dumps(
                    {"runtime_identity": self.plan.old.runtime_identity.model_dump(mode="json")}
                ).encode(),
                "identity_mismatch",
            ),
        ):
            with self.subTest(status=status, body=body):
                execute = Mock(
                    return_value=json.dumps(
                        {"status": status, "headers": {}, "body": base64.b64encode(body).decode()}
                    )
                )
                probes = CutoverProbes(self.plan, execute, Mock(return_value="runner"))
                with self.assertRaises(PrivateHealthFailed) as failure:
                    probes.private_health(self.plan.new)
                self.assertEqual(category, failure.exception.kind)
                self.assertNotIn("secret", str(failure.exception))

        execute.return_value = json.dumps(
            {
                "status": 200,
                "headers": {},
                "body": base64.b64encode(
                    json.dumps(
                        {"runtime_identity": self.plan.new.runtime_identity.model_dump(mode="json")}
                    ).encode()
                ).decode(),
            }
        )
        self.assertTrue(probes.private_health(self.plan.new).verified)

    def test_unavailable_or_malformed_state_never_leaks_provider_values(self) -> None:
        transport = Mock(spec=CutoverHostTransport)
        for result in (
            NativeSourceUnavailable("secret"),
            "secret",
            '"secret" false true 1\n"running" true false 0',
        ):
            with self.subTest(result=type(result).__name__):
                transport.read.side_effect = result if isinstance(result, Exception) else None
                transport.read.return_value = result
                self.assertEqual(
                    readiness_container_state(transport, "secret"),
                    {"status": "unavailable"},
                )

    def test_health_retry_can_succeed_without_failure_diagnostic_reads(self) -> None:
        host = self.readiness_host()
        host.probes.private_health.side_effect = [PrivateHealthFailed("connection_refused"), Mock()]
        host.observer.native_ready.side_effect = RuntimeError("past health check")
        with (
            patch.object(time, "monotonic", side_effect=[10, 10]),
            patch.object(time, "sleep"),
            self.assertRaisesRegex(RuntimeError, "past health check"),
        ):
            BoundOdooCutoverHost.readiness(host, self.plan, self.plan.new)
        self.assertEqual(host.probes.private_health.call_count, 2)
        host.transport.read.assert_not_called()

    def test_readiness_deadline_keeps_fences_and_reports_attempts_and_safe_container_state(
        self,
    ) -> None:
        host = self.readiness_host()
        host.probes.private_health.side_effect = NativeProgramFailed("secret probe body")
        host.transport.read.return_value = '"exited" false true 137\n'
        with (
            patch.object(time, "monotonic", side_effect=[10, 10, 11, 12]),
            patch.object(time, "sleep") as sleep,
            self.assertRaises(ValueError) as failure,
        ):
            BoundOdooCutoverHost.readiness(host, self.plan, self.plan.new)
        self.assertNotIn("secret", str(failure.exception))
        diagnostic = json.loads(str(failure.exception))
        self.assertEqual(diagnostic["attempts"], host.probes.private_health.call_count)
        self.assertEqual(diagnostic["elapsed_seconds"], 2)
        self.assertEqual(diagnostic["timeout_seconds"], host.runtime.readiness_timeout_seconds)
        self.assertEqual(diagnostic["failure"]["kind"], "transport_unavailable")
        self.assertTrue(diagnostic["container"]["oom_killed"])
        self.assertFalse(diagnostic["container"]["running"])
        host.runner.assert_not_called()
        self.assertEqual(sleep.call_count, 2)
        host.writers.require_drained.assert_called_once()
        host.observer.native_ready.assert_not_called()
        host.transport.assert_not_called()

    @staticmethod
    def readiness_host() -> Mock:
        host = Mock(spec=BoundOdooCutoverHost)
        host.runtime, host.writers, host.probes, host.transport, host.observer = (
            Mock() for _ in range(5)
        )
        host.native_writer_capability = Mock()
        host.runtime.readiness_timeout_seconds = 2
        host.writers.targets = ()
        host.writers._running.return_value = True
        return host
