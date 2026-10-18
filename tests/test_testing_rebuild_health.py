"""Exercise the public adapter over HTTP, including refused redirects and bad bodies."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from threading import Thread
from typing import Any
import unittest

from control_plane.contracts.runtime_identity import RuntimeIdentity
from control_plane.testing_rebuild_service import read_public_testing_health


class TestingPublicHealthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.identity = RuntimeIdentity(
            product="example",
            context="example",
            instance="testing",
            deployment_record_id="candidate",
            artifact_id="build",
            source_git_ref="source",
        )
        self.status = 200
        self.body = json.dumps(
            {"status": "pass", "runtime_identity": self.identity.model_dump(mode="json")}
        ).encode()
        self.requests: list[str] = []
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                fixture.requests.append(self.path)
                self.send_response(fixture.status)
                if fixture.status == 302:
                    self.send_header("Location", "/another-origin-health")
                self.end_headers()
                self.wfile.write(fixture.body)

            def log_message(self, format: str, *args: Any) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close)
        self.base_url = "http://127.0.0.1:" + str(self.server.server_port)

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.assertFalse(self.thread.is_alive())

    def test_healthy_origin_returns_its_actual_runtime_identity(self) -> None:
        health = read_public_testing_health(self.base_url, "/launchplane/health")
        self.assertTrue(health.verified)
        self.assertEqual(health.status, "pass")
        self.assertEqual(health.observed_runtime_identity, self.identity)
        self.assertEqual(health.urls, (self.base_url + "/launchplane/health",))

    def test_redirect_is_a_failed_same_origin_probe_and_is_never_followed(self) -> None:
        self.status = 302
        health = read_public_testing_health(self.base_url, "/health")
        self.assertEqual(health.status, "fail")
        self.assertTrue(health.verified)
        self.assertEqual(self.requests, ["/health"])

    def test_unparseable_body_does_not_become_a_verified_failure_or_success(self) -> None:
        self.body = b"unparseable"
        health = read_public_testing_health(self.base_url, "/health")
        self.assertFalse(health.verified)
        self.assertEqual(health.status, "pending")
