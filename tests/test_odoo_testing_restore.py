import base64
from contextlib import closing, redirect_stdout
import io
import json
import os
import shlex
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from typing import Any
import unittest
from unittest.mock import Mock, patch
from control_plane.dokploy.compose import (
    ODOO_DATABASE_DATA_DIRECTORY,
    ODOO_SCRIPT_RUNNER_HEALTHCHECK_COMMAND,
    _odoo_web_command,
)
from control_plane.integration_readback import INTEGRATION_READBACK_PASSED_PATH

from control_plane.odoo_testing_restore import (
    FRESH_TESTING_LOCAL_CREDENTIAL_KEYS,
    FRESH_TESTING_OMITTED_SETTINGS,
    FRESH_TESTING_RESTORE_HOST_PROGRAM,
    FRESH_TESTING_STALE_LOCK_PROGRAM,
    TESTING_COPY_COMPLETION_PARAMETER,
    FreshOdooTestingGeneration,
    generation_binding,
    build_fresh_testing_restore_script,
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
        self.assertFalse(keys(narrowed, "testing").intersection(FRESH_TESTING_OMITTED_SETTINGS))
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
        source = {
            "ODOO_UPSTREAM_HOST": "source.invalid",
            "ODOO_UPSTREAM_USER": "restore-reader",
            "ODOO_UPSTREAM_DB_USER": "postgres",
            "ODOO_UPSTREAM_DB_NAME": "source_db",
            "ODOO_UPSTREAM_FILESTORE_PATH": "/source/filestore/source_db",
            "DATA_WORKFLOW_SSH_KEY": "/existing/read-only-identity",
            "DATA_WORKFLOW_SSH_DIR": "/existing/ssh",
        }
        script = build_fresh_testing_restore_script(
            generation=self.gen,
            compose_project=self.gen.namespace,
            image_reference="registry.invalid/odoo@sha256:" + "f" * 64,
            database_name=self.database,
            source_environment=source,
            readback_spec="fixture-spec",
            readback_program="fixture readback",
            workflow_environment={"ODOO_UPDATE_MODULES": "website"},
            runtime_environment={},
            clear_stale_lock=False,
            data_workflow_lock_path="/volumes/data/.fixture_workflow_lock",
        )
        # Execute the host payload actually emitted for the schedule, with fake
        # provider commands below. A producer/consumer spec mismatch must fail.
        encoded = shlex.split(script.splitlines()[2])[2]
        self.spec: dict[str, Any] = json.loads(base64.b64decode(encoded))
        self.containers: dict[str, dict[str, Any]] = {}
        self.volumes: dict[str, dict[str, Any]] = {}
        self.calls: list[list[str]] = []
        self.sanitized = True
        self.existing_database = False
        self.resume_verified = False
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
                        "ODOO_MAX_CRON_THREADS=0",
                    ],
                },
                "HostConfig": {"PortBindings": {}},
                "Mounts": [
                    {
                        "Type": "volume",
                        "Name": self.gen.namespace + "-" + suffix,
                        "Destination": ODOO_DATABASE_DATA_DIRECTORY
                        if service == "database"
                        else "/volumes/" + suffix,
                    }
                    for suffix in suffixes
                ],
                "NetworkSettings": {"Networks": {"testing-data": {}}},
            }

        self.containers["web"]["NetworkSettings"]["Networks"]["dokploy-network"] = {}

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
            elif kwargs.get("input") == self.spec["runner"].encode():
                result = (
                    b"testing_copy_pre_registry_sanitized=true\ntesting_copy_sanitized=true\nodoo_module_update_completed=true\nodoo_module_update_modules_configured=true\n"
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
        if (
            kwargs.get("input") == self.spec["runner"].encode()
            and self.existing_database
            and not self.resume_verified
        ):
            return subprocess.CompletedProcess(
                command, 1, stdout=b"", stderr=b"private partial copy"
            )
        return subprocess.CompletedProcess(command, 0, stdout=result, stderr=b"")

    def run_program(self) -> tuple[int, str]:
        encoded = base64.b64encode(json.dumps(self.spec).encode()).decode()
        output = io.StringIO()
        status = 0
        with (
            patch.object(sys, "argv", ["copy", encoded]),
            patch("subprocess.run", side_effect=self.command),
            patch("tempfile.gettempdir", return_value=str(self.root)),
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
        status, output = self.run_program()
        self.assertEqual(status, 0, output)
        self.assertIn("testing_copy_sanitized=true", output)
        stop = self.calls.index(["docker", "stop", "web"])
        lock_check = next(
            index
            for index, call in enumerate(self.calls)
            if self.spec["data_workflow_lock_path"] in call
        )
        clear_gate = next(
            index
            for index, call in enumerate(self.calls)
            if call[:2] == ["docker", "exec"] and call[-1] == INTEGRATION_READBACK_PASSED_PATH
        )
        self.assertLess(clear_gate, stop)
        self.assertLess(stop, lock_check)
        source = next(i for i, call in enumerate(self.calls) if call[0] == "ssh")
        start = self.calls.index(["docker", "start", "web"])
        self.assertLess(stop, source)
        self.assertLess(source, start)
        remote = self.calls[source][-1]
        import shlex

        remote_arguments = shlex.split(remote)
        self.assertEqual(remote_arguments[-1], self.spec["source"]["ODOO_UPSTREAM_DB_NAME"])
        self.assertIn("pg_dump", remote_arguments)
        self.assertIn("--", remote_arguments)
        self.assertEqual(len([call for call in self.calls if call[0] == "ssh"]), 1)
        copies = [call for call in self.calls if call[:2] == ["docker", "cp"]]
        self.assertTrue(copies)
        self.assertFalse(any("read-only-identity" in " ".join(call) for call in copies))
        gate = self.root / "web-gate"
        gate.write_text(self.gate_hash)
        web_command = (
            _odoo_web_command(hold_until_integration_readback=True)
            .replace(INTEGRATION_READBACK_PASSED_PATH, str(gate))
            .replace("$$", "$")
        )
        subprocess.run(
            ["bash", "-c", web_command],
            env={
                **os.environ,
                "ODOO_DB_NAME": self.database,
                "ODOO_INSTANCE_OVERRIDES_PAYLOAD_B64": self.gate_payload,
                "ODOO_WEB_COMMAND": "true",
            },
            timeout=2,
            check=True,
        )

    def test_database_mount_at_another_path_refuses_before_source_capture(self) -> None:
        self.containers["database"]["Mounts"][0]["Destination"] = "/other-database"
        status, _output = self.run_program()
        self.assertNotEqual(status, 0)
        self.assertFalse(any(call[0] in {"ssh", "rsync"} for call in self.calls))

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

    def test_verified_existing_copy_resumes_without_another_production_capture(self) -> None:
        self.existing_database = True
        self.resume_verified = True
        status, output = self.run_program()
        self.assertEqual(status, 0, output)
        self.assertFalse(any(call[0] in {"ssh", "rsync"} for call in self.calls))
        self.assertFalse(any(call[:2] == ["docker", "cp"] for call in self.calls))
        self.assertIn(["docker", "start", "web"], self.calls)

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
    def test_partial_initial_restore_drops_only_its_new_copy(self) -> None:
        self.boundary_case("restore-partial")

    def test_committed_isolation_precedes_first_registry_and_runs_again_after_hooks(self) -> None:
        self.boundary_case("normal")

    def test_failed_isolation_commit_never_reaches_registry_and_drops_only_fresh_copy(self) -> None:
        self.boundary_case("rollback")

    def test_failed_full_integration_readback_is_refused_before_registry(self) -> None:
        self.boundary_case("readback")

    def test_completed_source_copy_can_resume_without_overwriting_data(self) -> None:
        self.boundary_case("normal", phase="resume")

    def test_resume_rejects_a_copy_from_another_generation_without_dropping_it(self) -> None:
        self.boundary_case("foreign-witness", phase="resume")

    def boundary_case(self, fault: str, *, phase: str = "fresh") -> None:
        import logging
        import sqlite3
        from types import ModuleType, SimpleNamespace
        from control_plane.odoo_testing_restore import TESTING_COPY_RUNNER
        from tests.test_odoo_connection_isolation import SqliteCursor, seed

        events: list[str] = []
        self.addCleanup(logging.disable, logging.root.manager.disable)
        self.enterContext(patch.object(sys, "path", list(sys.path)))
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
            if phase == "resume":
                connection.execute(
                    "INSERT INTO ir_config_parameter VALUES (?, ?)",
                    (
                        TESTING_COPY_COMPLETION_PARAMETER,
                        "foreign-copy" if fault == "foreign-witness" else "fixture-copy-binding",
                    ),
                )
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
                    if "FROM pg_database" in query:
                        self.cursor.execute("SELECT 0")
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
                with closing(sqlite3.connect(database)) as observer:
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
                def __init__(self, settings: Any, *_: object):
                    self.local = settings

                def acquire_data_workflow_lock(self) -> None:
                    events.append("lock")

                def release_data_workflow_lock(self) -> None:
                    events.append("unlock")

                def connect_to_db(self) -> Connection:
                    return self.local.db_conn  # type: ignore[no-any-return]

                def _connect_with_retry(self, _: str) -> Connection:
                    return Connection(sqlite3.connect(database))

                def overwrite_database(self, _: Path) -> None:
                    events.append("restore-fresh")
                    if fault == "restore-partial":
                        raise RuntimeError("partial initial restore")

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

                def module_update_witnesses(self, modules: list[str]) -> dict[str, str]:
                    return {name: "after" if "registry" in events else "before" for name in modules}

                def _module_states_by_name(self) -> dict[str, str]:
                    return {"website": "installed"}

                def assert_core_schema_healthy(self) -> None:
                    events.append("schema")

                def ensure_gpt_users(self) -> None:
                    self_env = os.environ
                    if self_env.get("ODOO_KEY"):
                        raise AssertionError("Testing runner must omit GPT credential")

                def _drop_database_after_failed_restore(self) -> None:
                    self.local.db_conn.rollback()
                    events.append("drop-fresh")

            artifact: Any = ModuleType("run_odoo_data_workflows")
            artifact.LocalServerSettings = lambda: SimpleNamespace(
                host="database",
                db_name="fresh",
                platform_instance="testing",
                db_conn=Connection(connection, writer=True),
            )
            artifact.OdooDataWorkflowRunner = NativeRunner
            (Path(directory) / "fixture.dump").write_bytes(b"fixture verified dump")
            output = io.StringIO()
            with (
                patch.dict(sys.modules, {artifact.__name__: artifact}),
                patch.dict(
                    os.environ,
                    {
                        "PLATFORM_INSTANCE": "testing",
                        "ODOO_KEY": "",
                        "ODOO_ADMIN_PASSWORD": "testing-password",
                        "ODOO_UPDATE_MODULES": "website",
                    },
                ),
                patch.object(
                    sys,
                    "argv",
                    [
                        "runner",
                        str(Path(directory) / "fixture.dump"),
                        "fresh",
                        "fixture-spec",
                        base64.b64encode(b"fixture readback").decode(),
                        phase,
                        "fixture-copy-binding",
                    ],
                ),
                patch(
                    "subprocess.run",
                    return_value=subprocess.CompletedProcess(
                        [],
                        0,
                        stdout=b"integration_readback_ok=false"
                        if fault == "readback"
                        else b"integration_readback_ok=true",
                    ),
                ),
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
                if phase == "fresh":
                    self.assertIn("drop-fresh", events)
                else:
                    self.assertNotIn("drop-fresh", events)
                self.assertNotIn("testing_copy_sanitized=true", output.getvalue())
            if phase == "resume":
                self.assertNotIn("restore-fresh", events)
            self.assertEqual(events[0], "lock")
            self.assertEqual(events[-1], "unlock")


class FreshTestingIntentTests(unittest.TestCase):
    def test_exact_modules_and_no_live_override_can_reach_capture(self) -> None:
        from control_plane.odoo_testing_restore import validate_fresh_testing_intent
        from control_plane.integration_readback import INTEGRATION_FAMILIES

        policy = {
            "families": [
                {"config_parameters": list(item.config_parameters)} for item in INTEGRATION_FAMILIES
            ]
        }
        spec = base64.b64encode(json.dumps(policy).encode()).decode()
        validate_fresh_testing_intent(modules="website,web", encoded_payload="", readback_spec=spec)
        for modules in ("", "AUTO", "auto", "web,Auto", "web;untrusted"):
            with self.subTest(modules=modules), self.assertRaises(ValueError):
                validate_fresh_testing_intent(
                    modules=modules, encoded_payload="", readback_spec=spec
                )
        for family in INTEGRATION_FAMILIES:
            for key in family.config_parameters:
                payload = base64.b64encode(
                    json.dumps(
                        {
                            "config_parameters": [
                                {
                                    "key": key,
                                    "value": {"source": "literal", "value": "copied-live-setting"},
                                }
                            ]
                        }
                    ).encode()
                ).decode()
                with self.subTest(key=key), self.assertRaises(ValueError):
                    validate_fresh_testing_intent(
                        modules="website", encoded_payload=payload, readback_spec=spec
                    )
        addon_payload: dict[str, Any] = {
            "addon_settings": [
                {
                    "addon": "shopify",
                    "setting": "action",
                    "value": {"source": "literal", "value": "apply"},
                }
            ]
        }
        with self.assertRaises(ValueError):
            validate_fresh_testing_intent(
                modules="website",
                encoded_payload=base64.b64encode(json.dumps(addon_payload).encode()).decode(),
                readback_spec=spec,
            )
        addon_payload["addon_settings"][0]["value"]["value"] = "clear"
        validate_fresh_testing_intent(
            modules="website",
            encoded_payload=base64.b64encode(json.dumps(addon_payload).encode()).decode(),
            readback_spec=spec,
        )

    def test_stale_lock_requires_terminal_schedule_and_dead_owner_and_stays_in_copy(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            processes = root / "processes"
            processes.mkdir()
            healthcheck = processes / str(os.getpid() + 2)
            healthcheck.mkdir()
            (healthcheck / "cmdline").write_bytes(
                b"/bin/sh\0-c\0" + ODOO_SCRIPT_RUNNER_HEALTHCHECK_COMMAND.encode() + b"\0"
            )
            lock = root / "workflow.lock"
            for alive, terminal, removed in (
                (True, True, False),
                (False, False, False),
                (False, True, True),
            ):
                with self.subTest(alive=alive, terminal=terminal):
                    lock.write_text("pid=1234\nstarted_at=1\n")
                    with (
                        patch.object(
                            sys,
                            "argv",
                            [
                                "lock",
                                str(lock),
                                "true" if terminal else "false",
                                str(root),
                                str(processes),
                                "fixture-database",
                                ODOO_SCRIPT_RUNNER_HEALTHCHECK_COMMAND,
                            ],
                        ),
                        patch("os.kill", side_effect=None if alive else ProcessLookupError),
                    ):
                        if removed:
                            exec(
                                compile(FRESH_TESTING_STALE_LOCK_PROGRAM, "<stale-lock>", "exec"),
                                {},
                            )
                        else:
                            with self.assertRaises(RuntimeError):
                                exec(
                                    compile(
                                        FRESH_TESTING_STALE_LOCK_PROGRAM, "<stale-lock>", "exec"
                                    ),
                                    {},
                                )
                    self.assertEqual(lock.exists(), not removed)
            lock.write_text("pid=1234\nstarted_at=1\n")
            child = processes / str(os.getpid() + 1)
            child.mkdir()
            (child / "cmdline").write_bytes(b"python3\x00/odoo/odoo-bin\x00-d\x00fixture-database")
            with (
                patch.object(
                    sys,
                    "argv",
                    [
                        "lock",
                        str(lock),
                        "true",
                        str(root),
                        str(processes),
                        "fixture-database",
                        ODOO_SCRIPT_RUNNER_HEALTHCHECK_COMMAND,
                    ],
                ),
                patch("os.kill", side_effect=ProcessLookupError),
                self.assertRaises(RuntimeError),
            ):
                exec(compile(FRESH_TESTING_STALE_LOCK_PROGRAM, "<stale-lock>", "exec"), {})
            self.assertTrue(lock.exists())
            lock.unlink()
            outside = root.parent / (root.name + "-outside-lock")
            outside.write_text("pid=1234\n")
            self.addCleanup(outside.unlink, missing_ok=True)
            lock.symlink_to(outside)
            with (
                patch.object(
                    sys,
                    "argv",
                    [
                        "lock",
                        str(lock),
                        "true",
                        str(root),
                        str(processes),
                        "fixture-database",
                        ODOO_SCRIPT_RUNNER_HEALTHCHECK_COMMAND,
                    ],
                ),
                patch("os.kill", side_effect=ProcessLookupError),
                self.assertRaises(RuntimeError),
            ):
                exec(compile(FRESH_TESTING_STALE_LOCK_PROGRAM, "<stale-lock>", "exec"), {})
            self.assertTrue(outside.exists())


class FreshTestingPlannerModeTests(unittest.TestCase):
    def test_planner_requires_copy_first_and_keeps_later_existing_data_deploys_supported(
        self,
    ) -> None:
        from typing import cast
        from control_plane.contracts.odoo_stable_target_replacement import (
            OdooStableTargetReplacementRequest,
        )
        from control_plane.workflows.odoo_stable_target_replacement import (
            build_odoo_stable_target_replacement_plan,
        )
        from control_plane.odoo_testing_restore import fresh_testing_mode_refusal
        from tests.test_odoo_stable_target_replacement import (
            _Store,
            _target_record,
            _target_id_record,
            _inventory,
            _request,
            _DATABASE_ENV_LINES,
        )

        store = _Store(
            target_record=_target_record(),
            target_id_record=_target_id_record(),
            inventory=_inventory(),
        )
        target = store.target_record
        target_id = store.target_id_record
        assert target is not None and target_id is not None
        for completed, mode in (
            (False, "existing"),
            (True, "upstream_restore"),
            (True, "existing"),
        ):
            current = generation().model_copy(
                update={
                    "product": store.profile.product,
                    "context": target.context,
                    "target_id": target_id.target_id,
                    "reconcile_requested_at": "queued",
                    "completed_at": "done" if completed else "",
                }
            )
            payload = {
                "name": target.target_name,
                "sourceType": "raw",
                "composePath": "docker-compose.yml",
                "composeFile": "services: {}",
                "env": "\n".join(
                    (
                        *_DATABASE_ENV_LINES,
                        "ODOO_DATA_VOLUME=cm_testing_odoo_data",
                        "ODOO_LOG_VOLUME=cm_testing_odoo_logs",
                        "ODOO_DB_VOLUME=cm_testing_odoo_db",
                    )
                ),
            }
            with (
                self.subTest(completed=completed, mode=mode),
                patch(
                    "control_plane.workflows.odoo_stable_target_replacement.read_current_testing_generation",
                    return_value=current,
                ),
                patch(
                    "control_plane.workflows.odoo_stable_target_replacement.dokploy_source.read_dokploy_config",
                    return_value=("host", "token"),
                ),
                patch(
                    "control_plane.workflows.odoo_stable_target_replacement.dokploy_api.fetch_dokploy_target_payload",
                    return_value=payload,
                ),
                patch(
                    "control_plane.workflows.odoo_stable_target_replacement.dokploy_api.latest_deployment_for_target",
                    return_value={"deploymentId": "fixture", "status": "success"},
                ),
            ):
                plan = build_odoo_stable_target_replacement_plan(
                    control_plane_root=Path("."),
                    record_store=store,
                    request=OdooStableTargetReplacementRequest(
                        product=store.profile.product,
                        instance="testing",
                        data_source_mode=cast(Any, mode),
                        allow_empty_data=True,
                    ),
                    dokploy_request=cast(Any, _request),
                )
                refusal = fresh_testing_mode_refusal(current, mode)
                if refusal is not None:
                    self.assertIn(refusal[0], plan.blocker_codes)
                    self.assertEqual(plan.plan_status, "blocked")
                else:
                    self.assertEqual(plan.plan_status, "ready")
