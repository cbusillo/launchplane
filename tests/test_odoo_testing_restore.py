import base64
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from typing import Any
import unittest
from unittest.mock import Mock, patch

from control_plane.odoo_testing_restore import (
    FRESH_TESTING_LOCAL_CREDENTIAL_KEYS,
    FRESH_TESTING_OMITTED_SETTINGS,
    FRESH_TESTING_RESTORE_HOST_PROGRAM,
    FreshOdooTestingGeneration,
    generation_binding,
    narrow_fresh_testing_requirements,
    require_distinct_testing_credentials,
)
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    product_config_requirement_applies_to_lane,
)
from tests.support.profiles import _odoo_profile_payload_with_prod_lane


def generation() -> FreshOdooTestingGeneration:
    return FreshOdooTestingGeneration(
        product="example-odoo",
        context="example",
        operation_id="held-example",
        target_id="fresh-compose",
        namespace="lp-testing-" + "a" * 32,
        domain_host="fresh.example.invalid",
        resources_binding_sha256="b" * 64,
        bootstrap_plan_sha256="c" * 64,
        request_sha256="d" * 64,
        prepared_at="2026-10-15T00:00:00Z",
    )


class TestingCredentialProofTests(unittest.TestCase):
    def setUp(self) -> None:
        self.profile = LaunchplaneProductProfileRecord.model_validate(
            _odoo_profile_payload_with_prod_lane("example-odoo")
        )
        self.context = next(
            lane.context for lane in self.profile.lanes if lane.instance == "testing"
        )

    def test_values_are_compared_including_cross_key_reuse_without_exposing_them(self) -> None:
        testing = {key: "testing-" + key for key in FRESH_TESTING_LOCAL_CREDENTIAL_KEYS}
        source = {key: "source-" + key for key in FRESH_TESTING_LOCAL_CREDENTIAL_KEYS}
        with patch(
            "control_plane.secrets.resolve_secret_values_for_integration_from_store",
            side_effect=[testing, source],
        ):
            require_distinct_testing_credentials(Mock(), self.profile, self.context)
        source["ODOO_MASTER_PASSWORD"] = testing["ODOO_ADMIN_PASSWORD"]
        with patch(
            "control_plane.secrets.resolve_secret_values_for_integration_from_store",
            side_effect=[testing, source],
        ):
            with self.assertRaises(ValueError) as error:
                require_distinct_testing_credentials(Mock(), self.profile, self.context)
        self.assertFalse(any(value in str(error.exception) for value in testing.values()))

    def test_missing_existing_versions_cannot_be_treated_as_isolated(self) -> None:
        with patch(
            "control_plane.secrets.resolve_secret_values_for_integration_from_store",
            side_effect=[{}, {}],
        ):
            with self.assertRaises(ValueError):
                require_distinct_testing_credentials(Mock(), self.profile, self.context)

    def test_narrowing_requirements_preserves_effective_production_contract(self) -> None:
        from control_plane.contracts.product_profile_record import ProductSecretConfigRequirement

        profile = self.profile.model_copy(
            update={
                "expected_config": self.profile.expected_config.model_copy(
                    update={
                        "managed_secret_bindings": (
                            ProductSecretConfigRequirement(
                                integration="runtime_environment", binding_key="ODOO_KEY"
                            ),
                            ProductSecretConfigRequirement(
                                integration="runtime_environment", binding_key="ODOO_ADMIN_PASSWORD"
                            ),
                        )
                    }
                )
            }
        )
        narrowed = narrow_fresh_testing_requirements(
            profile, context=self.context, omitted_keys=FRESH_TESTING_OMITTED_SETTINGS
        )

        def keys(record: LaunchplaneProductProfileRecord, instance: str) -> set[str]:
            lane = next(lane for lane in record.lanes if lane.instance == instance)
            return {
                item.binding_key
                for item in record.expected_config.managed_secret_bindings
                if product_config_requirement_applies_to_lane(
                    requirement_context=item.context, requirement_instance=item.instance, lane=lane
                )
            }

        self.assertEqual(keys(narrowed, "prod"), keys(profile, "prod"))
        self.assertIn("ODOO_ADMIN_PASSWORD", keys(narrowed, "testing"))
        self.assertNotIn("ODOO_KEY", keys(narrowed, "testing"))
        self.assertEqual(narrowed.lanes, profile.lanes)

    def test_queue_and_completion_receipts_do_not_change_generation_identity(self) -> None:
        record = generation()
        self.assertEqual(
            generation_binding(record),
            generation_binding(
                record.model_copy(
                    update={"reconcile_requested_at": "queued", "completed_at": "done"}
                )
            ),
        )
        self.assertNotEqual(
            generation_binding(record),
            generation_binding(record.model_copy(update={"target_id": "different"})),
        )


class FreshTestingHostProgramTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.gen = generation()
        self.database = self.gen.namespace.replace("-", "_")
        self.spec: dict[str, Any] = {
            "generation": self.gen.model_dump(),
            "compose_project": self.gen.namespace,
            "image_reference": "registry.invalid/odoo@sha256:" + "f" * 64,
            "database_name": self.database,
            "source": {
                "ODOO_UPSTREAM_HOST": "source.invalid",
                "ODOO_UPSTREAM_USER": "restore-reader",
                "ODOO_UPSTREAM_DB_USER": "postgres",
                "ODOO_UPSTREAM_DB_NAME": "source_db",
                "ODOO_UPSTREAM_FILESTORE_PATH": "/source/filestore/source_db",
            },
            "ssh_key_path": "/existing/read-only-identity",
            "ssh_directory": "/existing/ssh",
            "runner": "fixture runner",
            "workflow_environment": {},
            "readback_program": "fixture readback",
            "readback_spec": "fixture-spec",
        }
        self.containers: dict[str, dict[str, Any]] = {}
        self.volumes: dict[str, dict[str, Any]] = {}
        self.calls: list[list[str]] = []
        self.sanitized = True
        self.existing_database = False
        self.gate_payload = "payload-fixture"
        self.gate_hash = ""
        for suffix in ("data", "logs", "db"):
            name = self.gen.namespace + "-" + suffix
            backing = self.root / name
            backing.mkdir()
            self.volumes[name] = {
                "Driver": "local",
                "Options": {},
                "Mountpoint": str(backing),
                "Labels": {"io.launchplane.testing-resources": self.gen.resources_binding_sha256},
            }
        for service in ("web", "database", "script-runner"):
            suffixes = ("db",) if service == "database" else ("data", "logs")
            self.containers[service] = {
                "Id": service,
                "Config": {
                    "Image": self.spec["image_reference"],
                    "Labels": {
                        "com.docker.compose.project": self.gen.namespace,
                        "com.docker.compose.service": service,
                    },
                    "Env": [
                        "PLATFORM_INSTANCE=testing",
                        "ODOO_DB_HOST=database",
                        "ODOO_DB_NAME=" + self.database,
                    ],
                },
                "HostConfig": {"PortBindings": {}},
                "Mounts": [
                    {"Type": "volume", "Name": self.gen.namespace + "-" + suffix}
                    for suffix in suffixes
                ],
                "NetworkSettings": {"Networks": {"testing-data": {}}},
            }

    def command(self, command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        self.calls.append(command)
        result = b""
        if command[0] in {"ssh", "rsync"}:
            if command[0] == "ssh":
                kwargs["stdout"].write(b"fixture dump")
        elif command[:2] == ["docker", "ps"]:
            result = "\n".join(self.containers).encode()
        elif command[:2] == ["docker", "inspect"]:
            item = (
                {"Internal": True} if command[2] == "testing-data" else self.containers[command[2]]
            )
            result = json.dumps([item]).encode()
        elif command[:3] == ["docker", "volume", "inspect"]:
            result = json.dumps([self.volumes[command[-1]]]).encode()
        elif command[:2] == ["docker", "exec"]:
            if "psql " in " ".join(command):
                result = b"1" if self.existing_database else b"0"
            elif command[-2:] in (["id", "-u"], ["id", "-g"]):
                result = b"1000"
            elif kwargs.get("input") == b"fixture runner":
                result = (
                    b"testing_copy_pre_registry_sanitized=true\ntesting_copy_sanitized=true\n"
                    if self.sanitized
                    else b"private-error-value"
                )
            elif kwargs.get("input") == b"fixture readback":
                result = b"integration_readback_ok=true"
            elif command[-2] == "-c" and command[-1].startswith("import hashlib,os"):
                # Execute the actual generated child program, including escapes.
                with (
                    patch.dict(
                        os.environ,
                        {
                            "ODOO_DB_NAME": self.database,
                            "ODOO_INSTANCE_OVERRIDES_PAYLOAD_B64": self.gate_payload,
                        },
                    ),
                    patch("pathlib.Path.write_text") as write,
                ):
                    exec(command[-1], {})
                    self.gate_hash = write.call_args.args[0]
        return subprocess.CompletedProcess(command, 0, stdout=result, stderr=b"")

    def run_program(self) -> tuple[int, str]:
        encoded = base64.b64encode(json.dumps(self.spec).encode()).decode()
        output = io.StringIO()
        status = 0
        with (
            patch.object(sys, "argv", ["copy", encoded]),
            patch("subprocess.run", side_effect=self.command),
            redirect_stdout(output),
        ):
            try:
                exec(compile(FRESH_TESTING_RESTORE_HOST_PROGRAM, "<fresh-copy>", "exec"), {})
            except SystemExit as error:
                status = int(error.code or 0)
        return status, output.getvalue()

    def test_read_only_capture_never_copies_identity_and_web_starts_after_sanitization(
        self,
    ) -> None:
        import hashlib

        status, output = self.run_program()
        self.assertEqual(status, 0, output)
        self.assertIn("testing_copy_sanitized=true", output)
        stop = self.calls.index(["docker", "stop", "web"])
        source = next(i for i, call in enumerate(self.calls) if call[0] == "ssh")
        start = self.calls.index(["docker", "start", "web"])
        self.assertLess(stop, source)
        self.assertLess(source, start)
        remote = self.calls[source][-1]
        self.assertIn("pg_dump -Fc -- source_db", remote)
        self.assertEqual(len([call for call in self.calls if call[0] == "ssh"]), 1)
        copies = [call for call in self.calls if call[:2] == ["docker", "cp"]]
        self.assertTrue(copies)
        self.assertFalse(any("read-only-identity" in " ".join(call) for call in copies))
        self.assertEqual(
            self.gate_hash,
            hashlib.sha256((self.database + "\n" + self.gate_payload).encode()).hexdigest(),
        )

    def test_existing_database_and_failed_sanitization_keep_web_stopped(self) -> None:
        for fault in ("existing", "sanitization"):
            with self.subTest(fault=fault):
                self.calls.clear()
                self.existing_database = fault == "existing"
                self.sanitized = fault != "sanitization"
                status, output = self.run_program()
                self.assertNotEqual(status, 0)
                self.assertNotIn(["docker", "start", "web"], self.calls)
                self.assertNotIn("private-error-value", output)
                if fault == "existing":
                    self.assertFalse(any(call[0] == "ssh" for call in self.calls))

    def test_published_port_or_host_credential_mount_refuses_before_source_read(self) -> None:
        self.containers["web"]["HostConfig"]["PortBindings"] = {"8069/tcp": [{"HostPort": "18069"}]}
        status, _ = self.run_program()
        self.assertNotEqual(status, 0)
        self.assertFalse(any(call[0] == "ssh" for call in self.calls))
        self.containers["web"]["HostConfig"]["PortBindings"] = {}
        self.containers["script-runner"]["Mounts"].append({"Type": "bind", "Source": "/identity"})
        status, _ = self.run_program()
        self.assertNotEqual(status, 0)
        self.assertFalse(any(call[0] == "ssh" for call in self.calls))

    def test_actual_backing_alias_is_refused_even_when_volume_names_differ(self) -> None:
        data = self.volumes[self.gen.namespace + "-data"]
        self.volumes[self.gen.namespace + "-db"]["Mountpoint"] = data["Mountpoint"]
        status, _ = self.run_program()
        self.assertNotEqual(status, 0)
        self.assertFalse(any(call[0] == "ssh" for call in self.calls))


class TestingCopyBoundaryTests(unittest.TestCase):
    def test_committed_isolation_precedes_first_registry_and_runs_again_after_hooks(self) -> None:
        self.boundary_case("normal")

    def test_failed_isolation_commit_never_reaches_registry_and_drops_only_fresh_copy(self) -> None:
        self.boundary_case("rollback")

    def test_credential_reuse_is_refused_before_registry(self) -> None:
        self.boundary_case("reuse")

    def boundary_case(self, fault: str) -> None:
        import sqlite3
        from types import ModuleType, SimpleNamespace
        from control_plane.odoo_testing_restore import TESTING_COPY_RUNNER
        from tests.test_odoo_connection_isolation import SqliteCursor, seed

        events: list[str] = []
        with TemporaryDirectory() as directory:
            database = Path(directory) / "fresh.db"
            connection = sqlite3.connect(database)
            seed(connection)
            connection.executescript("""
                CREATE TABLE res_users (password TEXT);
                INSERT INTO res_users VALUES ('encoded-production-password');
                CREATE TABLE website (google_analytics_key TEXT);
                INSERT INTO website VALUES ('live-analytics');
                INSERT INTO ir_config_parameter VALUES ('google_analytics.client_secret', 'live-secret');
            """)
            connection.commit()
            self.addCleanup(connection.close)

            class Cursor(SqliteCursor):
                def __enter__(self) -> "Cursor":
                    return self

                def __exit__(self, *args: object) -> None:
                    self.cursor.close()

                def execute(self, query: str, params: Any = ()) -> None:
                    if query == "SET TRANSACTION READ ONLY":
                        return
                    super().execute(query, params)

            class Connection:
                def __init__(self, conn: sqlite3.Connection, *, writer: bool = False):
                    self.conn = conn
                    self.writer = writer

                def cursor(self) -> Cursor:
                    return Cursor(self.conn)

                def commit(self) -> None:
                    if fault != "rollback" or not self.writer:
                        self.conn.commit()

                def rollback(self) -> None:
                    self.conn.rollback()

                def close(self) -> None:
                    self.conn.close()

            def isolated() -> None:
                with sqlite3.connect(database) as observer:
                    self.assertIsNone(
                        observer.execute("SELECT google_analytics_key FROM website").fetchone()[0]
                    )
                    self.assertIsNone(
                        observer.execute(
                            "SELECT usps_api_secret FROM delivery_carrier WHERE delivery_type='usps_rest'"
                        ).fetchone()[0]
                    )
                    self.assertEqual(
                        observer.execute(
                            "SELECT count(*) FROM ir_config_parameter WHERE key='google_analytics.client_secret'"
                        ).fetchone()[0],
                        0,
                    )

            class NativeRunner:
                def __init__(self, settings: object, *_: object):
                    self.local = settings

                def acquire_data_workflow_lock(self) -> None:
                    events.append("lock")

                def release_data_workflow_lock(self) -> None:
                    events.append("unlock")

                def connect_to_db(self) -> Connection:
                    return self.local.db_conn

                def _connect_with_retry(self, _: str) -> Connection:
                    return Connection(sqlite3.connect(database))

                def overwrite_database(self, _: Path) -> None:
                    events.append("restore-fresh")

                def prepare_credentials_before_registry(self, **_: object) -> None:
                    connection.execute("UPDATE res_users SET password=NULL")

                def _resolve_filestore_owner(self) -> str:
                    return "fixture-owner"

                def _prepare_restored_database(self, *_: object, **__: object) -> None:
                    isolated()
                    events.append("registry")
                    connection.execute(
                        "UPDATE website SET google_analytics_key='hook-reintroduced'"
                    )
                    connection.commit()
                    self.prepare_credentials_before_registry(restored_copy=True, do_sanitize=True)
                    isolated()

                def assert_core_schema_healthy(self) -> None:
                    events.append("schema")

                def ensure_gpt_users(self) -> None:
                    self_env = os.environ
                    if self_env.get("ODOO_KEY"):
                        raise AssertionError("Testing runner must omit GPT credential")

                def _drop_database_after_failed_restore(self) -> None:
                    self.local.db_conn.rollback()
                    events.append("drop-fresh")

            class Crypt:
                def __init__(self, **_: object):
                    pass

                def verify(self, candidate: str, encoded: str) -> bool:
                    return (
                        candidate == "production-password"
                        and encoded == "encoded-production-password"
                    )

            artifact = ModuleType("run_odoo_data_workflows")
            artifact.LocalServerSettings = lambda: SimpleNamespace(
                host="database",
                db_name="fresh",
                platform_instance="testing",
                db_conn=Connection(connection, writer=True),
            )
            artifact.OdooDataWorkflowRunner = NativeRunner
            artifact.CryptContext = Crypt
            output = io.StringIO()
            with (
                patch.dict(sys.modules, {artifact.__name__: artifact}),
                patch.dict(
                    os.environ,
                    {
                        "PLATFORM_INSTANCE": "testing",
                        "ODOO_KEY": "",
                        "ODOO_ADMIN_PASSWORD": "production-password"
                        if fault == "reuse"
                        else "testing-password",
                    },
                ),
                patch.object(sys, "argv", ["runner", "fixture.dump", "fresh"]),
                patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0)),
                redirect_stdout(output),
            ):
                if fault == "normal":
                    exec(compile(TESTING_COPY_RUNNER, "<copy-runner>", "exec"), {})
                else:
                    with self.assertRaises(RuntimeError):
                        exec(compile(TESTING_COPY_RUNNER, "<copy-runner>", "exec"), {})
            if fault == "normal":
                self.assertIn("registry", events)
                self.assertIn("testing_copy_sanitized=true", output.getvalue())
                isolated()
            else:
                self.assertNotIn("registry", events)
                self.assertIn("drop-fresh", events)
                self.assertNotIn("testing_copy_sanitized=true", output.getvalue())
            self.assertEqual(events[0], "lock")
            self.assertEqual(events[-1], "unlock")
