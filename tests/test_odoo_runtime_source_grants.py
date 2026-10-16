import base64
import configparser
import contextlib
import io
import json
import os
import subprocess
import sys
import unittest
from typing import Any
from unittest.mock import MagicMock, patch

from control_plane import odoo_runtime_reads as reads
from control_plane import odoo_import_diagnostics as diagnostic
from control_plane.dokploy.compose import ODOO_WEB_CONFIG_PATH
from control_plane.odoo_source_probe import GrantStatus
from control_plane import odoo_runtime_source_grants as runtime
from control_plane.contracts.odoo_import_parameters import config_parameter_runtime_key
from control_plane.contracts.runtime_identity import RUNTIME_IDENTITY_ENV_KEY
from control_plane.odoo_instance_overrides import config_parameter_secret_env_key
from tests.test_odoo_import_diagnostics import DiagnosticFixture, PRIVATE


class RuntimeSourceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = DiagnosticFixture()
        self.full_container_id = self.fixture.connection.evidence.container_id
        self.values = self.fixture.applied.copy()
        self.env = {
            "PLATFORM_INSTANCE": "testing",
            "ODOO_DB_NAME": "fixture_db",
            RUNTIME_IDENTITY_ENV_KEY: self.fixture.identity.model_dump_json(),
        }
        self.keys: dict[str, dict[str, str]] = {}
        for name, prefix in diagnostic.SOURCE_PREFIXES.items():
            self.keys[name] = {}
            for field in (*diagnostic.FIELDS, "password"):
                key = (
                    config_parameter_secret_env_key(prefix + ".password")
                    if field == "password"
                    else config_parameter_runtime_key(prefix + "." + field)
                )
                self.keys[name][field] = key
                self.env[key] = self.values[prefix + "." + field]
        self.config = configparser.ConfigParser(interpolation=None)
        self.config["options"] = {
            "db_name": "fixture_db",
            "db_host": "database",
            "db_user": "local_fixture",
            "db_password": PRIVATE,
        }
        self.database = MagicMock()
        self.cursor = self.database.cursor.return_value.__enter__.return_value
        self.cursor.fetchmany.side_effect = lambda bound: list(self.values.items())[:bound]
        self.mysql = MagicMock()
        cursor = self.mysql.cursor.return_value.__enter__.return_value
        cursor.fetchmany.return_value = [
            ("GRANT SELECT ON *.* TO 'runtime_reader'@'runtime-origin'",)
        ]
        self.provider_payloads: list[dict[str, Any]] = []
        self.inspections = 0
        self.drift = ""
        self.source_calls = 0
        self.proc_calls: list[tuple[list[str], dict[str, Any]]] = []

    def mysql_connect(self, **kwargs: Any) -> Any:
        self.source_calls += 1
        if kwargs["host"] == "cm_data.invalid":
            if self.drift == "source_failure":
                raise OSError(PRIVATE)
            if self.drift == "parameter":
                self.values["cm_data.db.password"] = "changed-fixture"
        return self.mysql

    def execute(self, program: str, input_text: str, argv: list[str] | None = None) -> str:
        with (
            patch.object(sys, "stdin", io.StringIO(input_text)),
            patch.object(sys, "argv", argv or ["probe"]),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            exec(compile(program, "<fixed-runtime-probe>", "exec"), {"__name__": "__main__"})
        return output.getvalue()

    def subprocess_run(self, command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.proc_calls.append((command, kwargs))
        if command[:2] == ["docker", "inspect"]:
            self.inspections += 1
            payload: dict[str, Any] = {
                "Id": self.full_container_id,
                "Config": {
                    "Image": self.fixture.identity.image_reference,
                    "Labels": {
                        "com.docker.compose.project": self.fixture.connection.app_name,
                        "com.docker.compose.service": "web",
                    },
                    "Env": [key + "=" + value for key, value in self.env.items()],
                },
                "State": {"Running": True, "StartedAt": "fixture-start"},
                "NetworkSettings": {"Networks": {"fixture-network": {}}},
            }
            if self.drift == "health":
                payload["State"]["Health"] = {"Log": [{"Start": str(self.inspections)}]}
            if self.drift == "image":
                payload["Config"]["Image"] = "foreign-image"
            if self.inspections > 1 and self.drift == "container":
                payload["State"]["StartedAt"] = "new-start"
            return subprocess.CompletedProcess(command, 0, stdout=json.dumps([payload]))
        output = self.execute(command[-1], kwargs["input"])
        return subprocess.CompletedProcess(command, 0, stdout=output)

    def remote(self, **kwargs: Any) -> None:
        payload = kwargs["payload"]
        self.provider_payloads.append(payload)
        encoded = base64.b64encode(json.dumps(payload).encode()).decode()
        output = self.execute(kwargs["program"], "", ["host", encoded, "FRAME:"])
        kwargs["receive"](json.loads(output.removeprefix("FRAME:")))

    def run_probe(self) -> dict[str, GrantStatus]:
        self.postgres = MagicMock(connect=MagicMock(return_value=self.database))
        with (
            patch.dict(os.environ, self.env, clear=True),
            patch.dict(sys.modules, {"psycopg2": self.postgres}),
            patch.object(configparser.ConfigParser, "read", return_value=[ODOO_WEB_CONFIG_PATH]),
            patch.object(
                configparser.ConfigParser, "__getitem__", return_value=self.config["options"]
            ),
            patch("pymysql.connect", side_effect=self.mysql_connect),
            patch.object(subprocess, "run", side_effect=self.subprocess_run),
            patch.object(runtime, "_read_remote", side_effect=self.remote),
        ):
            return runtime.runtime_grants(
                self.fixture.connection,
                tuple(diagnostic.SOURCE_PREFIXES),
                prefixes=diagnostic.SOURCE_PREFIXES,
                fields=diagnostic.FIELDS,
                environment_keys=self.keys,
                observed_identity=self.env[RUNTIME_IDENTITY_ENV_KEY],
            )

    def test_real_program_chain_uses_runtime_origin_without_exporting_credentials(self) -> None:
        self.assertTrue(all(value == "read_only" for value in self.run_probe().values()))
        self.assertGreater(self.source_calls, 0)
        for command, kwargs in self.proc_calls:
            self.assertNotIn(PRIVATE, repr(command))
            self.assertGreater(kwargs["timeout"], 0)
        self.assertNotIn(PRIVATE, json.dumps(self.provider_payloads))
        self.mysql.close.assert_called()

    def test_finalized_timestamp_defaults_short_id_and_health_ticks_preserve_origin_proof(
        self,
    ) -> None:
        identity = json.loads(self.env[RUNTIME_IDENTITY_ENV_KEY])
        identity["deployed_at"] = "2026-10-15T12:01:00Z"
        identity.pop("schema_version")
        identity["source_revision"] = identity.pop("source_git_ref")
        self.env[RUNTIME_IDENTITY_ENV_KEY] = json.dumps(identity)
        self.fixture.connection = self.fixture.connection.__class__(
            **{
                **self.fixture.connection.__dict__,
                "evidence": self.fixture.connection.evidence.model_copy(
                    update={"container_id": self.full_container_id[:12]}
                ),
            }
        )
        self.drift = "health"
        self.assertTrue(all(value == "read_only" for value in self.run_probe().values()))
        for command, _ in self.proc_calls:
            if command[:2] == ["docker", "exec"]:
                self.assertIn(self.full_container_id, command)

    def test_equivalent_verified_tls_settings_preserve_runtime_proof(self) -> None:
        self.env[self.keys["cm_data"]["ssl_verify"]] = "true"
        self.assertEqual(self.run_probe()["cm_data"], "read_only")

    def test_unreachable_source_does_not_hide_other_sources(self) -> None:
        self.drift = "source_failure"
        result = self.run_probe()
        self.assertEqual(result["cm_data"], "unknown")
        self.assertEqual(result["repairshopr"], "read_only")
        self.assertEqual(result["fishbowl"], "read_only")

    def test_runtime_writer_and_role_results(self) -> None:
        cursor = self.mysql.cursor.return_value.__enter__.return_value
        for grants, expected in [
            ([("GRANT SELECT, INSERT ON *.* TO 'reader'@'runtime'",)], "unsafe"),
            ([("GRANT `role`@`%` TO `reader`@`runtime`",)], "unknown"),
            ([("GRANT SELECT ON *.* TO 'reader'@'runtime' WITH GRANT OPTION",)], "unsafe"),
            ([("REVOKE SELECT ON `private`.* FROM 'reader'@'runtime'",)], "unknown"),
        ]:
            with self.subTest(expected=expected, grants=grants):
                cursor.fetchmany.return_value = grants
                self.assertTrue(all(value == expected for value in self.run_probe().values()))

    def test_parameter_binding_tls_and_identity_mismatch_never_authenticate(self) -> None:
        original_env = self.env.copy()
        original_values = self.values.copy()
        for kind in ("password", "host", "user", "db", "port", "ssl_verify", "identity"):
            with self.subTest(kind=kind):
                self.env = original_env.copy()
                self.values = original_values.copy()
                self.source_calls = 0
                if kind == "identity":
                    self.env[RUNTIME_IDENTITY_ENV_KEY] = "{}"
                else:
                    for prefix in diagnostic.SOURCE_PREFIXES.values():
                        self.values[prefix + "." + kind] = "mismatched"
                self.assertTrue(all(value == "unknown" for value in self.run_probe().values()))
                self.assertEqual(self.source_calls, 0)

    def test_config_and_container_drift_discard_all_grants(self) -> None:
        for drift in ("parameter", "container", "image"):
            with self.subTest(drift=drift):
                self.drift = drift
                self.inspections = 0
                self.assertTrue(all(value == "unknown" for value in self.run_probe().values()))

    def test_transport_failure_and_missing_identity_are_value_free(self) -> None:
        original = self.fixture.connection
        with patch.object(runtime, "_read_remote", side_effect=OSError(PRIVATE)):
            self.assertEqual(
                runtime.runtime_grants(
                    original,
                    ("cm_data",),
                    prefixes=diagnostic.SOURCE_PREFIXES,
                    fields=diagnostic.FIELDS,
                    environment_keys=self.keys,
                    observed_identity=self.env[RUNTIME_IDENTITY_ENV_KEY],
                ),
                {"cm_data": "unknown"},
            )
        self.fixture.connection = original.__class__(
            **{**original.__dict__, "diagnostic_identity": None}
        )
        with patch.object(runtime, "_read_remote") as remote:
            with self.assertRaises(reads.OdooRuntimeReadError):
                runtime.runtime_grants(
                    self.fixture.connection,
                    ("cm_data",),
                    prefixes=diagnostic.SOURCE_PREFIXES,
                    fields=diagnostic.FIELDS,
                    environment_keys=self.keys,
                    observed_identity=self.env[RUNTIME_IDENTITY_ENV_KEY],
                )
            remote.assert_not_called()
