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
            [
                "primary",
                "authz-policy-reconcile",
                "generic-web-onboarding",
                "manager-preview-approval",
                "owner-acceptance",
                "product-owner-policy-admin",
                "product-health-monitoring",
                "product-retirement",
                "detached-application-retirement",
                "preview-feedback-remediation",
                "generic-web-route-binding",
                "odoo-route-binding",
                "odoo-external-route-binding",
                "odoo-testing-ingress-route",
                "odoo-testing-route-binding-refresh",
                "odoo-testing-target-replacement",
                "odoo-opw-preview-feedback",
                "odoo-opw-production-enrollment",
                "odoo-production-enrollment",
                "odoo-production-operation-read",
                "odoo-production-backup-restore",
            ],
        )
        expected_jobs = {
            "reconcile-primary": (
                "${{ inputs.managed_set == 'primary' }}",
                "${{ secrets.LAUNCHPLANE_AUTHZ_MANAGED_SET_JSON }}",
            ),
            "reconcile-authz-policy-reconcile": (
                "${{ inputs.managed_set == 'authz-policy-reconcile' }}",
                "${{ secrets.LAUNCHPLANE_AUTHZ_POLICY_RECONCILE_MANAGED_SET_JSON }}",
            ),
            "reconcile-manager-preview-approval": (
                "${{ inputs.managed_set == 'manager-preview-approval' }}",
                "${{ secrets.LAUNCHPLANE_AUTHZ_MANAGER_PREVIEW_APPROVAL_MANAGED_SET_JSON }}",
            ),
            "reconcile-owner-acceptance": (
                "${{ inputs.managed_set == 'owner-acceptance' }}",
                "${{ secrets.LAUNCHPLANE_AUTHZ_OWNER_ACCEPTANCE_MANAGED_SET_JSON }}",
            ),
            "reconcile-product-owner-policy-admin": (
                "${{ inputs.managed_set == 'product-owner-policy-admin' }}",
                "${{ secrets.LAUNCHPLANE_AUTHZ_PRODUCT_OWNER_POLICY_ADMIN_MANAGED_SET_JSON }}",
            ),
            "reconcile-generic-web-onboarding": (
                "${{ inputs.managed_set == 'generic-web-onboarding' }}",
                "${{ secrets.LAUNCHPLANE_AUTHZ_GENERIC_WEB_ONBOARDING_MANAGED_SET_JSON }}",
            ),
            "reconcile-product-health-monitoring": (
                "${{ inputs.managed_set == 'product-health-monitoring' }}",
                "${{ secrets.LAUNCHPLANE_AUTHZ_PRODUCT_HEALTH_MONITORING_MANAGED_SET_JSON }}",
            ),
            "reconcile-product-retirement": (
                "${{ inputs.managed_set == 'product-retirement' }}",
                "${{ secrets.LAUNCHPLANE_AUTHZ_PRODUCT_RETIREMENT_MANAGED_SET_JSON }}",
            ),
            "reconcile-detached-application-retirement": (
                "${{ inputs.managed_set == 'detached-application-retirement' }}",
                "${{ secrets.LAUNCHPLANE_AUTHZ_DETACHED_APPLICATION_RETIREMENT_MANAGED_SET_JSON }}",
            ),
            "reconcile-preview-feedback-remediation": (
                "${{ inputs.managed_set == 'preview-feedback-remediation' }}",
                "${{ secrets.LAUNCHPLANE_AUTHZ_PREVIEW_FEEDBACK_REMEDIATION_MANAGED_SET_JSON }}",
            ),
            "reconcile-generic-web-route-binding": (
                "${{ inputs.managed_set == 'generic-web-route-binding' }}",
                "${{ secrets.LAUNCHPLANE_AUTHZ_GENERIC_WEB_ROUTE_BINDING_MANAGED_SET_JSON }}",
            ),
            "reconcile-odoo-route-binding": (
                "${{ inputs.managed_set == 'odoo-route-binding' }}",
                "${{ secrets.LAUNCHPLANE_AUTHZ_ODOO_ROUTE_BINDING_MANAGED_SET_JSON }}",
            ),
            "reconcile-odoo-external-route-binding": (
                "${{ inputs.managed_set == 'odoo-external-route-binding' }}",
                "${{ secrets.LAUNCHPLANE_AUTHZ_ODOO_EXTERNAL_ROUTE_BINDING_MANAGED_SET_JSON }}",
            ),
            "reconcile-odoo-testing-ingress-route": (
                "${{ inputs.managed_set == 'odoo-testing-ingress-route' }}",
                "${{ secrets.LAUNCHPLANE_AUTHZ_ODOO_TESTING_INGRESS_ROUTE_MANAGED_SET_JSON }}",
            ),
            "reconcile-odoo-testing-route-binding-refresh": (
                "${{ inputs.managed_set == 'odoo-testing-route-binding-refresh' }}",
                "${{ secrets.LAUNCHPLANE_AUTHZ_ODOO_TESTING_ROUTE_BINDING_REFRESH_MANAGED_SET_JSON }}",
            ),
            "reconcile-odoo-testing-target-replacement": (
                "${{ inputs.managed_set == 'odoo-testing-target-replacement' }}",
                "${{ secrets.LAUNCHPLANE_AUTHZ_ODOO_TESTING_TARGET_REPLACEMENT_MANAGED_SET_JSON }}",
            ),
            "reconcile-odoo-opw-preview-feedback": (
                "${{ inputs.managed_set == 'odoo-opw-preview-feedback' }}",
                "${{ secrets.LAUNCHPLANE_AUTHZ_ODOO_OPW_PREVIEW_FEEDBACK_MANAGED_SET_JSON }}",
            ),
            "reconcile-odoo-opw-production-enrollment": (
                "${{ inputs.managed_set == 'odoo-opw-production-enrollment' }}",
                "${{ secrets.LAUNCHPLANE_AUTHZ_ODOO_OPW_PRODUCTION_ENROLLMENT_MANAGED_SET_JSON }}",
            ),
            "reconcile-odoo-production-enrollment": (
                "${{ inputs.managed_set == 'odoo-production-enrollment' }}",
                "${{ secrets.LAUNCHPLANE_AUTHZ_ODOO_PRODUCTION_ENROLLMENT_MANAGED_SET_JSON }}",
            ),
            "reconcile-odoo-production-operation-read": (
                "${{ inputs.managed_set == 'odoo-production-operation-read' }}",
                "${{ secrets.LAUNCHPLANE_AUTHZ_ODOO_PRODUCTION_OPERATION_READ_MANAGED_SET_JSON }}",
            ),
            "reconcile-odoo-production-backup-restore": (
                "${{ inputs.managed_set == 'odoo-production-backup-restore' }}",
                "${{ secrets.LAUNCHPLANE_AUTHZ_ODOO_PRODUCTION_BACKUP_RESTORE_MANAGED_SET_JSON }}",
            ),
        }
        self.assertEqual(set(self.dispatch_workflow.jobs), set(expected_jobs))
        for job_name, (condition, expected_secret) in expected_jobs.items():
            with self.subTest(job=job_name):
                self.assertEqual(
                    self.dispatch_workflow.job_uses(job_name),
                    "cbusillo/launchplane/.github/workflows/"
                    "reusable-authz-policy-reconcile.yml@"
                    "39aecc250d6dee91204e24673725bd1ea1ca6bda",
                )
                self.assertEqual(
                    self.dispatch_workflow.job_permissions(job_name),
                    {"contents": "read", "id-token": "write"},
                )
                dispatch_job = self.dispatch_workflow.job(job_name)
                self.assertEqual(dispatch_job["if"], condition)
                dispatch_inputs = dispatch_job["with"]
                assert isinstance(dispatch_inputs, dict)
                expected_managed_set_ids = {
                    "reconcile-authz-policy-reconcile": "operator.authz-policy-reconcile",
                    "reconcile-generic-web-onboarding": "operator.generic-web-onboarding",
                    "reconcile-manager-preview-approval": "operator.manager-preview-approval",
                    "reconcile-owner-acceptance": "operator.owner-acceptance",
                    "reconcile-product-owner-policy-admin": "operator.product-owner-policy-admin",
                    "reconcile-product-retirement": "operator.product-retirement",
                    "reconcile-detached-application-retirement": (
                        "operator.detached-application-retirement"
                    ),
                    "reconcile-preview-feedback-remediation": (
                        "operator.preview-feedback-remediation"
                    ),
                    "reconcile-generic-web-route-binding": (
                        "operator.generic-web-route-binding"
                    ),
                }
                if job_name in expected_managed_set_ids:
                    self.assertEqual(
                        dispatch_inputs["expected_managed_set_id"],
                        expected_managed_set_ids[job_name],
                    )
                else:
                    self.assertNotIn("expected_managed_set_id", dispatch_inputs)
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
        self.assertNotIn("authz_managed_set", dispatch_inputs)
        self.assertNotIn(
            "operator-authz-policy-reconcile-bootstrap",
            self.deploy_workflow.jobs,
        )
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
        workflow_call_inputs = workflow_call_contract["inputs"]
        assert isinstance(workflow_call_inputs, dict)
        expected_managed_set_id = workflow_call_inputs["expected_managed_set_id"]
        assert isinstance(expected_managed_set_id, dict)
        self.assertEqual(expected_managed_set_id["default"], "")
        self.assertEqual(expected_managed_set_id["required"], False)
        self.assertEqual(expected_managed_set_id["type"], "string")
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
                    "EXPECTED_MANAGED_SET_ID": "operator.launchplane",
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

    def test_render_step_rejects_unexpected_managed_set_id(self) -> None:
        render_step = self.workflow.step_named(
            "reconcile", "Validate and render managed authz request"
        )
        self.assertIsNotNone(render_step)
        assert render_step is not None
        configuration = {
            "schema_version": 2,
            "product": "launchplane",
            "managed_set_id": "operator.primary",
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
                    "EXPECTED_MANAGED_SET_ID": "operator.manager-preview-approval",
                    "GITHUB_EVENT_NAME": "workflow_dispatch",
                    "GITHUB_OUTPUT": str(Path(temporary_directory) / "github-output"),
                    "GITHUB_REF": "refs/heads/main",
                    "GITHUB_RUN_ATTEMPT": "1",
                    "GITHUB_RUN_ID": "1234",
                    "LAUNCHPLANE_AUTHZ_MANAGED_SET_JSON": json.dumps(configuration),
                    "MODE": "dry_run",
                    "REASON": "Review the manager preview authorization set.",
                    "RELATED_ISSUE": "cbusillo/launchplane#1919",
                    "REVIEWED_PLAN_SHA256": "",
                    "RUNNER_TEMP": temporary_directory,
                },
                text=True,
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "managed_set_id does not match the selected managed set",
            result.stderr,
        )

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
