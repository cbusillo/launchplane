from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
import unittest

from tests.support.workflows import load_workflow


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class ExecutedWorkflowGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.output = self.root / "output"
        self.summary = self.root / "summary"
        self.bash = shutil.which("bash")
        self.jq = shutil.which("jq")
        if self.bash is None or self.jq is None:
            self.fail("Executed workflow tests require bash and jq, as the workers do")
        self.env = {
            "PATH": f"{Path(self.jq).parent}{os.pathsep}{os.defpath}",
            "RUNNER_TEMP": str(self.root),
            "GITHUB_OUTPUT": str(self.output),
            "GITHUB_STEP_SUMMARY": str(self.summary),
        }

    def run_step(
        self,
        workflow: str,
        job: str,
        name: str,
        env: dict[str, str],
        *,
        status_code: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        step = load_workflow(REPOSITORY_ROOT / ".github/workflows" / workflow).step_named(job, name)
        if step is None:
            raise AssertionError(f"Missing executable step: {workflow}: {name}")
        script = step.run
        if status_code is not None:
            # Resolve the action output that GitHub substitutes before invoking bash.
            script = script.replace("${{ steps.launchplane.outputs.status-code }}", status_code)
        assert self.bash is not None
        return subprocess.run(
            [self.bash, "-ceu", script],
            cwd=self.root,
            env={**self.env, **env},
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

    def deploy_identity(self, *, run_id: str, attempt: str) -> str:
        self.output.unlink(missing_ok=True)
        result = self.run_step(
            "reusable-generic-web-stable-deploy.yml",
            "stable-deploy",
            "Resolve Launchplane deploy request",
            {
                "PRODUCT": "example-site",
                "INSTANCE": "testing",
                "ARTIFACT_ID": "artifact-one",
                "DEPLOY_REFERENCE": "release-one",
                "SOURCE_GIT_REF": "refs/heads/main",
                "GITHUB_RUN_ID": run_id,
                "GITHUB_RUN_ATTEMPT": attempt,
            },
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        outputs = dict(line.split("=", 1) for line in self.output.read_text().splitlines())
        return outputs["idempotency_key"]

    def test_rerun_reuses_identity_but_new_run_has_its_own_identity(self) -> None:
        first = self.deploy_identity(run_id="101", attempt="1")
        self.assertEqual(first, self.deploy_identity(run_id="101", attempt="2"))
        self.assertNotEqual(first, self.deploy_identity(run_id="102", attempt="1"))

    def backup_result(
        self, payload: dict[str, object], *, status: str = "pass", status_code: str = "202"
    ) -> subprocess.CompletedProcess[str]:
        (self.root / "odoo-prod-backup-verification.json").write_text(json.dumps(payload))
        return self.run_step(
            "reusable-odoo-prod-backup-verification.yml",
            "verify",
            "Verify bounded backup evidence",
            {"VERIFICATION_STATUS": status},
            status_code=status_code,
        )

    def test_backup_accepts_bounded_evidence_and_writes_summary(self) -> None:
        result = self.backup_result(
            {"result": {"verification_status": "pass"}, "records": {"backup_record_id": "backup-1"}}
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(self.summary.read_text())["backup_record_id"], "backup-1")

    def test_backup_rejects_unbounded_fields_at_top_level_and_nested_in_arrays(self) -> None:
        for field in (
            "backup_root",
            "backup_dir",
            "database_dump_path",
            "filestore_archive_path",
            "manifest_path",
            "error_message",
        ):
            payloads: tuple[dict[str, object], ...] = (
                {field: "private-value"},
                {"result": [{"evidence": {field: None}}]},
            )
            for payload in payloads:
                with self.subTest(field=field, payload=payload):
                    self.summary.unlink(missing_ok=True)
                    result = self.backup_result(payload)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("unbounded evidence", result.stderr)
                    self.assertFalse(self.summary.exists())
                    self.assertNotIn("private-value", result.stdout + result.stderr)

    def test_backup_rejects_http_failure_and_failed_verification(self) -> None:
        for status_code, status in (("500", "pass"), ("202", "fail")):
            with self.subTest(status_code=status_code, status=status):
                result = self.backup_result(
                    {"result": {"verification_status": status}},
                    status=status,
                    status_code=status_code,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.summary.exists())

    def test_route_binding_requires_confirmation_and_nonblank_idempotency_key(self) -> None:
        for confirmation, key, accepted in (
            ("APPLY LAUNCHPLANE ROUTE BINDING RECONCILE", "operation-1", True),
            ("wrong confirmation", "operation-1", False),
            ("APPLY LAUNCHPLANE ROUTE BINDING RECONCILE", "", False),
            ("APPLY LAUNCHPLANE ROUTE BINDING RECONCILE", "   ", False),
            ("APPLY LAUNCHPLANE ROUTE BINDING RECONCILE", " \t\n ", False),
        ):
            with self.subTest(confirmation=confirmation, key=key):
                result = self.run_step(
                    "reusable-route-binding-reconcile.yml",
                    "reconcile",
                    "Validate route-binding apply guards",
                    {"CONFIRMATION": confirmation, "IDEMPOTENCY_KEY": key},
                )
                self.assertEqual(result.returncode == 0, accepted, result.stderr)

    def test_route_binding_binds_current_record_and_refuses_failed_reads(self) -> None:
        current = self.root / "current.json"
        current.write_text(json.dumps({"record": {"record_sha256": "a" * 64}}))
        for status, expected in (
            ("200", {"state": "present", "record_sha256": "a" * 64}),
            ("404", {"state": "absent"}),
            ("500", None),
        ):
            with self.subTest(status=status):
                payload = self.root / "route-binding-reconcile-request.json"
                payload.unlink(missing_ok=True)
                result = self.run_step(
                    "reusable-route-binding-reconcile.yml",
                    "reconcile",
                    "Build route-binding reconcile request",
                    {
                        "CURRENT_STATUS": status,
                        "CURRENT_FILE": str(current),
                        "MODE": "dry-run",
                        "PRODUCT": "example-site",
                        "CONTEXT": "example-site",
                        "INSTANCE": "testing",
                        "SOURCE_LABEL": "fixture",
                        "REASON": "Review route binding",
                        "CONFIRMATION": "",
                    },
                )
                if expected is None:
                    self.assertNotEqual(result.returncode, 0)
                    self.assertFalse(payload.exists())
                else:
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(json.loads(payload.read_text())["expected_current"], expected)

    def health_env(self) -> dict[str, str]:
        return {
            "PRODUCT": "example-site",
            "CONTEXT": "example-site",
            "INSTANCE": "testing",
            "CHECK_NAME": "ingress",
            "CHECK_KIND": "public_http",
            "MONITORING_INTENT": "public",
            "REASON": "Review health check",
            "ENABLED": "true",
            "REQUIRE_RUNTIME_IDENTITY": "true",
            "PRIVATE_ENDPOINT_KEY": "",
            "MODE": "apply",
            "CONFIRMATION": "APPLY PRODUCT HEALTH MONITORING",
            "REVIEWED_PLAN_SHA256": "a" * 64,
            "IDEMPOTENCY_KEY": "operation-1",
        }

    def validate_health(self, changes: dict[str, str]) -> subprocess.CompletedProcess[str]:
        return self.run_step(
            "reusable-product-health-monitoring.yml",
            "apply",
            "Validate product health monitoring request",
            {**self.health_env(), **changes},
        )

    def test_health_accepts_reviewed_public_private_and_dry_run_requests(self) -> None:
        for changes in (
            {},
            {"CHECK_KIND": "private_http", "PRIVATE_ENDPOINT_KEY": "endpoint-1"},
            {"MODE": "dry-run", "REVIEWED_PLAN_SHA256": "", "IDEMPOTENCY_KEY": ""},
            {"ENABLED": "false", "REQUIRE_RUNTIME_IDENTITY": "false"},
        ):
            with self.subTest(changes=changes):
                result = self.validate_health(changes)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_health_rejects_unreviewed_apply_and_invalid_check_combinations(self) -> None:
        cases = (
            ({"CONFIRMATION": "wrong"}, "confirmation"),
            ({"REVIEWED_PLAN_SHA256": ""}, "SHA-256"),
            ({"REVIEWED_PLAN_SHA256": "not-a-digest"}, "SHA-256"),
            ({"IDEMPOTENCY_KEY": " \t "}, "idempotency_key"),
            ({"ENABLED": "false"}, "disabled check"),
            ({"PRIVATE_ENDPOINT_KEY": "endpoint-1"}, "public_http"),
            ({"CHECK_KIND": "private_http"}, "private_endpoint_key"),
            ({"CHECK_KIND": "unknown"}, "check_kind"),
            ({"MONITORING_INTENT": "unknown"}, "monitoring_intent"),
            ({"MODE": "unknown"}, "mode"),
            ({"MODE": "dry-run", "IDEMPOTENCY_KEY": ""}, "reviewed_plan_sha256"),
            ({"MODE": "dry-run", "REVIEWED_PLAN_SHA256": ""}, "idempotency_key"),
        )
        for changes, diagnostic in cases:
            with self.subTest(changes=changes):
                result = self.validate_health(changes)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(diagnostic, result.stderr)

    def test_health_requires_single_line_nonblank_identifiers_and_reason(self) -> None:
        for field in (
            "PRODUCT",
            "CONTEXT",
            "INSTANCE",
            "CHECK_NAME",
            "CHECK_KIND",
            "MONITORING_INTENT",
            "REASON",
        ):
            for value in (" \t ", "first\nsecond"):
                with self.subTest(field=field, value=value):
                    result = self.validate_health({field: value})
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn(field, result.stderr)
