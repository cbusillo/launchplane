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
        self.dispatch_workflow = load_workflow(".github/workflows/authz-policy-reconcile.yml")
        self.deploy_workflow = load_workflow(".github/workflows/deploy-launchplane.yml")

    def test_dispatch_wrapper_calls_one_immutable_worker(self) -> None:
        trigger = self.dispatch_workflow.data["on"]
        assert isinstance(trigger, dict)
        self.assertEqual(set(trigger), {"workflow_dispatch"})
        dispatch = trigger["workflow_dispatch"]
        assert isinstance(dispatch, dict)
        dispatch_inputs = dispatch["inputs"]
        assert isinstance(dispatch_inputs, dict)
        managed_set_input = dispatch_inputs["managed_set"]
        assert isinstance(managed_set_input, dict)
        self.assertEqual(managed_set_input["default"], "primary")
        self.assertEqual(
            managed_set_input["options"],
            ["primary", "odoo-route-binding", "odoo-external-route-binding"],
        )
        expected_jobs = {
            "reconcile-primary": (
                "${{ inputs.managed_set == 'primary' }}",
                "${{ secrets.LAUNCHPLANE_AUTHZ_MANAGED_SET_JSON }}",
            ),
            "reconcile-odoo-route-binding": (
                "${{ inputs.managed_set == 'odoo-route-binding' }}",
                "${{ secrets.LAUNCHPLANE_AUTHZ_ODOO_ROUTE_BINDING_MANAGED_SET_JSON }}",
            ),
            "reconcile-odoo-external-route-binding": (
                "${{ inputs.managed_set == 'odoo-external-route-binding' }}",
                "${{ secrets.LAUNCHPLANE_AUTHZ_ODOO_EXTERNAL_ROUTE_BINDING_MANAGED_SET_JSON }}",
            ),
        }
        self.assertEqual(set(self.dispatch_workflow.jobs), set(expected_jobs))
        for job_name, (condition, expected_secret) in expected_jobs.items():
            with self.subTest(job=job_name):
                self.assertEqual(
                    self.dispatch_workflow.job_uses(job_name),
                    "cbusillo/launchplane/.github/workflows/"
                    "reusable-authz-policy-reconcile.yml@"
                    "4dbef2945b0a297a6edaa949a42d8c7d4cbc01cd",
                )
                self.assertEqual(
                    self.dispatch_workflow.job_permissions(job_name),
                    {"contents": "read", "id-token": "write"},
                )
                dispatch_job = self.dispatch_workflow.job(job_name)
                self.assertEqual(dispatch_job["if"], condition)
                dispatch_secrets = dispatch_job["secrets"]
                assert isinstance(dispatch_secrets, dict)
                self.assertEqual(dispatch_secrets["managed_set_json"], expected_secret)

    def test_deploy_workflow_bootstraps_managed_authz_without_deploying(self) -> None:
        deploy_job = self.deploy_workflow.job("deploy")
        self.assertIn("inputs.authz_managed_mode == 'none'", str(deploy_job["if"]))
        self.assertNotIn("operator-authz-grants", self.deploy_workflow.jobs)
        dispatch = self.deploy_workflow.data["on"]
        assert isinstance(dispatch, dict)
        workflow_dispatch = dispatch["workflow_dispatch"]
        assert isinstance(workflow_dispatch, dict)
        dispatch_inputs = workflow_dispatch["inputs"]
        assert isinstance(dispatch_inputs, dict)
        self.assertFalse(any(name.startswith("authz_grants_") for name in dispatch_inputs))
        managed_job = self.deploy_workflow.job("operator-authz-managed")
        self.assertEqual(
            self.deploy_workflow.job_uses("operator-authz-managed"),
            "cbusillo/launchplane/.github/workflows/reusable-authz-policy-reconcile.yml@"
            "4dbef2945b0a297a6edaa949a42d8c7d4cbc01cd",
        )
        self.assertEqual(managed_job["needs"], "operator-authz-managed-validate")
        self.assertEqual(
            self.deploy_workflow.job_permissions("operator-authz-managed"),
            {"contents": "read", "id-token": "write"},
        )
        managed_secrets = managed_job["secrets"]
        assert isinstance(managed_secrets, dict)
        self.assertEqual(
            managed_secrets["managed_set_json"],
            "${{ secrets.LAUNCHPLANE_AUTHZ_MANAGED_SET_JSON }}",
        )
        managed_inputs = managed_job["with"]
        assert isinstance(managed_inputs, dict)
        self.assertEqual(managed_inputs["mode"], "${{ inputs.authz_managed_mode }}")
        self.assertEqual(
            managed_inputs["reviewed_plan_sha256"],
            "${{ inputs.authz_managed_reviewed_plan_sha256 }}",
        )
        validation_step = self.deploy_workflow.step_named(
            "operator-authz-managed-validate", "Validate managed authz isolation"
        )
        self.assertIsNotNone(validation_step)
        assert validation_step is not None
        self.assertIn("cannot include a deploy image input", validation_step.run)
        self.assertIn("cannot include break-glass rollback inputs", validation_step.run)

    def test_reusable_workflow_is_protected_and_service_backed(self) -> None:
        workflow_call = self.workflow.data["on"]
        self.assertIsInstance(workflow_call, dict)
        assert isinstance(workflow_call, dict)
        self.assertEqual(set(workflow_call), {"workflow_call"})
        workflow_call_contract = workflow_call["workflow_call"]
        assert isinstance(workflow_call_contract, dict)
        workflow_call_secrets = workflow_call_contract["secrets"]
        assert isinstance(workflow_call_secrets, dict)
        managed_set_secret = workflow_call_secrets["managed_set_json"]
        assert isinstance(managed_set_secret, dict)
        self.assertEqual(managed_set_secret["required"], True)
        job = self.workflow.job("reconcile")
        self.assertEqual(job["runs-on"], "ubuntu-latest")
        self.assertEqual(job["environment"], "launchplane-authz-admin")
        job_environment = job["env"]
        assert isinstance(job_environment, dict)
        self.assertEqual(
            job_environment["LAUNCHPLANE_AUTHZ_MANAGED_SET_JSON"],
            "${{ secrets.managed_set_json }}",
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
        self.assertEqual(request_step.data["id"], "reconcile")
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

        failure_step = self.workflow.step_named("reconcile", "Summarize managed authz failure")
        self.assertIsNotNone(failure_step)
        assert failure_step is not None
        self.assertEqual(
            failure_step.data["if"],
            "${{ failure() && steps.reconcile.outcome == 'failure' }}",
        )
        self.assertIn("error-summary.json", failure_step.run)
        self.assertIn(".[0:500]", failure_step.run)
        self.assertNotIn('cat "$RESPONSE_FILE"', failure_step.run)

        upload_step = self.workflow.step_named("reconcile", "Upload managed authz evidence")
        self.assertIsNotNone(upload_step)
        assert upload_step is not None
        self.assertEqual(
            upload_step.with_values["path"],
            "${{ steps.request.outputs.evidence_directory }}",
        )
        self.assertEqual(upload_step.with_values["retention-days"], 30)
        cleanup_step = self.workflow.step_named(
            "reconcile", "Remove managed authz request material"
        )
        self.assertIsNotNone(cleanup_step)
        assert cleanup_step is not None
        self.assertEqual(
            cleanup_step.data["if"],
            "always() && steps.request.outputs.private_directory != ''",
        )
        self.assertIn('rm -rf -- "$PRIVATE_DIRECTORY"', cleanup_step.run)

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
                line.split("=", 1) for line in output_file.read_text(encoding="utf-8").splitlines()
            )
            request = json.loads(Path(outputs["request_file"]).read_text(encoding="utf-8"))
            self.assertEqual(request["mode"], "dry_run")
            self.assertEqual(request["reason"], "Review the managed Launchplane authority set.")
            self.assertEqual(request["related_issue"], "cbusillo/launchplane#1774")
            self.assertEqual(request["reviewed_plan_sha256"], "")
            self.assertEqual(outputs["idempotency_key"], "")
            self.assertRegex(outputs["configuration_sha256"], r"^[0-9a-f]{64}$")
            evidence_directory = Path(outputs["evidence_directory"])
            self.assertNotEqual(Path(outputs["request_file"]).parent, evidence_directory)
            request_summary = json.loads(
                (evidence_directory / "request-summary.json").read_text(encoding="utf-8")
            )
            self.assertEqual(request_summary["managed_set_id"], "operator.launchplane")
            self.assertNotIn("desired_policy", request_summary)

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
