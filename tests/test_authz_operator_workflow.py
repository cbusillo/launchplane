from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest

from tests.support.workflows import load_workflow


class AuthzOperatorWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workflow = load_workflow(".github/workflows/reusable-authz-policy-reconcile.yml")

    def test_reusable_workflow_is_protected_and_service_backed(self) -> None:
        workflow_call = self.workflow.data["on"]
        self.assertIsInstance(workflow_call, dict)
        assert isinstance(workflow_call, dict)
        self.assertEqual(set(workflow_call), {"workflow_call"})
        job = self.workflow.job("reconcile")
        self.assertEqual(job["runs-on"], "ubuntu-latest")
        self.assertEqual(job["environment"], "launchplane-authz-admin")
        job_environment = job["env"]
        assert isinstance(job_environment, dict)
        self.assertEqual(
            job_environment["LAUNCHPLANE_AUTHZ_MANAGED_SET_JSON"],
            "${{ secrets.LAUNCHPLANE_AUTHZ_MANAGED_SET_JSON }}",
        )
        self.assertEqual(self.workflow.job_permissions("reconcile")["contents"], "read")
        self.assertEqual(self.workflow.job_permissions("reconcile")["id-token"], "write")
        concurrency = self.workflow.data["concurrency"]
        assert isinstance(concurrency, dict)
        self.assertEqual(concurrency["group"], "launchplane-authz-policy")
        self.assertEqual(concurrency["cancel-in-progress"], False)

        request_step = self.workflow.step_named("reconcile", "Reconcile managed authz policy")
        self.assertIsNotNone(request_step)
        assert request_step is not None
        self.assertRegex(
            request_step.uses,
            r"^cbusillo/launchplane/\.github/actions/launchplane-request@[0-9a-f]{40}$",
        )
        self.assertEqual(
            request_step.with_values["route-path"],
            "/v1/authz-policies/managed-rule-sets/reconcile",
        )
        self.assertEqual(
            request_step.with_values["payload-file"],
            "${{ steps.request.outputs.request_file }}",
        )
        self.assertEqual(
            request_step.with_values["idempotency-key"],
            "${{ steps.request.outputs.idempotency_key }}",
        )
        self.assertEqual(request_step.with_values["log-response-body"], "false")

        upload_step = self.workflow.step_named("reconcile", "Upload managed authz evidence")
        self.assertIsNotNone(upload_step)
        assert upload_step is not None
        self.assertEqual(upload_step.with_values["retention-days"], 30)

    def test_render_step_builds_review_bound_requests(self) -> None:
        render_step = self.workflow.step_named(
            "reconcile", "Validate and render managed authz request"
        )
        self.assertIsNotNone(render_step)
        assert render_step is not None
        configuration = {
            "schema_version": 2,
            "product": "launchplane",
            "managed_set_id": "operator.launchplane",
            "schema_migration": "migrate_v1_to_v2",
            "unmanaged_adoption": "adopt_matching",
            "desired_policy": {"schema_version": 2},
        }
        with TemporaryDirectory() as temporary_directory:
            output_file = Path(temporary_directory) / "github-output"
            result = subprocess.run(
                ["bash", "-ceu", render_step.run],
                check=False,
                capture_output=True,
                env={
                    **os.environ,
                    "DEFAULT_BRANCH": "main",
                    "GITHUB_EVENT_NAME": "workflow_dispatch",
                    "GITHUB_OUTPUT": str(output_file),
                    "GITHUB_REF": "refs/heads/main",
                    "GITHUB_RUN_ATTEMPT": "1",
                    "GITHUB_RUN_ID": "1234",
                    "LAUNCHPLANE_AUTHZ_MANAGED_SET_JSON": json.dumps(configuration),
                    "MODE": "dry_run",
                    "REASON": "Review the managed Launchplane authority set.",
                    "RELATED_ISSUE": "cbusillo/launchplane#1774",
                    "REVIEWED_PLAN_SHA256": "",
                    "RUNNER_TEMP": temporary_directory,
                },
                text=True,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            outputs = dict(
                line.split("=", 1)
                for line in output_file.read_text(encoding="utf-8").splitlines()
            )
            request = json.loads(Path(outputs["request_file"]).read_text(encoding="utf-8"))
            self.assertEqual(request["mode"], "dry_run")
            self.assertEqual(request["reason"], "Review the managed Launchplane authority set.")
            self.assertEqual(request["related_issue"], "cbusillo/launchplane#1774")
            self.assertEqual(request["reviewed_plan_sha256"], "")
            self.assertEqual(outputs["idempotency_key"], "")
            self.assertRegex(outputs["configuration_sha256"], r"^[0-9a-f]{64}$")

    def test_render_step_rejects_unreviewed_apply(self) -> None:
        render_step = self.workflow.step_named(
            "reconcile", "Validate and render managed authz request"
        )
        self.assertIsNotNone(render_step)
        assert render_step is not None
        configuration = {
            "schema_version": 2,
            "product": "launchplane",
            "managed_set_id": "operator.launchplane",
            "desired_policy": {"schema_version": 2},
        }
        with TemporaryDirectory() as temporary_directory:
            result = subprocess.run(
                ["bash", "-ceu", render_step.run],
                check=False,
                capture_output=True,
                env={
                    **os.environ,
                    "DEFAULT_BRANCH": "main",
                    "GITHUB_EVENT_NAME": "workflow_dispatch",
                    "GITHUB_OUTPUT": str(Path(temporary_directory) / "github-output"),
                    "GITHUB_REF": "refs/heads/main",
                    "GITHUB_RUN_ATTEMPT": "1",
                    "GITHUB_RUN_ID": "1234",
                    "LAUNCHPLANE_AUTHZ_MANAGED_SET_JSON": json.dumps(configuration),
                    "MODE": "apply",
                    "REASON": "Apply the reviewed managed Launchplane authority set.",
                    "RELATED_ISSUE": "cbusillo/launchplane#1774",
                    "REVIEWED_PLAN_SHA256": "",
                    "RUNNER_TEMP": temporary_directory,
                },
                text=True,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("apply requires the reviewed dry-run plan SHA-256", result.stderr)

    def test_render_step_rejects_operator_fields_in_managed_config(self) -> None:
        render_step = self.workflow.step_named(
            "reconcile", "Validate and render managed authz request"
        )
        self.assertIsNotNone(render_step)
        assert render_step is not None
        configuration = {
            "schema_version": 2,
            "product": "launchplane",
            "managed_set_id": "operator.launchplane",
            "desired_policy": {"schema_version": 2},
            "mode": "apply",
        }
        with TemporaryDirectory() as temporary_directory:
            result = subprocess.run(
                ["bash", "-ceu", render_step.run],
                check=False,
                capture_output=True,
                env={
                    **os.environ,
                    "DEFAULT_BRANCH": "main",
                    "GITHUB_EVENT_NAME": "workflow_dispatch",
                    "GITHUB_OUTPUT": str(Path(temporary_directory) / "github-output"),
                    "GITHUB_REF": "refs/heads/main",
                    "GITHUB_RUN_ATTEMPT": "1",
                    "GITHUB_RUN_ID": "1234",
                    "LAUNCHPLANE_AUTHZ_MANAGED_SET_JSON": json.dumps(configuration),
                    "MODE": "apply",
                    "REASON": "Apply the reviewed managed Launchplane authority set.",
                    "RELATED_ISSUE": "cbusillo/launchplane#1774",
                    "REVIEWED_PLAN_SHA256": "a" * 64,
                    "RUNNER_TEMP": temporary_directory,
                },
                text=True,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("contains unsupported fields: mode", result.stderr)


if __name__ == "__main__":
    unittest.main()
