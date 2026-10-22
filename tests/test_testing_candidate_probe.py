"""Run the fixed host/container programs with simulated provider boundaries."""

import base64
from contextlib import nullcontext, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from threading import Thread
from typing import Any, cast
import unittest
from unittest.mock import MagicMock, Mock, patch
from urllib.request import Request

from control_plane import testing_candidate_probe as probe
from tests import test_odoo_testing_restore as copy_tests


class CandidateHostProbeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = copy_tests.FreshTestingHostProgramTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.spec = self.fixture.spec
        self.identity = {"deployment_record_id": "candidate-build"}
        self.spec.update(
            runtime_identity=self.identity,
            expected_overrides_payload="fixture-settings",
            copy_binding_sha256="original-copy",
            witness_sql="fixture witness",
            credential_program=base64.b64encode(probe.CANDIDATE_CONTAINER_PROBE.encode()).decode(),
            readback_program=base64.b64encode(b"fixture integration program").decode(),
            base_url="https://testing.example.invalid",
            web_config="/tmp/config",
        )
        for container in self.fixture.containers.values():
            container["State"] = {"Status": "running"}
            container["Image"] = "fixture-image"
            container["Config"]["Env"].extend(
                [
                    "LAUNCHPLANE_RUNTIME_IDENTITY_JSON=" + json.dumps(self.identity),
                    "ODOO_INSTANCE_OVERRIDES_PAYLOAD_B64=fixture-settings",
                ]
            )
        self.fixture.containers["web"]["Config"]["Labels"]["traefik.enable"] = "false"
        self.fixture.containers["web"]["NetworkSettings"]["Networks"]["dokploy-network"][
            "Aliases"
        ] = [self.fixture.gen.namespace]
        self.witness = "original-copy"
        self.credentials_ok = True
        self.gate_ok = True
        self.image_ok = True
        self.container_probe: CandidateContainerProbeTests | None = None
        self.integration_error: bytes | None = None
        namespace: dict[str, Any] = {"__name__": "probe_test"}
        exec(compile(probe.CANDIDATE_HOST_PROBE, "<candidate-probe>", "exec"), namespace)
        self.program = namespace["probe"]

    def command(self, command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        result = b""
        if command[:3] == ["docker", "image", "inspect"]:
            result = json.dumps(
                [{"RepoDigests": [self.spec["image_reference"]] if self.image_ok else []}]
            ).encode()
        elif command[:2] == ["docker", "exec"]:
            data = kwargs.get("input")
            if data == b"fixture witness":
                result = self.witness.encode()
            elif data == b"fixture integration program":
                if self.integration_error is not None:
                    self.fixture.calls.append(command)
                    return subprocess.CompletedProcess(
                        command, 1, stdout=b"", stderr=self.integration_error
                    )
                result = b"integration_readback_ok=true\n"
            elif (
                "-c" in command
                and command[command.index("-c") + 1] == probe.CANDIDATE_CONTAINER_PROBE
            ):
                if self.container_probe is not None:
                    import traceback

                    try:
                        result = self.container_probe.run_probe().encode()
                    except Exception:
                        return subprocess.CompletedProcess(
                            command, 1, stdout=b"", stderr=traceback.format_exc().encode()
                        )
                else:
                    result = (
                        b"candidate_credentials_verified=true\n" if self.credentials_ok else b""
                    )
            elif command[2:4] == [self.fixture.containers["script-runner"]["Id"], "python3"]:
                if not self.gate_ok:
                    return subprocess.CompletedProcess(
                        command, 1, stdout=b"", stderr=b"private gate error"
                    )
            else:
                self.fail("Unexpected candidate docker exec")
        else:
            return self.fixture.command(command, **kwargs)
        self.fixture.calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout=result, stderr=b"")

    def run_probe(self) -> dict[str, object]:
        with patch("subprocess.run", side_effect=self.command):
            return cast(dict[str, object], self.program(self.spec))

    def failure_frame(self, side_effect: object | None = None) -> dict[str, object]:
        import sys

        output = io.StringIO()
        encoded = base64.b64encode(json.dumps(self.spec).encode()).decode()
        with (
            patch.object(sys, "argv", ["probe", encoded, "frame:"]),
            patch(
                "subprocess.run", side_effect=self.command if side_effect is None else side_effect
            ),
            redirect_stdout(output),
            self.assertRaises(SystemExit),
        ):
            exec(
                compile(probe.CANDIDATE_HOST_PROBE, "<candidate-probe>", "exec"),
                {"__name__": "__main__"},
            )
        return cast(dict[str, object], json.loads(output.getvalue().removeprefix("frame:")))

    def test_private_probe_checks_installed_copy_and_never_stops_or_restarts_anything(self) -> None:
        self.assertTrue(self.run_probe()["private_readiness_verified"])
        self.assertFalse(
            any(command[1] in {"stop", "start", "rm", "cp"} for command in self.fixture.calls)
        )

    def test_changed_copy_image_gate_or_credentials_refuses(self) -> None:
        for field, bad in (
            ("witness", "other-copy"),
            ("image_ok", False),
            ("gate_ok", False),
            ("credentials_ok", False),
        ):
            with self.subTest(field=field):
                original = getattr(self, field)
                setattr(self, field, bad)
                with self.assertRaises(RuntimeError):
                    self.run_probe()
                setattr(self, field, original)

    def test_provider_permission_refusal_remains_a_denial_on_the_wire(self) -> None:
        self.assertEqual(self.failure_frame(PermissionError("fixture denied")), {"type": "denied"})

    def test_bad_database_password_is_a_failed_probe_not_a_provider_denial(self) -> None:
        container_probe = CandidateContainerProbeTests()
        container_probe.setUp()
        self.addCleanup(container_probe.doCleanups)
        container_probe.driver.connect.side_effect = container_probe.driver.Error(
            "password authentication failed for user fixture"
        )
        self.container_probe = container_probe
        self.assertEqual(self.failure_frame()["kind"], "unavailable")

    def test_integration_readback_errors_fail_before_the_credential_program(self) -> None:
        for error in (
            b'psycopg2.OperationalError: password authentication failed for user "fixture"',
            b"psycopg2.errors.InsufficientPrivilege: permission denied for table res_users",
            b"PermissionError: permission denied opening candidate configuration",
        ):
            with self.subTest(error=error):
                self.integration_error = error
                self.fixture.calls.clear()
                self.assertEqual(self.failure_frame()["kind"], "unavailable")
                self.assertFalse(
                    any(
                        probe.CANDIDATE_CONTAINER_PROBE in command for command in self.fixture.calls
                    )
                )

    def test_docker_daemon_refusal_during_readback_remains_denied(self) -> None:
        for error in (
            b"permission denied while trying to connect to the Docker daemon socket",
            b"permission denied while trying to connect to the docker API at unix:///socket",
            b"Error response from daemon: authorization denied by plugin: fixture",
            b"Error response from daemon: unauthorized",
            b"Error response from daemon: access denied",
        ):
            with self.subTest(error=error):
                self.integration_error = error
                self.assertEqual(self.failure_frame(), {"type": "denied"})

    def test_shared_or_published_runtime_refuses_before_credential_check(self) -> None:
        self.fixture.containers["web"]["HostConfig"]["PortBindings"] = {
            "8069/tcp": [{"HostPort": "8069"}]
        }
        frame = self.failure_frame()
        from control_plane.copy_readback_diagnostics import ReadbackPredicate

        self.assertEqual(frame["predicate"], ReadbackPredicate.PORTS)
        self.assertEqual(frame["kind"], "refused")
        self.assertFalse(
            any(probe.CANDIDATE_CONTAINER_PROBE in command for command in self.fixture.calls)
        )


class CandidateContainerProbeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.config = Path(self.temporary.name) / "odoo.conf"
        self.config_text = (
            "[options]\nadmin_passwd = local-master-fixture\n"
            "db_host = database\ndb_name = candidate\ndb_user = local-user\n"
            "db_password = local-database-fixture\n"
        )
        self.config.write_text(self.config_text)
        self.identity = {"deployment_record_id": "candidate-build"}
        self.spec = {
            "base_url": "https://testing.example.invalid",
            "http_port": 8072,
            "runtime_identity": self.identity,
            "web_config": str(self.config),
        }
        self.health_identity = dict(self.identity)
        self.rows = [("encoded-local-admin",)]
        self.connection = MagicMock()
        self.cursor = self.connection.cursor.return_value.__enter__.return_value
        self.cursor.fetchall.side_effect = lambda: self.rows
        self.driver = SimpleNamespace(
            connect=Mock(return_value=self.connection),
            Error=type("OperationalError", (Exception,), {}),
        )
        self.context = Mock()
        self.context.verify.side_effect = lambda password, stored: (
            (password, stored)
            in {
                ("local-login-fixture", "encoded-local-admin"),
            }
        )
        self.requests: list[Request] = []

    def response(self, request: Any, **kwargs: Any) -> Any:
        self.requests.append(request)
        payload: dict[str, object]
        if request.full_url.endswith("/launchplane/health"):
            payload = {"runtime_identity": self.health_identity}
        else:
            # Model the old endpoint's successful response so the regression
            # fails on its POST rather than on an unsupported test fixture.
            payload = {"result": {"uid": 42}}
        result = io.BytesIO(json.dumps(payload).encode())
        result.status = 200  # type: ignore[attr-defined]
        return result

    def run_probe(self, *, admin_login: str | None = "tester", real_http: bool = False) -> str:
        opener = Mock()
        opener.open.side_effect = self.response
        output = io.StringIO()
        with (
            nullcontext()
            if real_http
            else patch("urllib.request.build_opener", return_value=opener),
            patch.dict(
                "sys.modules",
                {
                    "psycopg2": self.driver,
                    "passlib": SimpleNamespace(),
                    "passlib.context": SimpleNamespace(
                        CryptContext=Mock(return_value=self.context)
                    ),
                },
            ),
            patch("sys.stdin", io.StringIO(json.dumps(self.spec))),
            patch.dict(
                os.environ,
                {
                    "ODOO_DB_NAME": "candidate",
                    "ODOO_DB_USER": "local-user",
                    "ODOO_DB_PASSWORD": "local-database-fixture",
                    "ODOO_ADMIN_LOGIN": "tester",
                    "ODOO_ADMIN_PASSWORD": "local-login-fixture",
                    "ODOO_MASTER_PASSWORD": "local-master-fixture",
                },
            ),
            redirect_stdout(output),
        ):
            if admin_login is None:
                os.environ.pop("ODOO_ADMIN_LOGIN", None)
            else:
                os.environ["ODOO_ADMIN_LOGIN"] = admin_login
            exec(compile(probe.CANDIDATE_CONTAINER_PROBE, "<candidate-container>", "exec"), {})
        return output.getvalue()

    def test_health_and_local_credentials_use_only_loopback_with_stable_host(self) -> None:
        output = self.run_probe()
        self.assertEqual(output.strip(), "candidate_credentials_verified=true")
        self.assertTrue(
            all(
                request.full_url.startswith(f"http://127.0.0.1:{self.spec['http_port']}/")
                for request in self.requests
            )
        )
        self.assertTrue(
            all(
                request.get_header("Host") == "testing.example.invalid" for request in self.requests
            )
        )
        self.assertNotIn("local-login-fixture", output)
        self.assertNotIn("local-master-fixture", output)
        self.assertNotIn("local-database-fixture", output)

    def test_readiness_does_not_create_authenticated_use(self) -> None:
        self.run_probe()
        self.run_probe()
        self.assertTrue(all(request.get_method() == "GET" for request in self.requests))
        self.assertEqual(self.cursor.execute.call_args.args[1], ("tester",))
        self.assertEqual(self.connection.commit.call_count, 0)
        self.assertEqual(self.connection.rollback.call_count, 2)
        self.assertEqual(self.connection.close.call_count, 2)

    def test_unset_empty_and_whitespace_login_use_startup_default(self) -> None:
        for login in (None, "", "  "):
            with self.subTest(login=login):
                self.run_probe(admin_login=login)
                self.assertEqual(self.cursor.execute.call_args.args[1], ("admin",))
        self.run_probe(admin_login=" tester ")
        self.assertEqual(self.cursor.execute.call_args.args[1], ("tester",))

    def test_wrong_http_identity_admin_or_master_credential_refuses(self) -> None:
        self.health_identity = {"deployment_record_id": "other"}
        with self.assertRaises(RuntimeError):
            self.run_probe()
        self.health_identity = dict(self.identity)
        self.rows = [("encoded-other-admin",)]
        with self.assertRaises(RuntimeError):
            self.run_probe()
        self.rows = [("encoded-local-admin",)]
        self.config.write_text(self.config_text.replace("local-master-fixture", "wrong-master"))
        with self.assertRaises(RuntimeError):
            self.run_probe()

    def test_missing_or_ambiguous_admin_refuses_and_closes_read_only_connection(self) -> None:
        for rows in ([], [("",)], self.rows * 2):
            with self.subTest(rows=len(rows)):
                self.rows = rows
                with self.assertRaises(RuntimeError):
                    self.run_probe()
                self.connection.rollback.assert_called()
                self.connection.close.assert_called()

    def test_database_config_drift_refuses_before_connecting(self) -> None:
        for before, after in (
            ("database", "outside-db"),
            ("candidate", "other-database"),
            ("local-user", "other-user"),
            ("local-database-fixture", "other-password"),
        ):
            with self.subTest(field=before):
                self.config.write_text(self.config_text.replace(before, after))
                with self.assertRaises(RuntimeError):
                    self.run_probe()
        self.driver.connect.assert_not_called()

    def test_proxy_environment_cannot_redirect_the_private_health_request(self) -> None:
        calls: list[int] = []
        payload = json.dumps({"runtime_identity": self.identity}).encode()

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                calls.append(self.server.server_port)  # type: ignore[attr-defined]
                self.send_response(200)
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, format: str, *args: Any) -> None:
                pass

        with (
            ThreadingHTTPServer(("127.0.0.1", 0), Handler) as health,
            ThreadingHTTPServer(("127.0.0.1", 0), Handler) as proxy,
        ):
            threads = [Thread(target=server.serve_forever) for server in (health, proxy)]
            for thread in threads:
                thread.start()
            self.spec["http_port"] = health.server_port
            try:
                with (
                    patch.dict(os.environ, {"http_proxy": f"http://127.0.0.1:{proxy.server_port}"}),
                    patch("urllib.request.proxy_bypass", return_value=False),
                ):
                    self.run_probe(real_http=True)
            finally:
                for server in (health, proxy):
                    server.shutdown()
                for thread in threads:
                    thread.join()
        self.assertEqual(calls, [health.server_port])
