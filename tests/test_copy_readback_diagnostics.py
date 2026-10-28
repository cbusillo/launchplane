import io
import base64
from contextlib import nullcontext, redirect_stdout
from email.message import Message
import json
import os
from pathlib import Path
import runpy
import subprocess
from tempfile import TemporaryDirectory
from typing import Any
import unittest
import urllib.error
from unittest.mock import patch

from control_plane.dokploy.connection_isolation import isolation_script, run_connection_isolation
from control_plane.dokploy import api, post_deploy
from control_plane.dokploy.source import DokployTargetDefinition
from control_plane.copy_readback_diagnostics import (
    COPY_FAILURE_EVIDENCE_MARKER,
    COPY_FAILURE_OUTPUT_BYTES,
    COPY_FAILURE_STAGE_MARKER,
    COPY_FAILURE_STATE_DIRECTORY,
    CopyFailureStage,
    ReadbackPredicate,
    copy_failure_diagnostics_library,
)
from control_plane.odoo_connection_isolation import (
    OdooConnectionIsolationMode,
    ConnectionIsolationPreconditionFailed,
)


class CopyFailureEvidenceTests(unittest.TestCase):
    def test_symlinked_home_uses_its_verified_real_directory(self) -> None:
        namespace: dict[str, Any] = {"spec": {}}
        exec(copy_failure_diagnostics_library(), namespace)
        result = subprocess.CompletedProcess(["fixture"], 1, stdout=b"", stderr=b"cause")
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            home = root / "home"
            home.mkdir()
            alias = root / "alias"
            alias.symlink_to(home, target_is_directory=True)
            with patch.object(Path, "home", return_value=alias):
                identity = namespace["retain_copy_command_failure"](result, None, {})
            self.assertNotEqual(identity, "unavailable")
            self.assertTrue((home / COPY_FAILURE_STATE_DIRECTORY / (identity + ".json")).is_file())

    def test_partial_write_does_not_leave_unreferenced_private_output(self) -> None:
        namespace: dict[str, Any] = {"spec": {}}
        exec(copy_failure_diagnostics_library(), namespace)
        result = subprocess.CompletedProcess(["fixture"], 1, stdout=b"", stderr=b"cause")

        def full_disk(evidence: Any, handle: Any) -> None:
            handle.write("partial fixture output")
            raise OSError("fixture full disk")

        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            with (
                patch.object(Path, "home", return_value=root),
                patch.object(json, "dump", side_effect=full_disk),
            ):
                identity = namespace["retain_copy_command_failure"](result, None, {})
            self.assertEqual(identity, "unavailable")
            self.assertEqual(list((root / COPY_FAILURE_STATE_DIRECTORY).iterdir()), [])

    def test_private_evidence_retains_bounded_tails_and_only_an_input_digest(self) -> None:
        spec = {"fixture_input": "private settings", "runner": "fixture source"}
        namespace: dict[str, Any] = {"spec": spec}
        exec(copy_failure_diagnostics_library(), namespace)
        stdout = (f"{COPY_FAILURE_STAGE_MARKER}={CopyFailureStage.MODULE_UPDATE}\n").encode()
        stderr = b"x" * COPY_FAILURE_OUTPUT_BYTES + b"\nValueError: private fixture cause\n"
        result = subprocess.CompletedProcess(
            ["fixture", "private argument"], 1, stdout=stdout, stderr=stderr
        )
        output = io.StringIO()
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            with patch.object(Path, "home", return_value=root), redirect_stdout(output):
                namespace["emit_copy_command_failure"](result, namespace["spec"])
                namespace["emit_copy_command_failure"](result, namespace["spec"])
            identifiers = [
                line.partition("=")[2]
                for line in output.getvalue().splitlines()
                if line.startswith(COPY_FAILURE_EVIDENCE_MARKER + "=")
            ]
            self.assertNotEqual(identifiers[0], identifiers[1])
            path = root / COPY_FAILURE_STATE_DIRECTORY / (identifiers[0] + ".json")
            text = path.read_text()
            evidence = json.loads(text)
            self.assertEqual(
                base64.b64decode(evidence["stderr"]), stderr[-COPY_FAILURE_OUTPUT_BYTES:]
            )
            self.assertEqual(base64.b64decode(evidence["stdout"]), stdout)
            self.assertTrue(evidence["stderr_truncated"])
            self.assertFalse(evidence["stdout_truncated"])
            self.assertEqual(evidence["stderr_bytes"], len(stderr))
            self.assertNotIn("private settings", text)
            self.assertNotIn("private argument", text)
            self.assertNotIn("private fixture cause", output.getvalue())
            markers = post_deploy.extract_odoo_post_deploy_readback_markers(
                {"logs": output.getvalue() + COPY_FAILURE_EVIDENCE_MARKER + "=../../private\n"}
            )
            self.assertEqual(markers[COPY_FAILURE_EVIDENCE_MARKER], identifiers[0])

    def test_unsafe_or_unwritable_evidence_storage_keeps_closed_failure_diagnostics(self) -> None:
        namespace: dict[str, Any] = {"spec": {}}
        exec(copy_failure_diagnostics_library(), namespace)
        result = subprocess.CompletedProcess(
            ["fixture"],
            1,
            stdout=(f"{COPY_FAILURE_STAGE_MARKER}={CopyFailureStage.MODULE_UPDATE}\n").encode(),
            stderr=b"private fixture cause",
        )
        for fault in ("alias", "permissions", "disk"):
            with self.subTest(fault=fault), TemporaryDirectory() as temporary:
                root = Path(temporary).resolve()
                directory = root / COPY_FAILURE_STATE_DIRECTORY
                directory.parent.mkdir(parents=True)
                outside = root / "outside"
                outside.mkdir()
                if fault == "alias":
                    directory.symlink_to(outside, target_is_directory=True)
                else:
                    directory.mkdir(mode=0o755 if fault == "permissions" else 0o700)
                output = io.StringIO()
                with (
                    patch.object(Path, "home", return_value=root),
                    patch.object(os, "open", side_effect=OSError("private storage failure"))
                    if fault == "disk"
                    else nullcontext(),
                    redirect_stdout(output),
                ):
                    namespace["emit_copy_command_failure"](result, namespace["spec"])
                markers = post_deploy.extract_odoo_post_deploy_readback_markers(
                    {"logs": output.getvalue()}
                )
                self.assertEqual(markers[COPY_FAILURE_EVIDENCE_MARKER], "unavailable")
                self.assertEqual(markers[COPY_FAILURE_STAGE_MARKER], CopyFailureStage.MODULE_UPDATE)
                self.assertEqual(list(outside.iterdir()), [])
                self.assertNotIn("private", output.getvalue())


