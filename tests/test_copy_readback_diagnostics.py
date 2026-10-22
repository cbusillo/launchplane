import io
from email.message import Message
import json
import os
from pathlib import Path
import runpy
import subprocess
from tempfile import TemporaryDirectory
import unittest
import urllib.error
from unittest.mock import patch

from control_plane.dokploy.connection_isolation import isolation_script, run_connection_isolation
from control_plane.dokploy import api, post_deploy
from control_plane.dokploy.source import DokployTargetDefinition
from control_plane.copy_readback_diagnostics import ReadbackPredicate
from control_plane.odoo_connection_isolation import OdooConnectionIsolationMode


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

    def test_resource_negative_and_unavailable_findings_stay_distinct(self) -> None:
        for reason, predicate, kind in (
            ("shared_storage", ReadbackPredicate.RESOURCES, "refused"),
            ("review_binding", ReadbackPredicate.REVIEW_BINDING, "refused"),
            ("workflow_active", ReadbackPredicate.EXECUTION_LOCK, "refused"),
            ("runner_exec_active", ReadbackPredicate.RUNNER_IDLE, "refused"),
            ("inventory_unverified", ReadbackPredicate.RESOURCES, "unavailable"),
        ):
            with self.subTest(reason=reason):
                raw = {
                    "connection_isolation_resources_ok": "false",
                    "integration_readback_refused": "testing_resources/" + reason,
                    "readback_failed_predicate": ReadbackPredicate.RESOURCES,
                    "readback_failure_kind": "unavailable",
                }
                with self.assertRaises(post_deploy.OdooPostDeployReadbackFailure) as raised:
                    self.run_isolation(raw)
                evidence = raised.exception.evidence
                self.assertEqual(evidence["readback_failed_predicate"], predicate)
                self.assertEqual(evidence["readback_failure_kind"], kind)

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
            for status in ("fail", "pass", "http-error", "transport-error"):

                def response(**_: object) -> dict[str, object]:
                    if status == "http-error":
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
                                "summary": {"http_status": 504},
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
                    self.assertNotIn("private-value", output.getvalue())
                    self.assertFalse(evidence.exists())
