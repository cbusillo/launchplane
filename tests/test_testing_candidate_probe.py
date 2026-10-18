"""Run the fixed host/container programs with simulated provider boundaries."""

import base64
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from typing import Any, cast
import unittest
from unittest.mock import patch
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
                result = b"integration_readback_ok=true\n"
            elif (
                "-c" in command
                and command[command.index("-c") + 1] == probe.CANDIDATE_CONTAINER_PROBE
            ):
                result = b"candidate_credentials_verified=true\n" if self.credentials_ok else b""
            elif "-c" in command and command[command.index("-c") + 1].startswith(
                "import hashlib,os,sys"
            ):
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
        import sys

        output = io.StringIO()
        encoded = base64.b64encode(json.dumps(self.spec).encode()).decode()
        with (
            patch.object(sys, "argv", ["probe", encoded, "frame:"]),
            patch("subprocess.run", side_effect=PermissionError("fixture denied")),
            redirect_stdout(output),
            self.assertRaises(SystemExit),
        ):
            exec(
                compile(probe.CANDIDATE_HOST_PROBE, "<candidate-probe>", "exec"),
                {"__name__": "__main__"},
            )
        frame = json.loads(output.getvalue().removeprefix("frame:"))
        self.assertEqual(frame["type"], "denied")

    def test_shared_or_published_runtime_refuses_before_login(self) -> None:
        self.fixture.containers["web"]["HostConfig"]["PortBindings"] = {
            "8069/tcp": [{"HostPort": "8069"}]
        }
        with self.assertRaises(RuntimeError):
            self.run_probe()
        self.assertFalse(
            any(probe.CANDIDATE_CONTAINER_PROBE in command for command in self.fixture.calls)
        )


class CandidateContainerProbeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.config = Path(self.temporary.name) / "odoo.conf"
        self.config.write_text("[options]\nadmin_passwd = local-master-fixture\n")
        self.identity = {"deployment_record_id": "candidate-build"}
        self.spec = {
            "base_url": "https://testing.example.invalid",
            "http_port": 8072,
            "runtime_identity": self.identity,
            "web_config": str(self.config),
        }
        self.health_identity = dict(self.identity)
        self.uid = 42
        self.requests: list[Request] = []

    def response(self, request: Any, **kwargs: Any) -> Any:
        self.requests.append(request)
        payload: dict[str, object]
        if request.full_url.endswith("/launchplane/health"):
            payload = {"runtime_identity": self.health_identity}
        else:
            params = json.loads(request.data)["params"]
            self.assertEqual(
                params, {"db": "candidate", "login": "tester", "password": "local-login-fixture"}
            )
            payload = {"result": {"uid": self.uid}}
        result = io.BytesIO(json.dumps(payload).encode())
        result.status = 200  # type: ignore[attr-defined]
        return result

    def run_probe(self) -> str:
        from unittest.mock import Mock

        opener = Mock()
        opener.open.side_effect = self.response
        output = io.StringIO()
        with (
            patch("urllib.request.build_opener", return_value=opener),
            patch("sys.stdin", io.StringIO(json.dumps(self.spec))),
            patch.dict(
                os.environ,
                {
                    "ODOO_DB_NAME": "candidate",
                    "ODOO_ADMIN_LOGIN": "tester",
                    "ODOO_ADMIN_PASSWORD": "local-login-fixture",
                    "ODOO_MASTER_PASSWORD": "local-master-fixture",
                },
            ),
            redirect_stdout(output),
        ):
            exec(compile(probe.CANDIDATE_CONTAINER_PROBE, "<candidate-container>", "exec"), {})
        return output.getvalue()

    def test_health_and_real_local_signin_use_only_loopback_with_stable_host(self) -> None:
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

    def test_wrong_http_identity_login_or_master_credential_refuses(self) -> None:
        self.health_identity = {"deployment_record_id": "other"}
        with self.assertRaises(RuntimeError):
            self.run_probe()
        self.health_identity = dict(self.identity)
        self.uid = 0
        with self.assertRaises(RuntimeError):
            self.run_probe()
        self.uid = 42
        self.config.write_text("[options]\nadmin_passwd = wrong-master\n")
        with self.assertRaises(RuntimeError):
            self.run_probe()