class CopyReadbackDiagnosticsTests(unittest.TestCase):
    def run_isolation(
        self, outcome: dict[str, str] | Exception, mode: OdooConnectionIsolationMode = "dry-run"
    ) -> dict[str, str]:
        from tests.test_connection_isolation_qualification import resource_fixture

        _, identity = resource_fixture()
        target = DokployTargetDefinition(context="example", instance="testing", target_id="fixture")
        with (
            patch.object(
                api,
                "fetch_dokploy_target_payload",
                return_value={
                    "env": "ODOO_DB_NAME=copy\nLAUNCHPLANE_RUNTIME_IDENTITY_JSON=" + identity,
                },
            ),
            patch.object(
                post_deploy,
                "_resolve_dokploy_schedule_runtime",
                return_value=("server", "server", "project", "server"),
            ),
            patch.object(api, "find_matching_dokploy_schedule", return_value=None),
            patch.object(
                post_deploy,
                "run_manual_data_schedule",
                return_value=outcome if isinstance(outcome, dict) else {},
                side_effect=outcome if isinstance(outcome, Exception) else None,
            ),
        ):
            return run_connection_isolation(
                host="fixture",
                token="fixture",
                target_definition=target,
                mode=mode,
                provider_operation_key="fixture" if mode == "apply" else "",
            )

    def test_provider_done_without_integration_observation_cannot_make_a_review(self) -> None:
        with self.assertRaises(post_deploy.OdooPostDeployReadbackFailure) as raised:
            self.run_isolation(
                {"connection_isolation_resources_ok": "true", "connection_isolation_ok": "false"}
            )
        self.assertEqual(
            raised.exception.evidence["readback_failed_predicate"], ReadbackPredicate.INTEGRATIONS
        )
        self.assertEqual(raised.exception.evidence["readback_failure_kind"], "unavailable")

    def test_schedule_timeout_and_trigger_refusal_keep_predicate_without_changing_apply_uncertainty(
        self,
    ) -> None:
        for cause, predicate, kind in (
            ("timeout", ReadbackPredicate.SCHEDULE_TERMINAL, "unavailable"),
            ("trigger_rejected", ReadbackPredicate.SCHEDULE_TRIGGER, "refused"),
            ("trigger_baseline_unavailable", ReadbackPredicate.SCHEDULE_BASELINE, "unavailable"),
        ):
            error = api.DokployScheduleExecutionFailed(
                schedule_id="schedule",
                deployment_id="",
                deployment_status="",
                cause=cause,
                provider_detail="private-provider-value",
            )
            with (
                self.subTest(cause=cause),
                self.assertRaises(post_deploy.OdooPostDeployReadbackFailure) as raised,
            ):
                self.run_isolation(error)
            self.assertEqual(raised.exception.evidence["readback_failed_predicate"], predicate)
            self.assertEqual(raised.exception.evidence["readback_failure_kind"], kind)
            self.assertNotIn("private-provider-value", json.dumps(raised.exception.evidence))
        with patch(
            "control_plane.dokploy.connection_isolation.connection_isolation_plan_sha256",
            return_value="",
        ):
            with self.assertRaises(api.DokployScheduleExecutionFailed):
                self.run_isolation(error, mode="apply")

    def test_legacy_inventory_reason_cannot_invent_an_observed_negative(self) -> None:
        from control_plane.copy_readback_diagnostics import readback_failure_evidence

        for reason in ("inventory_changed", "future_read_unavailable"):
            evidence = readback_failure_evidence(
                {
                    "connection_isolation_resources_ok": "false",
                    "integration_readback_refused": "testing_resources/" + reason,
                }
            )
            self.assertEqual(evidence["readback_failure_kind"], "unavailable")

    def test_interrupted_dry_run_preserves_sql_findings_without_blaming_them(self) -> None:
        cause = post_deploy.OdooPostDeployReadbackFailure(
            "incomplete job",
            evidence={
                "connection_isolation_resources_ok": "true",
                "connection_isolation_ok": "false",
            },
        )
        with self.assertRaises(post_deploy.OdooPostDeployReadbackFailure) as raised:
            self.run_isolation(cause)
        self.assertEqual(
            raised.exception.evidence["readback_failed_predicate"],
            ReadbackPredicate.SCHEDULE_EVIDENCE,
        )
        self.assertEqual(raised.exception.evidence["readback_failure_kind"], "unavailable")
        self.assertEqual(raised.exception.evidence["connection_isolation_ok"], "false")

    def test_qualifier_refusal_survives_outer_shell_trap(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            docker = root / "docker"
            docker.write_text(
                "#!/usr/bin/env bash\n"
                'case "$*" in\n'
                "  *com.docker.compose.service=script-runner*) echo runner ;;\n"
                "  *com.docker.compose.service=web*) echo web ;;\n"
                "  *ExecIDs*) echo null ;;\n"
                "  *State.Running*) echo true ;;\n"
                '  \'ps -aq\') if [ "$FIXTURE_OUTCOME" = changed ] && [ -f "$FIXTURE_POLL" ]; then echo changed; else touch "$FIXTURE_POLL"; echo runner; fi ;;\n'
                "  'volume ls -q'|'network ls -q') exit 0 ;;\n"
                "  *'python3 -I -c'*) cat >/dev/null; case \"$FIXTURE_OUTCOME\" in\n"
                "    warning) echo harmless-warning >&2; printf '%s 172.18.0.2\\n' \"$FIXTURE_DIGEST\" ;;\n"
                "    markerless) exit 1 ;;\n"
                "    invalid) echo invalid-digest ;;\n"
                f"    *) printf 'readback_failed_predicate={ReadbackPredicate.REVIEW_BINDING}\\nreadback_failure_kind=refused\\n'; exit 1 ;;\n"
                "  esac ;;\n"
                "  exec*) printf 'connection_isolation_ok=true\\nintegration_readback_ok=true\\n' ;;\n"
                "  inspect*) echo '{}' ;;\n"
                "  *) exit 1 ;;\n"
                "esac\n"
            )
            docker.chmod(0o700)
            flock = root / "flock"
            flock.write_text("#!/usr/bin/env bash\nexit 0\n")
            flock.chmod(0o700)
            with patch.object(post_deploy, "ODOO_TESTING_SCHEDULE_LOCK_DIRECTORY", root):
                script = isolation_script(
                    compose_app_name="example",
                    database_name="copy",
                    instance="testing",
                    mode="dry-run",
                    readback_spec="fixture",
                )
            import hashlib

            for outcome in ("refused", "markerless", "invalid", "changed", "warning"):
                with self.subTest(outcome=outcome):
                    poll = root / outcome
                    result = subprocess.run(
                        ["bash", "-c", script],
                        capture_output=True,
                        text=True,
                        env={
                            **os.environ,
                            "PATH": str(root) + os.pathsep + os.environ["PATH"],
                            "FIXTURE_OUTCOME": outcome,
                            "FIXTURE_POLL": str(poll),
                            "FIXTURE_DIGEST": hashlib.sha256(b"fixture").hexdigest(),
                        },
                        check=False,
                    )
                    markers = post_deploy.extract_odoo_post_deploy_readback_markers(
                        {"logs": result.stdout + result.stderr}
                    )
                    if outcome == "warning":
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertEqual(markers["connection_isolation_resources_ok"], "true")
                        self.assertNotIn("readback_failed_predicate", markers)
                        continue
                    self.assertNotEqual(result.returncode, 0)
                    self.assertEqual(
                        markers["readback_failed_predicate"],
                        ReadbackPredicate.REVIEW_BINDING
                        if outcome == "refused"
                        else ReadbackPredicate.RESOURCES,
                    )
                    self.assertEqual(
                        markers["readback_failure_kind"],
                        "refused" if outcome == "refused" else "unavailable",
                    )

    def test_stopped_runner_refuses_before_exec_with_safe_evidence(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            docker = root / "docker"
            docker.write_text(
                "#!/usr/bin/env bash\n"
                'case "$*" in\n'
                "  *com.docker.compose.service=script-runner*) echo runner ;;\n"
                "  *com.docker.compose.service=web*) echo web ;;\n"
                "  *ExecIDs*) echo null ;;\n"
                "  *State.Running*) echo false ;;\n"
                "  *) echo private-provider-detail >&2; exit 1 ;;\n"
                "esac\n"
            )
            docker.chmod(0o700)
            flock = root / "flock"
            flock.write_text("#!/usr/bin/env bash\nexit 0\n")
            flock.chmod(0o700)
            for mode in ("readback", "dry-run"):
                with (
                    self.subTest(mode=mode),
                    patch.object(post_deploy, "ODOO_TESTING_SCHEDULE_LOCK_DIRECTORY", root),
                ):
                    script = isolation_script(
                        compose_app_name="example",
                        database_name="copy",
                        instance="testing",
                        mode=mode,
                        readback_spec="fixture",
                    )
                    result = subprocess.run(
                        ["bash", "-c", script],
                        capture_output=True,
                        text=True,
                        env={**os.environ, "PATH": str(root) + os.pathsep + os.environ["PATH"]},
                        check=False,
                    )
                    self.assertNotEqual(result.returncode, 0)
                    markers = post_deploy.extract_odoo_post_deploy_readback_markers(
                        {"logs": result.stdout + result.stderr}
                    )
                    self.assertEqual(
                        markers.get("readback_failed_predicate"), ReadbackPredicate.RUNNER_RUNNING
                    )
                    self.assertNotIn("private-provider-detail", json.dumps(markers))

    def test_failed_schedule_without_markers_keeps_a_diagnostic(self) -> None:
        target = DokployTargetDefinition(context="example", instance="testing", target_id="fixture")
        with (
            patch.object(api, "upsert_dokploy_schedule", return_value={"scheduleId": "schedule"}),
            patch.object(
                api,
                "run_dokploy_schedule",
                side_effect=api.DokployScheduleExecutionFailed(
                    schedule_id="schedule",
                    deployment_id="job",
                    deployment_status="error",
                    cause="remote_command_exit",
                    provider_detail="private-provider-detail",
                    recent_output=(),
                ),
            ),
            patch.object(post_deploy, "_read_odoo_post_deploy_log_markers", return_value={}),
            self.assertRaises(post_deploy.OdooPostDeployReadbackFailure) as raised,
        ):
            post_deploy.run_manual_data_schedule(
                host="fixture",
                token="fixture",
                target_definition=target,
                schedule_lookup_id="fixture",
                schedule_type="server",
                schedule_name="fixture",
                schedule_app_name="fixture",
                schedule_payload={},
                resolved_schedule_execution_timeout_seconds=30,
            )
        self.assertTrue(raised.exception.evidence)
        self.assertNotIn("private-provider-detail", json.dumps(raised.exception.evidence))

    def test_client_rejects_empty_failure_and_pass_evidence_without_writing_review(self) -> None:
        main = runpy.run_path("scripts/repair-odoo-copy-connections.py", run_name="copy_client")[
            "main"
        ]
        with TemporaryDirectory() as directory:
            root = Path(directory)
            helper = root / "launchplane-write-action.py"
            helper.touch()
            evidence = root / "review.json"
            for status in (
                "fail",
                "pass",
                "http-error",
                "http-workflow",
                "http-review",
                "transport-error",
            ):

                def response(**_: object) -> dict[str, object]:
                    if status.startswith("http-"):
                        raise urllib.error.HTTPError(
                            "https://service.invalid", 504, "private-value", Message(), None
                        )
                    if status == "transport-error":
                        raise urllib.error.URLError("private-value")
                    return {
                        "trace_id": "launchplane_req_fixture",
                        "result": {
                            "post_deploy_status": status,
                            "override_evidence": {"password": "private-value"},
                        },
                    }

                with (
                    self.subTest(status=status),
                    patch(
                        "sys.argv",
                        [
                            "repair",
                            "dry-run",
                            "--operator-helper",
                            str(helper),
                            "--product",
                            "example",
                            "--context",
                            "example",
                            "--instance",
                            "testing",
                            "--reason",
                            "inspect original",
                            "--evidence-file",
                            str(evidence),
                        ],
                    ),
                    patch(
                        "runpy.run_path",
                        return_value={
                            "resolve_settings": lambda _: {
                                "service_url": "https://service.invalid",
                                "token": "fixture",
                            },
                            "validate_service_url": lambda _: None,
                            "request_launchplane": response,
                            "summarize_http_error": lambda **_: {
                                "status": "unavailable",
                                "summary": {
                                    "http_status": 409 if status != "http-error" else 504,
                                    "error_code": {
                                        "http-workflow": ConnectionIsolationPreconditionFailed(
                                            "workflow_active"
                                        ).code,
                                        "http-review": ConnectionIsolationPreconditionFailed(
                                            "plan_changed"
                                        ).code,
                                    }.get(status, "provider_unavailable"),
                                },
                            },
                            "LaunchplaneSafetyError": ValueError,
                            "public_trace_id": lambda value: value,
                        },
                    ),
                    patch("sys.stdout", new_callable=io.StringIO) as output,
                ):
                    self.assertEqual(main(), 1)
                    result = json.loads(output.getvalue())
                    self.assertTrue(result.get("evidence"))
                    if status in {"http-workflow", "http-review"}:
                        self.assertEqual(result["evidence"]["readback_failure_kind"], "refused")
                        self.assertEqual(
                            result["evidence"]["readback_failed_predicate"],
                            ReadbackPredicate.EXECUTION_LOCK
                            if status == "http-workflow"
                            else ReadbackPredicate.REVIEW_BINDING,
                        )
                    self.assertNotIn("private-value", output.getvalue())
                    self.assertFalse(evidence.exists())
