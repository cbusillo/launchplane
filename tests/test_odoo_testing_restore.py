import base64
from contextlib import closing, redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from typing import Any
import unittest
import yaml
from unittest.mock import Mock, patch
from control_plane.dokploy.compose import (
    ODOO_DATABASE_DATA_DIRECTORY,
    ODOO_SCRIPT_RUNNER_HEALTHCHECK_COMMAND,
    render_odoo_raw_compose_file,
    _odoo_web_command,
)
from control_plane.integration_readback import INTEGRATION_READBACK_PASSED_PATH
from control_plane.copy_readback_diagnostics import ReadbackPredicate

from control_plane.odoo_testing_restore import (
    FRESH_TESTING_LOCAL_CREDENTIAL_KEYS,
    FRESH_TESTING_OMITTED_SETTINGS,
    FRESH_TESTING_RESTORE_HOST_PROGRAM,
    FRESH_TESTING_STALE_LOCK_PROGRAM,
    TESTING_COPY_COMPLETION_PARAMETER,
    TESTING_COPY_VERIFICATION_FRAME,
    TestingCredentialsOverlap,
    TestingCredentialVersionsMissing,
    FreshOdooTestingGeneration,
    generation_binding,
    build_fresh_testing_restore_script,
    fresh_testing_restore_spec,
    render_fresh_testing_restore_spec,
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
            with self.assertRaises(TestingCredentialsOverlap) as error:
                require_distinct_testing_credentials(Mock(), self.profile, self.context)
        self.assertFalse(any(value in str(error.exception) for value in testing.values()))

    def test_missing_existing_versions_cannot_be_treated_as_isolated(self) -> None:
        with patch(
            "control_plane.secrets.resolve_secret_values_for_integration_from_store",
            side_effect=[{}, {}],
        ):
            with self.assertRaises(TestingCredentialVersionsMissing):
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
    def test_real_restore_renderer_fits_schedule_transport_for_ssh_and_native_copy(self) -> None:
        from control_plane.dokploy.api import validate_dokploy_schedule_script
        from control_plane.integration_readback import INTEGRATION_READBACK_PROGRAM
        from tests.test_odoo_stable_target_replacement import _UPSTREAM_RESTORE_ENV
        from control_plane.odoo_testing_restore import (
            FreshRestoreScriptArguments,
            NATIVE_CAPTURE_SCRIPT_RESERVE_BYTES,
        )
        from control_plane.contracts.odoo_testing_source import NATIVE_TESTING_COPY_ARTIFACT_BYTES

        for native in (False, True):
            with self.subTest(native=native):
                record = generation().model_copy(
                    update={
                        "source_mode": "container-export" if native else "ssh",
                        "source_binding_sha256": "e" * 64 if native else "",
                    }
                )
                arguments = FreshRestoreScriptArguments(
                    generation=record,
                    compose_project=record.namespace,
                    image_reference="registry.invalid/odoo@sha256:" + "f" * 64,
                    database_name=record.namespace.replace("-", "_"),
                    source_environment={} if native else _UPSTREAM_RESTORE_ENV,
                    readback_spec="fixture-spec",
                    readback_program=INTEGRATION_READBACK_PROGRAM,
                    workflow_environment={"ODOO_UPDATE_MODULES": "website"},
                    runtime_environment={},
                    clear_stale_lock=False,
                    data_workflow_lock_path="/volumes/data/.fixture_workflow_lock",
                )
                script = build_fresh_testing_restore_script(
                    **arguments,
                    native_capture={
                        "capture_id": "a" * 32,
                        "generation_binding_sha256": generation_binding(record),
                        "source_binding_sha256": record.source_binding_sha256,
                        "database": {
                            "sha256": "b" * 64,
                            "size": NATIVE_TESTING_COPY_ARTIFACT_BYTES,
                        },
                        "filestore": {
                            "sha256": "c" * 64,
                            "size": NATIVE_TESTING_COPY_ARTIFACT_BYTES,
                        },
                    }
                    if native
                    else None,
                )
                materialized = validate_dokploy_schedule_script(
                    {"script": script, "appName": record.namespace}
                )
                if native:
                    reserved = validate_dokploy_schedule_script(
                        {
                            "script": build_fresh_testing_restore_script(**arguments)
                            + " " * NATIVE_CAPTURE_SCRIPT_RESERVE_BYTES,
                            "appName": record.namespace,
                        }
                    )
                    self.assertLessEqual(materialized, reserved)

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
        self.spec: dict[str, Any] = fresh_testing_restore_spec(
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
        self.containers: dict[str, dict[str, Any]] = {}
        self.volumes: dict[str, dict[str, Any]] = {}
        self.calls: list[list[str]] = []
        self.sanitized = True
        self.website_settings_pass = True
        self.website_settings_call: list[str] | None = None
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
                "State": {"Status": "running"},
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
        from control_plane._odoo_website_settings_program import WEBSITE_SETTINGS_PROGRAM

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
            elif kwargs.get("input") == WEBSITE_SETTINGS_PROGRAM.encode():
                self.website_settings_call = command
                if not self.website_settings_pass:
                    return subprocess.CompletedProcess(
                        command, 1, stdout=b"", stderr=b"private website failure"
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
            patch.object(
                sys,
                "argv",
                ["copy", encoded] + (["fixture:"] if self.spec.get("inspection_only") else []),
            ),
            patch("subprocess.run", side_effect=self.command),
            patch("tempfile.gettempdir", return_value=str(self.root)),
            redirect_stdout(output),
        ):
            try:
                if self.spec.get("inspection_only"):
                    exec(compile(FRESH_TESTING_RESTORE_HOST_PROGRAM, "<fresh-copy>", "exec"), {})
                else:
                    script = render_fresh_testing_restore_spec(self.spec)
                    from control_plane.dokploy.api import validate_dokploy_schedule_script

                    validate_dokploy_schedule_script(
                        {"script": script, "appName": self.gen.namespace}
                    )
                    program = script.partition("<<'LP_FRESH_COPY'\n")[2].rsplit(
                        "\nLP_FRESH_COPY", 1
                    )[0]
                    exec(compile(program, "<fresh-copy-stdin>", "exec"), {})
            except SystemExit as error:
                status = int(error.code or 0)
        return status, output.getvalue()

    def test_new_copy_starts_without_performing_inline_disposal(self) -> None:
        previous = "lp_drill_" + "b" * 32
        self.spec["generation"].update(
            discard_drill_database=previous, discard_copy_binding="c" * 64
        )
        code, output = self.run_program()
        self.assertEqual(code, 0, output)
        self.assertIn(["docker", "start", "web"], self.calls)
        self.assertFalse(any(previous in call for call in self.calls))

    def test_failed_new_copy_never_disposes_prior_drill(self) -> None:
        previous_drill = "lp_drill_" + "b" * 32
        self.spec["generation"].update(
            discard_drill_database=previous_drill, discard_copy_binding="c" * 64
        )
        self.sanitized = False
        code, _ = self.run_program()
        self.assertNotEqual(code, 0)
        self.assertFalse(any(previous_drill in call for call in self.calls))

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

    def test_native_receiver_inspection_uses_isolation_guard_without_effects(self) -> None:
        self.spec["inspection_only"] = True
        self.spec["generation"]["source_mode"] = "container-export"
        status, output = self.run_program()
        self.assertEqual(status, 0, output)
        frames = [
            json.loads(line.removeprefix("fixture:"))
            for line in output.splitlines()
            if line.startswith("fixture:")
        ]
        self.assertEqual(len(frames), 1)
        self.assertFalse(frames[0]["resume"])
        self.assertEqual(frames[0]["runner"], self.containers["script-runner"]["Id"])
        self.assertFalse(
            any(
                call[0] in {"ssh", "rsync"}
                or call[:2] in (["docker", "stop"], ["docker", "start"], ["docker", "cp"])
                or "rm" in call
                for call in self.calls
            )
        )

    def test_stopped_or_unreadable_runner_identifies_predicate_without_exec(self) -> None:
        self.spec.update(inspection_only=True, usage_only=True)
        for state, kind in (("exited", "refused"), (None, "unavailable")):
            with self.subTest(state=state):
                self.containers["script-runner"]["State"] = {"Status": state}
                self.calls.clear()
                status, output = self.run_program()
                self.assertNotEqual(status, 0)
                frames = [json.loads(line.removeprefix("fixture:")) for line in output.splitlines()]
                self.assertEqual(frames[0].get("predicate"), ReadbackPredicate.RUNNER_RUNNING)
                self.assertEqual(frames[0].get("kind"), kind)
                self.assertFalse(any(call[:2] == ["docker", "exec"] for call in self.calls))

    def test_cleanup_inspection_distinguishes_observed_missing_database_from_unreadable_history(
        self,
    ) -> None:
        self.spec.update(inspection_only=True, task_account_cleanup=True)
        status, output = self.run_program()
        self.assertNotEqual(status, 0)
        frame = json.loads(output.strip().removeprefix("fixture:"))
        self.assertEqual(frame["predicate"], ReadbackPredicate.CLEANUP_DATABASE)
        self.assertEqual(frame["kind"], "refused")

    def test_public_verification_requires_live_copy_digest_identity_and_open_gate_without_writes(
        self,
    ) -> None:
        import sqlite3

        # Obtain the persisted gate using the real producer, with provider IO
        # faked by this scenario. The fixture follows its binding format.
        payload = "fixture-payload"
        self.gate_payload = payload
        status, output = self.run_program()
        self.assertEqual(status, 0, output)
        self.calls.clear()
        (self.root / (self.gen.namespace + "-copy.lock")).unlink()
        self.spec.update(
            inspection_only=True,
            verification_only=True,
            runtime_identity={"deployment_record_id": "installed"},
        )
        gate = self.root / "verified-gate"
        self.spec["web_gate_path"] = str(gate)
        gate.write_text(self.gate_hash)
        # Temporary lock-file loss must not require another restore.
        self.existing_database = True
        for container in self.containers.values():
            container["State"] = {"Status": "running"}
            container["Image"] = "fixture-image"
            container["Config"]["Env"].extend(
                [
                    "LAUNCHPLANE_RUNTIME_IDENTITY_JSON="
                    + json.dumps(self.spec["runtime_identity"]),
                    "ODOO_INSTANCE_OVERRIDES_PAYLOAD_B64=" + payload,
                ]
            )
        original = self.command
        witness = generation_binding(self.gen)
        actual_image = self.spec["image_reference"]
        sql = sqlite3.connect(":memory:")
        self.addCleanup(sql.close)
        sql.execute("CREATE TABLE ir_config_parameter (key TEXT PRIMARY KEY, value TEXT)")

        def read(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
            data = kwargs.get("input") or b""
            if command[:3] == ["docker", "image", "inspect"]:
                self.calls.append(command)
                return subprocess.CompletedProcess(
                    command,
                    0,
                    stdout=json.dumps([{"RepoDigests": [actual_image]}]).encode(),
                    stderr=b"",
                )
            if data and command[-1] == self.database and '-d "$1"' in " ".join(command):
                self.calls.append(command)
                sql.execute(
                    "INSERT OR REPLACE INTO ir_config_parameter VALUES (?, ?)",
                    (TESTING_COPY_COMPLETION_PARAMETER, witness),
                )
                observed = sql.execute(data.decode()).fetchone()
                return subprocess.CompletedProcess(
                    command, 0, stdout=str(observed[0]).encode() if observed else b"", stderr=b""
                )
            if command[-2:] == [self.database, str(gate)]:
                self.calls.append(command)
                with (
                    patch.dict(os.environ, {"ODOO_INSTANCE_OVERRIDES_PAYLOAD_B64": payload}),
                    patch.object(sys, "argv", ["gate", self.database, str(gate)]),
                ):
                    exec(command[command.index("-c") + 1], {})
                return subprocess.CompletedProcess(command, 0, stdout=b"", stderr=b"")
            return original(command, **kwargs)

        with patch.object(self, "command", side_effect=read):
            status, output = self.run_program()
            self.assertEqual(status, 0, output)
            frames = [
                json.loads(line.removeprefix("fixture:"))
                for line in output.splitlines()
                if line.startswith("fixture:")
            ]
            self.assertEqual(frames, [TESTING_COPY_VERIFICATION_FRAME])
            for fault in ("witness", "image", "gate"):
                with self.subTest(fault=fault):
                    witness = "wrong" if fault == "witness" else generation_binding(self.gen)
                    actual_image = "wrong" if fault == "image" else self.spec["image_reference"]
                    gate.write_text("wrong" if fault == "gate" else self.gate_hash)
                    status, _ = self.run_program()
                    self.assertNotEqual(status, 0)
        self.assertFalse(
            any(
                call[0] in {"ssh", "rsync"}
                or call[:2] in (["docker", "stop"], ["docker", "start"], ["docker", "cp"])
                or "rm" in call
                for call in self.calls
            )
        )

    def test_drill_usage_reads_successful_logins_without_starting_or_restoring(self) -> None:
        from datetime import datetime, timezone

        self.spec.update(
            inspection_only=True,
            usage_only=True,
            usage_since=datetime.now(timezone.utc).isoformat(),
        )
        self.existing_database = True
        command = self.command
        for used in (True, False):
            with self.subTest(used=used):
                self.calls.clear()

                def database_read(arguments: list[str], **kwargs: Any) -> Any:
                    query = kwargs.get("input") or b""
                    if b"ir_config_parameter" in query:
                        result = self.spec["copy_binding_sha256"].encode()
                    elif b"res_users_log" in query:
                        result = b"t" if used else b"f"
                    else:
                        return command(arguments, **kwargs)
                    self.calls.append(arguments)
                    return subprocess.CompletedProcess(arguments, 0, stdout=result, stderr=b"")

                with patch.object(self, "command", side_effect=database_read):
                    status, output = self.run_program()
                self.assertEqual(status, 0, output)
                frames = [
                    json.loads(line.removeprefix("fixture:"))
                    for line in output.splitlines()
                    if line.startswith("fixture:")
                ]
                self.assertEqual(frames, [{"type": "complete", "recent_signins": used}])
                self.assertFalse(
                    any(
                        call[:2] in (["docker", "stop"], ["docker", "start"], ["docker", "cp"])
                        or "rm" in call
                        or call[0] in {"ssh", "rsync"}
                        for call in self.calls
                    )
                )

    def test_drill_without_login_database_preserves_unknown_use_and_resource_proof(self) -> None:
        from datetime import datetime, timezone

        self.spec.update(
            inspection_only=True,
            usage_only=True,
            usage_since=datetime.now(timezone.utc).isoformat(),
        )
        status, output = self.run_program()
        self.assertEqual(status, 0, output)
        frames = [
            json.loads(line.removeprefix("fixture:"))
            for line in output.splitlines()
            if line.startswith("fixture:")
        ]
        from control_plane.odoo_testing_drill_usage import parse_drill_usage_result

        self.assertIsNone(parse_drill_usage_result(frames))

    def test_login_query_failure_is_unknown_but_permission_refusal_is_denied(self) -> None:
        from datetime import datetime, timezone

        self.spec.update(
            inspection_only=True,
            usage_only=True,
            usage_since=datetime.now(timezone.utc).isoformat(),
        )
        self.existing_database = True
        command = self.command
        for denied, message in (
            (False, b"missing login table"),
            (False, b'psql: FATAL: password authentication failed for user "odoo"'),
            (False, b"ERROR: permission denied for table res_users_log"),
            (False, b"container request unauthorized"),
            (True, b"permission denied while trying to connect to the Docker daemon socket"),
            (
                True,
                b"permission denied while trying to connect to the docker API at unix:///socket",
            ),
        ):
            with self.subTest(message=message):

                def database_read(arguments: list[str], **kwargs: Any) -> Any:
                    query = kwargs.get("input") or b""
                    if b"ir_config_parameter" in query:
                        return subprocess.CompletedProcess(
                            arguments,
                            0,
                            stdout=self.spec["copy_binding_sha256"].encode(),
                            stderr=b"",
                        )
                    if b"res_users_log" in query:
                        return subprocess.CompletedProcess(
                            arguments,
                            1,
                            stdout=b"",
                            stderr=message,
                        )
                    return command(arguments, **kwargs)

                with patch.object(self, "command", side_effect=database_read):
                    status, output = self.run_program()
                frames = [
                    json.loads(line.removeprefix("fixture:"))
                    for line in output.splitlines()
                    if line.startswith("fixture:")
                ]
                if denied:
                    self.assertNotEqual(status, 0)
                    self.assertEqual(frames, [{"type": "denied"}])
                else:
                    self.assertEqual(status, 0, output)
                    self.assertEqual(frames, [{"type": "complete", "resources_qualified": True}])

    def test_container_file_permission_error_is_failed_not_provider_denied(self) -> None:
        self.spec.update(inspection_only=True, verification_only=True)
        with patch.object(
            self,
            "command",
            return_value=subprocess.CompletedProcess(
                ["docker", "inspect"],
                1,
                stdout=b"",
                stderr=b"Python: [Errno 13] Permission denied: '/data/filestore'",
            ),
        ):
            status, output = self.run_program()
        self.assertNotEqual(status, 0)
        frames = [
            json.loads(line.removeprefix("fixture:"))
            for line in output.splitlines()
            if line.startswith("fixture:")
        ]
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0]["type"], "failed")
        self.assertEqual(frames[0]["kind"], "unavailable")

    def test_host_lock_permission_error_is_failed_not_provider_denied(self) -> None:
        self.spec.update(inspection_only=True, verification_only=True)
        with patch("os.open", side_effect=PermissionError("Fixture host lock inaccessible")):
            status, output = self.run_program()
        self.assertNotEqual(status, 0)
        frames = [
            json.loads(line.removeprefix("fixture:"))
            for line in output.splitlines()
            if line.startswith("fixture:")
        ]
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0]["type"], "failed")
        self.assertEqual(frames[0]["kind"], "unavailable")

    def test_down_lane_resource_qualification_does_not_need_login_database(self) -> None:
        from control_plane.odoo_testing_drill_usage import parse_drill_usage_result

        self.spec.update(inspection_only=True, usage_only=True, usage_required=False)
        status, output = self.run_program()
        self.assertEqual(status, 0, output)
        frames = [
            json.loads(line.removeprefix("fixture:"))
            for line in output.splitlines()
            if line.startswith("fixture:")
        ]
        self.assertIsNone(parse_drill_usage_result(frames))
        self.assertFalse(
            any(call[:2] in (["docker", "stop"], ["docker", "start"]) for call in self.calls)
        )
        self.containers["web"]["HostConfig"]["PortBindings"] = {"8069/tcp": [{}]}
        status, _ = self.run_program()
        self.assertNotEqual(status, 0)

    def test_task_cleanup_qualifies_physical_copy_without_reading_login_history(self) -> None:
        self.spec.update(inspection_only=True, usage_only=True, task_account_cleanup=True)
        self.existing_database = True
        command = self.command

        def read(arguments: list[str], **kwargs: Any) -> Any:
            query = kwargs.get("input") or b""
            if b"res_users_log" in query:
                raise AssertionError("Task cleanup must not depend on login history")
            if b"ir_config_parameter" in query:
                return subprocess.CompletedProcess(
                    arguments, 0, stdout=self.spec["copy_binding_sha256"].encode(), stderr=b""
                )
            return command(arguments, **kwargs)

        with patch.object(self, "command", side_effect=read):
            status, output = self.run_program()
        self.assertEqual(status, 0, output)
        frames = [
            json.loads(line.removeprefix("fixture:"))
            for line in output.splitlines()
            if line.startswith("fixture:")
        ]
        self.assertEqual(len(frames), 1)
        self.assertTrue(frames[0]["cleanup_qualified"])
        self.assertFalse(
            any(
                call[:2] in (["docker", "stop"], ["docker", "start"], ["docker", "cp"])
                for call in self.calls
            )
        )
        self.containers["other"] = {
            "Id": "other",
            "Config": {},
            "Mounts": [{"Name": self.gen.namespace + "-data"}],
        }
        with patch.object(self, "command", side_effect=read):
            status, _ = self.run_program()
        self.assertNotEqual(status, 0)

    def test_reclaim_preserves_a_new_copy_that_has_started_serving(self) -> None:
        from datetime import datetime, timezone

        new_database = "lp_drill_" + "d" * 32
        self.spec.update(
            inspection_only=True,
            usage_only=True,
            usage_reclaim=True,
            usage_new_database=new_database,
            usage_database_names=[self.database, new_database],
            usage_since=datetime.now(timezone.utc).isoformat(),
        )
        self.existing_database = True
        command = self.command
        for running, opened in ((True, False), (False, True), (False, False)):
            with self.subTest(running=running, opened=opened):
                self.containers["web"]["State"] = {"Running": running}
                for service in ("web", "script-runner"):
                    self.containers[service]["Config"]["Env"] = [
                        item
                        if not item.startswith("ODOO_DB_NAME=")
                        else "ODOO_DB_NAME=" + new_database
                        for item in self.containers[service]["Config"]["Env"]
                    ]

                def read(arguments: list[str], **kwargs: Any) -> Any:
                    query = kwargs.get("input") or b""
                    if b"ir_config_parameter" in query:
                        result = self.spec["copy_binding_sha256"].encode()
                    elif b"res_users_log" in query:
                        result = b"f"
                    elif arguments[-1] == self.spec["web_gate_path"]:
                        result = str(opened).encode()
                    else:
                        return command(arguments, **kwargs)
                    return subprocess.CompletedProcess(arguments, 0, stdout=result, stderr=b"")

                with patch.object(self, "command", side_effect=read):
                    status, output = self.run_program()
                self.assertEqual(status, 0, output)
                frame = json.loads(output.strip().removeprefix("fixture:"))
                self.assertEqual(
                    frame,
                    {"type": "complete", "new_copy_serving": True}
                    if opened
                    else {"type": "complete", "recent_signins": False},
                )
        self.assertFalse(
            any(call[:2] in (["docker", "stop"], ["docker", "start"]) for call in self.calls)
        )

    def test_provider_converted_testing_networks_pass_the_receiver_guard(self) -> None:
        """Execute the guard on the renderer's graph plus Dokploy's domain injection."""
        composition = yaml.safe_load(
            render_odoo_raw_compose_file(
                image_reference=self.spec["image_reference"],
                hold_web_until_integration_readback=True,
                domain_hosts=(self.gen.domain_host,),
                publish_host_ports=False,
                mount_workflow_ssh=False,
                isolated_testing=True,
            )
        )
        for service, container in self.containers.items():
            networks = composition["services"][service]["networks"]
            container["NetworkSettings"]["Networks"] = dict.fromkeys(networks, {})
        # The installed provider preserves explicit networks and appends these
        # two names when a domain routes to web (Dokploy upstream #4499).
        self.containers["web"]["NetworkSettings"]["Networks"].update(
            {"default": {}, "dokploy-network": {}}
        )
        command = self.command

        def provider_command(arguments: list[str], **kwargs: Any) -> Any:
            if arguments[:2] == ["docker", "inspect"] and arguments[2] in composition["networks"]:
                return subprocess.CompletedProcess(
                    arguments,
                    0,
                    stdout=json.dumps(
                        [{"Internal": composition["networks"][arguments[2]].get("internal", False)}]
                    ).encode(),
                )
            return command(arguments, **kwargs)

        self.spec["inspection_only"] = True
        with patch.object(self, "command", side_effect=provider_command):
            status, output = self.run_program()
        self.assertEqual(status, 0, output)
        self.assertFalse(any(call[:2] == ["docker", "start"] for call in self.calls))

    def test_native_receiver_refuses_shared_storage_before_inspection_success(self) -> None:
        self.spec["inspection_only"] = True
        self.containers["other"] = {
            "Id": "other",
            "Config": {},
            "Mounts": [{"Name": self.gen.namespace + "-data"}],
        }
        status, output = self.run_program()
        self.assertNotEqual(status, 0, output)
        frames = [
            json.loads(line.removeprefix("fixture:"))
            for line in output.splitlines()
            if line.startswith("fixture:")
        ]
        self.assertFalse(any(frame.get("type") == "complete" for frame in frames))

    def test_native_receiver_refuses_existing_database_without_generation_witness(self) -> None:
        self.spec["inspection_only"] = True
        self.existing_database = True
        status, output = self.run_program()
        self.assertNotEqual(status, 0, output)
        self.assertNotIn(["docker", "stop", "web"], self.calls)

    def test_native_restore_consumes_receipt_and_never_selects_ssh_fallback(self) -> None:
        self.spec["generation"].update(
            source_mode="container-export", source_binding_sha256="f" * 64
        )
        self.spec["source"] = {}
        self.spec["native_capture"] = {
            "capture_id": "a" * 32,
            "generation_binding_sha256": self.spec["copy_binding_sha256"],
            "source_binding_sha256": "f" * 64,
            "database": {"sha256": "b" * 64, "size": 100},
            "filestore": {"sha256": "c" * 64, "size": 100},
        }
        status, output = self.run_program()
        self.assertEqual(status, 0, output)
        self.assertFalse(any(call[0] in {"ssh", "rsync"} for call in self.calls))
        self.assertIn(["docker", "start", "web"], self.calls)
        self.calls.clear()
        self.spec["native_capture"] = None
        status, output = self.run_program()
        self.assertNotEqual(status, 0, output)
        self.assertFalse(any(call[0] in {"ssh", "rsync"} for call in self.calls))
        self.assertNotIn(["docker", "start", "web"], self.calls)

    def test_website_exclusion_is_required_before_web_start(self) -> None:
        status, output = self.run_program()
        self.assertEqual(status, 0, output)
        self.assertIsNotNone(self.website_settings_call)
        assert self.website_settings_call is not None
        self.assertEqual(self.website_settings_call[-2:], [self.database, self.gen.instance])
        readback = next(
            index
            for index, call in enumerate(self.calls)
            if call[-2:] == [self.database, self.spec["readback_spec"]]
        )
        self.assertLess(
            self.calls.index(self.website_settings_call),
            readback,
        )
        self.assertLess(readback, self.calls.index(["docker", "start", "web"]))
        self.calls.clear()
        self.gate_hash = ""
        self.website_settings_pass = False
        status, output = self.run_program()
        self.assertNotEqual(status, 0)
        self.assertNotIn(["docker", "start", "web"], self.calls)
        self.assertEqual(self.gate_hash, "")
        self.assertNotIn("private website failure", output)

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
        self.spec["generation"]["drill_storage_budget"] = {
            "database_bytes": 10**30,
            "filestore_bytes": 10**30,
            "staging_bytes": 10**30,
            "reserve_bytes": 10**30,
        }
        status, output = self.run_program()
        self.assertEqual(status, 0, output)
        self.assertFalse(any(call[0] in {"ssh", "rsync"} for call in self.calls))
        self.assertFalse(any(call[:2] == ["docker", "cp"] for call in self.calls))
        self.assertIn(["docker", "start", "web"], self.calls)

    def test_native_staged_chunks_are_not_charged_again_before_restore(self) -> None:
        from types import SimpleNamespace

        self.spec["generation"].update(
            source_mode="container-export",
            source_binding_sha256="f" * 64,
            drill_storage_budget={
                "database_bytes": 20,
                "filestore_bytes": 20,
                "staging_bytes": 80,
                "reserve_bytes": 1,
            },
        )
        self.spec["source"] = {}
        self.spec["native_capture"] = {
            "capture_id": "a" * 32,
            "generation_binding_sha256": self.spec["copy_binding_sha256"],
            "source_binding_sha256": "f" * 64,
            "database": {"size": 20},
            "filestore": {"size": 20},
        }
        self.containers["script-runner"]["GraphDriver"] = {"Data": {"UpperDir": str(self.root)}}
        command = self.command

        def staged(arguments: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
            if kwargs.get("input") == self.spec["native_files_program"].encode():
                request = json.loads(base64.b64decode(arguments[-1]))
                if request["mode"] == "inspect":
                    return subprocess.CompletedProcess(
                        arguments,
                        0,
                        stdout=json.dumps({"chunks": [{"size": 40}], "artifacts": {}}).encode(),
                        stderr=b"",
                    )
            return command(arguments, **kwargs)

        with (
            patch.object(self, "command", side_effect=staged),
            patch.object(os, "statvfs", return_value=SimpleNamespace(f_bavail=90, f_frsize=1)),
        ):
            status, output = self.run_program()
        self.assertEqual(status, 0, output)
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
    def test_rehearsal_source_is_sanitized_without_starting_a_registry(self) -> None:
        self.boundary_case("normal", instance="rehearsal", sanitize_only=True)

    def test_rehearsal_upgrade_keeps_the_sql_and_post_hook_boundaries(self) -> None:
        self.boundary_case("normal", instance="rehearsal", phase="resume")

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

    def boundary_case(
        self,
        fault: str,
        *,
        phase: str = "fresh",
        instance: str = "testing",
        sanitize_only: bool = False,
    ) -> None:
        import logging
        import sqlite3
        from types import ModuleType, SimpleNamespace
        from control_plane.odoo_testing_restore import TESTING_COPY_RUNNER
        from control_plane.odoo_testing_restore import rehearsal_copy_runner
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
                platform_instance=instance,
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
                        "PLATFORM_INSTANCE": instance,
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
                    program = (
                        rehearsal_copy_runner(sanitize_source_only=sanitize_only)
                        if instance == "rehearsal"
                        else TESTING_COPY_RUNNER
                    )
                    exec(compile(program, "<copy-runner>", "exec"), {})
                else:
                    with self.assertRaises(RuntimeError):
                        exec(compile(TESTING_COPY_RUNNER, "<copy-runner>", "exec"), {})
            if fault == "normal":
                if sanitize_only:
                    self.assertNotIn("registry", events)
                else:
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

    def test_connection_fences_accept_only_safe_neutralization_literals(self) -> None:
        from control_plane.odoo_testing_restore import validate_fresh_testing_intent
        from control_plane.odoo_connection_isolation import (
            CONNECTION_ISOLATION_PARAMETERS,
            NEUTRALIZED_FLAG,
        )

        spec = base64.b64encode(json.dumps({"families": []}).encode()).decode()

        def fence_payload(value: object) -> str:
            item = {"key": NEUTRALIZED_FLAG, "value": value}
            return base64.b64encode(json.dumps({"config_parameters": [item]}).encode()).decode()

        for stored in (CONNECTION_ISOLATION_PARAMETERS[NEUTRALIZED_FLAG], str(True)):
            validate_fresh_testing_intent(
                modules="website",
                encoded_payload=fence_payload({"source": "literal", "value": stored}),
                readback_spec=spec,
            )
        for unsafe in (
            {"source": "literal", "value": str(False)},
            {"source": "literal", "value": ""},
            {"source": "secret_binding", "value": str(True)},
            {"source": "literal", "value": str(True), "environment_variable": "OVERRIDE"},
        ):
            with self.subTest(value=unsafe), self.assertRaises(ValueError):
                validate_fresh_testing_intent(
                    modules="website", encoded_payload=fence_payload(unsafe), readback_spec=spec
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
