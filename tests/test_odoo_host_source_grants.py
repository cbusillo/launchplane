import base64
import contextlib
import io
import json
import socket
import ssl
import subprocess
import sys
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pymysql
from fastapi import FastAPI, HTTPException

from control_plane import odoo_host_source_grants as host_grants
from control_plane import odoo_import_diagnostics as diagnostic
from control_plane import odoo_runtime_reads as reads
from control_plane import odoo_source_probe as probe
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.dokploy import api
from control_plane.dokploy import source as dokploy_source
from control_plane.http_routes.odoo_runtime import (
    OdooRuntimeReadDependencies,
    register_odoo_runtime_read_routes,
)
from control_plane.http_routes.support import ReadRouteDependencies
from tests.support.auth import _identity
from tests.support.http import request
from tests.test_odoo_import_diagnostics import (
    PRIVATE,
    STAMP,
    DiagnosticFixture,
    assert_testing_read_response,
)

PROBE_LABEL = "io.launchplane.source-grant-probe"
TESTING_SERVER = "nonprod-server"
PRODUCTION_SERVER = "production-server"


class SimulatedDocker:
    def __init__(self, image: str) -> None:
        self.image = image
        self.calls: list[tuple[list[str], bytes | None]] = []
        self.containers: dict[str, str] = {}
        self.digests = [image]
        self.labels_override: dict[str, str] | None = None
        self.probe_stdout = json.dumps(
            {"grants": "read_only", "names": ["SELECT"], "failure": None}
        )
        self.start_returncode = 0
        self.fail_create = False
        self.keep_after_remove = False

    def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        assert argv[0] == "docker"
        self.calls.append((argv, kwargs.get("input")))
        reply: subprocess.CompletedProcess[bytes] = getattr(self, "_" + argv[1])(argv[2:])
        return reply

    @staticmethod
    def _reply(stdout: str = "", returncode: int = 0) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess([], returncode, stdout=stdout.encode())

    def _image(self, arguments: list[str]) -> subprocess.CompletedProcess[bytes]:
        if arguments[-1] != self.image:
            return self._reply(returncode=1)
        return self._reply(json.dumps(self.digests))

    def _create(self, arguments: list[str]) -> subprocess.CompletedProcess[bytes]:
        name = arguments[arguments.index("--name") + 1]
        if self.fail_create:
            self.containers[name] = name + "-id"
            return self._reply(returncode=1)
        self.containers[name] = name + "-id"
        return self._reply(name + "-id\n")

    def _start(self, arguments: list[str]) -> subprocess.CompletedProcess[bytes]:
        return self._reply(self.probe_stdout, self.start_returncode)

    def _ps(self, arguments: list[str]) -> subprocess.CompletedProcess[bytes]:
        name = arguments[-1].removeprefix("name=^/").removesuffix("$")
        return self._reply(self.containers.get(name, ""))

    def _inspect(self, arguments: list[str]) -> subprocess.CompletedProcess[bytes]:
        name = arguments[-1].removesuffix("-id")
        labels = self.labels_override if self.labels_override is not None else {PROBE_LABEL: name}
        return self._reply(json.dumps([{"Config": {"Labels": labels}}]))

    def _rm(self, arguments: list[str]) -> subprocess.CompletedProcess[bytes]:
        if not self.keep_after_remove:
            self.containers.pop(arguments[-1].removesuffix("-id"))
        return self._reply()

    def verbs(self) -> list[str]:
        return [call[0][1] for call in self.calls]


class HostSourceGrantTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = DiagnosticFixture()
        self.docker = SimulatedDocker(self.fixture.identity.image_reference)
        self.servers = {
            "fixture-target-id": TESTING_SERVER,
            "production-target-id": PRODUCTION_SERVER,
        }
        self.remote_calls: list[dict[str, Any]] = []
        self.fixture.store.read_dokploy_target_id_record.side_effect = self.target_id_record
        cache = patch.dict(diagnostic._FAILED_PROBES, {}, clear=True)
        cache.start()
        self.addCleanup(cache.stop)
        for active in (
            patch.object(host_grants, "_read_remote", side_effect=self.remote),
            patch.object(
                dokploy_source,
                "read_dokploy_config",
                return_value=("https://provider.invalid", "fixture-provider-token"),
            ),
            patch.object(api, "fetch_dokploy_target_payload", side_effect=self.target_payload),
            patch.object(
                reads, "select_stable_runtime", return_value=self.fixture.connection.selection
            ),
            patch.object(reads, "assert_selection_current"),
            patch.object(host_grants, "_decrypt_secret_value", return_value=PRIVATE),
            patch.object(
                reads,
                "connect_runtime",
                side_effect=AssertionError("the host probe must not need a running runtime"),
            ),
            patch.object(
                reads,
                "diagnostic_authentication",
                side_effect=AssertionError("the host probe must not use an Odoo identity"),
            ),
        ):
            active.start()
            self.addCleanup(active.stop)

    @staticmethod
    def target_id_record(*, context_name: str, instance_name: str) -> DokployTargetIdRecord:
        return DokployTargetIdRecord(
            context=context_name,
            instance=instance_name,
            target_id="fixture-target-id" if instance_name == "testing" else "production-target-id",
            updated_at=STAMP,
        )

    def target_payload(self, *, target_id: str, **_: Any) -> dict[str, str]:
        return {"name": "fixture-target", "serverId": self.servers[target_id]}

    def remote(self, **kwargs: Any) -> None:
        self.remote_calls.append(kwargs)
        encoded = base64.b64encode(json.dumps(kwargs["payload"]).encode()).decode()
        with (
            patch.object(sys, "argv", ["host", encoded, "PREFIX:"]),
            patch("subprocess.run", self.docker),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            exec(compile(kwargs["program"], "<host>", "exec"), {"__name__": "__main__"})
        for line in output.getvalue().splitlines():
            kwargs["receive"](json.loads(line.removeprefix("PREFIX:")))

    def diagnose(self) -> host_grants.HostSourceGrantResult:
        return host_grants.diagnose_host_source_grants(
            store=self.fixture.store,
            product=self.fixture.profile.product,
            control_plane_root=Path("fixture"),
            database_url=None,
        )

    def test_probe_runs_without_any_running_runtime_or_odoo_identity(self) -> None:
        result = self.diagnose()
        self.assertEqual(result.grants, "read_only", result)
        self.assertEqual(result.grant_names, ("SELECT",))
        self.assertIsNone(result.failure_stage)
        self.assertEqual(result.container_cleanup, "removed")
        self.assertEqual(result.rechecks, "current")
        self.assertRegex(result.evidence_sha256, r"^[a-f0-9]{64}$")
        self.assertEqual(self.remote_calls[0]["server_id"], TESTING_SERVER)
        self.assertEqual(self.docker.containers, {})

    def test_password_reaches_only_container_stdin(self) -> None:
        result = self.diagnose()
        self.assertNotIn(PRIVATE, result.model_dump_json())
        starts = [stdin for argv, stdin in self.docker.calls if argv[1] == "start"]
        self.assertEqual(len(starts), 1)
        self.assertIn(PRIVATE.encode(), starts[0] or b"")
        self.assertEqual([argv for argv, _ in self.docker.calls if PRIVATE in " ".join(argv)], [])

    def test_container_runs_recorded_image_read_only_without_env_or_pull(self) -> None:
        self.diagnose()
        create = next(argv for argv, _ in self.docker.calls if argv[1] == "create")
        self.assertIn(self.fixture.identity.image_reference, create)
        self.assertNotIn("--env", create)
        self.assertNotIn("-e", create)
        for flag in ("--read-only", "--interactive"):
            self.assertIn(flag, create)
        self.assertEqual(create[create.index("--pull") + 1], "never")
        self.assertEqual(create[create.index("--cap-drop") + 1], "ALL")
        self.assertEqual(create[create.index("--network") + 1], "host")

    def test_unsafe_grants_report_names_and_never_read_only(self) -> None:
        self.docker.probe_stdout = json.dumps(
            {"grants": "unsafe", "names": ["INSERT", "SELECT"], "failure": None}
        )
        result = self.diagnose()
        self.assertEqual(result.grants, "unsafe")
        self.assertEqual(result.grant_names, ("INSERT", "SELECT"))

    def test_probe_failure_stays_unknown_with_value_free_stage(self) -> None:
        self.docker.probe_stdout = json.dumps({"grants": "unknown", "names": [], "failure": "dns"})
        result = self.diagnose()
        self.assertEqual((result.grants, result.failure_stage), ("unknown", "dns"))

    def test_malformed_container_output_is_discarded_without_echo(self) -> None:
        for stdout in (
            "not json " + PRIVATE,
            json.dumps({"grants": "read_only", "names": [PRIVATE], "failure": None}),
            json.dumps({"grants": "read_only", "names": ["SELECT"], "failure": None, "x": PRIVATE}),
            json.dumps({"grants": PRIVATE, "names": [], "failure": None}),
        ):
            with self.subTest(stdout=stdout[:20]):
                diagnostic._FAILED_PROBES.clear()
                self.docker.probe_stdout = stdout
                result = self.diagnose()
                self.assertEqual(result.grants, "unknown")
                self.assertEqual(result.grant_names, ())
                self.assertNotIn(PRIVATE, result.model_dump_json())
                self.assertEqual(result.container_cleanup, "removed")

    def test_container_is_removed_when_the_probe_exits_nonzero(self) -> None:
        self.docker.start_returncode = 137
        result = self.diagnose()
        self.assertEqual((result.grants, result.failure_stage), ("unknown", "execution"))
        self.assertEqual(result.container_cleanup, "removed")
        self.assertEqual(self.docker.containers, {})

    def test_create_failure_still_removes_the_owned_name(self) -> None:
        self.docker.fail_create = True
        result = self.diagnose()
        self.assertEqual((result.grants, result.failure_stage), ("unknown", "create"))
        self.assertEqual(result.container_cleanup, "removed")
        self.assertEqual(self.docker.containers, {})

    def test_cleanup_refuses_a_container_it_does_not_own(self) -> None:
        self.docker.labels_override = {PROBE_LABEL: "someone-else"}
        result = self.diagnose()
        self.assertEqual(result.container_cleanup, "unverified")
        self.assertNotIn("rm", self.docker.verbs())
        self.assertEqual(result.grants, "unknown")

    def test_lingering_container_reports_cleanup_unverified_and_no_pass(self) -> None:
        self.docker.keep_after_remove = True
        result = self.diagnose()
        self.assertEqual(result.container_cleanup, "unverified")
        self.assertEqual(result.grants, "unknown")

    def test_unverified_image_never_creates_a_container(self) -> None:
        self.docker.digests = ["ghcr.io/example/other@sha256:" + "c" * 64]
        result = self.diagnose()
        self.assertEqual((result.grants, result.failure_stage), ("unknown", "image"))
        self.assertEqual(result.container_cleanup, "not_started")
        self.assertNotIn("create", self.docker.verbs())

    def test_production_host_is_refused_before_any_credential_moves(self) -> None:
        self.servers["production-target-id"] = TESTING_SERVER
        result = self.diagnose()
        self.assertEqual((result.grants, result.failure_stage), ("unknown", "production_host"))
        self.assertEqual(self.remote_calls, [])

    def test_missing_host_or_target_mismatch_is_refused(self) -> None:
        for change, value in (("serverId", ""), ("name", "another-target")):
            with self.subTest(change=change):
                payload = {"name": "fixture-target", "serverId": TESTING_SERVER, change: value}
                with patch.object(api, "fetch_dokploy_target_payload", return_value=payload):
                    result = self.diagnose()
                self.assertEqual((result.grants, result.failure_stage), ("unknown", "target"))
                self.assertEqual(self.remote_calls, [])

    def test_binding_without_a_read_only_allowance_is_refused(self) -> None:
        target = self.fixture.store.read_dokploy_target_record.return_value
        policies = target.policies.model_copy(update={"integration_allowances": ()})
        self.fixture.store.read_dokploy_target_record.return_value = target.model_copy(
            update={"policies": policies}
        )
        result = self.diagnose()
        self.assertEqual((result.grants, result.failure_stage), ("unknown", "binding"))
        self.assertEqual(self.remote_calls, [])

    def test_changed_secret_version_is_refused(self) -> None:
        self.fixture.versions["fishbowl-v1"] = self.fixture.versions["fishbowl-v1"].model_copy(
            update={"version_id": "another"}
        )
        result = self.diagnose()
        self.assertEqual(result.failure_stage, "binding")
        self.assertEqual(self.remote_calls, [])

    def test_unverified_tls_setting_is_refused(self) -> None:
        self.fixture.runtime = self.fixture.runtime.model_copy(
            update={
                "env": {
                    **self.fixture.runtime.env,
                    "ENV_OVERRIDE_CONFIG_PARAM__FISHBOWL__SSL_VERIFY": "False",
                }
            }
        )
        result = self.diagnose()
        self.assertEqual((result.grants, result.failure_stage), ("unknown", "settings"))
        self.assertEqual(self.remote_calls, [])

    def test_host_change_during_probe_discards_the_result(self) -> None:
        original = self.remote

        def move_host(**kwargs: Any) -> None:
            original(**kwargs)
            self.servers["fixture-target-id"] = "another-server"

        with patch.object(host_grants, "_read_remote", side_effect=move_host):
            result = self.diagnose()
        self.assertEqual((result.grants, result.failure_stage), ("unknown", "drift"))
        self.assertEqual(result.grant_names, ())
        self.assertEqual(result.rechecks, "changed")
        self.assertEqual(result.container_cleanup, "removed")

    def test_authority_change_during_probe_discards_the_result(self) -> None:
        original = self.remote

        def rotate(**kwargs: Any) -> None:
            original(**kwargs)
            self.fixture.versions["fishbowl-v1"] = self.fixture.versions["fishbowl-v1"].model_copy(
                update={"created_at": "2026-10-16T12:00:00Z"}
            )

        with patch.object(host_grants, "_read_remote", side_effect=rotate):
            result = self.diagnose()
        self.assertEqual((result.grants, result.failure_stage), ("unknown", "drift"))

    def test_transport_loss_reports_cleanup_unverified_without_text(self) -> None:
        with patch.object(host_grants, "_read_remote", side_effect=RuntimeError(PRIVATE)):
            result = self.diagnose()
        self.assertEqual((result.grants, result.failure_stage), ("unknown", "transport"))
        self.assertEqual(result.container_cleanup, "unverified")
        self.assertNotIn(PRIVATE, result.model_dump_json())

    def test_failure_cools_down_and_success_is_never_cached(self) -> None:
        self.docker.probe_stdout = json.dumps({"grants": "unknown", "names": [], "failure": "auth"})
        self.diagnose()
        self.assertEqual(self.diagnose().failure_stage, "capacity")
        self.assertEqual(len(self.remote_calls), 1)
        diagnostic._FAILED_PROBES.clear()
        self.docker.probe_stdout = json.dumps(
            {"grants": "read_only", "names": ["SELECT"], "failure": None}
        )
        self.diagnose()
        self.diagnose()
        self.assertEqual(len(self.remote_calls), 3)


class ProbeReportTests(unittest.TestCase):
    def report(self, *, rows: object = None, error: Exception | None = None) -> dict[str, Any]:
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchmany.return_value = rows
        settings = {"host": "h", "user": "u", "password": PRIVATE, "report_names": "1"}
        with (
            patch.object(pymysql, "connect", return_value=connection, side_effect=error),
            patch("control_plane.odoo_source_probe.sys.stdin", io.StringIO(json.dumps(settings))),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            probe.main()
        report: dict[str, Any] = json.loads(output.getvalue())
        return report

    def test_report_lists_privilege_names_without_account_or_host(self) -> None:
        report = self.report(rows=(("GRANT SELECT, SHOW VIEW ON *.* TO 'reader'@'%'",),))
        self.assertEqual(
            report, {"grants": "read_only", "names": ["SELECT", "SHOW VIEW"], "failure": None}
        )

    def test_writer_grant_is_unsafe_and_named(self) -> None:
        report = self.report(rows=(("GRANT SELECT, INSERT ON `cm`.* TO 'reader'@'%'",),))
        self.assertEqual(report["grants"], "unsafe")
        self.assertEqual(report["names"], ["INSERT", "SELECT"])

    def test_role_and_unrecognized_rows_stay_unknown_and_unnamed(self) -> None:
        report = self.report(
            rows=(
                ("GRANT SELECT ON *.* TO 'reader'@'%'",),
                ("GRANT `secret_role`@`%` TO 'reader'@'%'",),
            )
        )
        self.assertEqual(report["grants"], "unknown")
        self.assertEqual(report["names"], ["SELECT"])
        self.assertNotIn("secret_role", json.dumps(report))

    def test_connection_failures_map_to_fixed_words_only(self) -> None:
        wrapped = pymysql.err.OperationalError(2003, f"cannot connect {PRIVATE}")
        for cause, expected in (
            (socket.gaierror(-2, "name"), "dns"),
            (ssl.SSLCertVerificationError(PRIVATE), "tls"),
            (socket.timeout(PRIVATE), "timeout"),
            (ConnectionRefusedError(PRIVATE), "tcp"),
        ):
            with self.subTest(expected=expected):
                setattr(wrapped, "original_exception", cause)
                report = self.report(error=wrapped)
                self.assertEqual(report, {"grants": "unknown", "names": [], "failure": expected})

    def test_rejected_login_is_auth_and_carries_no_server_text(self) -> None:
        report = self.report(error=pymysql.err.OperationalError(1045, f"denied {PRIVATE}"))
        self.assertEqual(report, {"grants": "unknown", "names": [], "failure": "auth"})

    def test_existing_consumers_keep_the_status_only_shape(self) -> None:
        connection = MagicMock()
        connection.cursor.return_value.__enter__.return_value.fetchmany.return_value = (
            ("GRANT SELECT ON *.* TO 'reader'@'%'",),
        )
        settings = {"host": "h", "user": "u", "password": PRIVATE}
        with (
            patch.object(pymysql, "connect", return_value=connection),
            patch("control_plane.odoo_source_probe.sys.stdin", io.StringIO(json.dumps(settings))),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            probe.main()
        self.assertEqual(json.loads(output.getvalue()), {"grants": "read_only"})


class HostGrantRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_route_is_testing_only_authorized_and_accepts_no_input(self) -> None:
        fixture = DiagnosticFixture()
        allowed = MagicMock(return_value=True)

        def http_error(**kwargs: Any) -> HTTPException:
            return HTTPException(
                status_code=kwargs["status_code"],
                detail={"code": kwargs["code"], "message": kwargs["message"]},
            )

        app = FastAPI()
        register_odoo_runtime_read_routes(
            app,  # type: ignore[arg-type]
            dependencies=OdooRuntimeReadDependencies(
                common=ReadRouteDependencies(
                    read_identity=lambda: _identity(),
                    get_record_store=lambda: fixture.store,
                    next_trace_id=lambda: "fixture-trace",
                    authorization_allows=allowed,
                    http_error=http_error,
                    error_response_model=diagnostic.ImportDiagnosticResult,
                ),
                control_plane_root=Path("fixture"),
                database_url=None,
            ),
        )
        path = (
            f"/v1/products/{fixture.profile.product}/environments/testing/odoo-source-grants/host"
        )
        with patch(
            "control_plane.http_routes.odoo_runtime.diagnose_host_source_grants",
            return_value=host_grants.HostSourceGrantResult(),
        ) as execute:
            response = await request(app, "GET", path)
            assert_testing_read_response(self, response, allowed, fixture.profile.product)
            self.assertEqual(response.json()["result"]["grants"], "unknown")
            execute.reset_mock()
            for bad_path in (
                path.replace("/testing/", "/prod/"),
                path + "?host=override.invalid",
                path + "?image=other",
                path + "?sql=SELECT",
            ):
                self.assertEqual((await request(app, "GET", bad_path)).status_code, 400)
            self.assertEqual(
                (await request(app, "POST", path, payload={"password": PRIVATE})).status_code, 405
            )
            allowed.return_value = False
            self.assertEqual((await request(app, "GET", path)).status_code, 403)
            execute.assert_not_called()
