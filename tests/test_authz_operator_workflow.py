from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
from tempfile import TemporaryDirectory
import unittest

from pydantic import ValidationError

from control_plane.authz_grant_service import AuthzManagedPolicyReconcileEnvelope
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
                "privileged-operation-bootstrap",
                "generic-web-onboarding",
                "manager-preview-approval",
                "owner-acceptance",
                "product-owner-policy-admin",
                "product-health-monitoring",
                "product-retirement",
                "detached-application-retirement",
                "preview-feedback-remediation",
                "generic-web-route-binding",
                "generic-web-testing-ingress-route",
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
            "reconcile-privileged-operation-bootstrap": (
                "${{ inputs.managed_set == 'privileged-operation-bootstrap' }}",
                None,
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
            "reconcile-generic-web-testing-ingress-route": (
                "${{ inputs.managed_set == 'generic-web-testing-ingress-route' }}",
                "${{ secrets.LAUNCHPLANE_AUTHZ_GENERIC_WEB_TESTING_INGRESS_ROUTE_MANAGED_SET_JSON }}",
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
        raw_managed_set_options = managed_set_input["options"]
        assert isinstance(raw_managed_set_options, list)
        managed_set_options: list[str] = []
        for option in raw_managed_set_options:
            assert isinstance(option, str)
            managed_set_options.append(option)
        self.assertEqual(
            set(managed_set_options),
            {job_name.removeprefix("reconcile-") for job_name in expected_jobs},
        )
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
                    "reconcile-primary": "operator.primary",
                    "reconcile-authz-policy-reconcile": "operator.authz-policy-reconcile",
                    "reconcile-privileged-operation-bootstrap": (
                        "operator.privileged-operation-bootstrap"
                    ),
                    "reconcile-generic-web-onboarding": "operator.generic-web-onboarding",
                    "reconcile-manager-preview-approval": "operator.manager-preview-approval",
                    "reconcile-owner-acceptance": "operator.owner-acceptance",
                    "reconcile-product-owner-policy-admin": "operator.product-owner-policy-admin",
                    "reconcile-product-health-monitoring": "operator.product-health-monitoring",
                    "reconcile-product-retirement": "operator.product-retirement",
                    "reconcile-detached-application-retirement": "operator.detached-application-retirement",
                    "reconcile-preview-feedback-remediation": "operator.preview-feedback-remediation",
                    "reconcile-generic-web-route-binding": "operator.generic-web-route-binding",
                    "reconcile-generic-web-testing-ingress-route": (
                        "operator.generic-web-testing-ingress-route"
                    ),
                    "reconcile-odoo-route-binding": "operator.odoo-route-binding",
                    "reconcile-odoo-external-route-binding": "operator.odoo-external-route-binding",
                    "reconcile-odoo-testing-ingress-route": "operator.odoo-testing-ingress-route",
                    "reconcile-odoo-testing-route-binding-refresh": (
                        "operator.odoo-testing-route-binding-refresh"
                    ),
                    "reconcile-odoo-testing-target-replacement": (
                        "operator.odoo-testing-target-replacement"
                    ),
                    "reconcile-odoo-opw-preview-feedback": "operator.odoo-opw-preview-feedback",
                    "reconcile-odoo-opw-production-enrollment": (
                        "operator.odoo-opw-production-enrollment"
                    ),
                    "reconcile-odoo-production-enrollment": "operator.odoo-production-enrollment",
                    "reconcile-odoo-production-operation-read": (
                        "operator.odoo-production-operation-read"
                    ),
                    "reconcile-odoo-production-backup-restore": (
                        "operator.odoo-production-backup-restore"
                    ),
                }
                self.assertEqual(
                    dispatch_inputs["expected_managed_set_id"],
                    expected_managed_set_ids[job_name],
                )
                dispatch_secrets = dispatch_job["secrets"]
                assert isinstance(dispatch_secrets, dict)
                managed_set_json = dispatch_secrets["managed_set_json"]
                assert isinstance(managed_set_json, str)
                if expected_secret is None:
                    self.assertIn("github.event.repository.owner.id", managed_set_json)
                    self.assertIn(
                        "toJSON(vars.LAUNCHPLANE_TERMINAL_AGENT_SUBJECT || null)",
                        managed_set_json,
                    )
                    self.assertIn(
                        "toJSON(vars.LAUNCHPLANE_TERMINAL_AGENT_TOKEN_LABEL || null)",
                        managed_set_json,
                    )
                    self.assertIn("authz_policy_operation.propose", managed_set_json)
                    self.assertIn("authz_policy_operation.read", managed_set_json)
                    self.assertIn("authz_policy_operation.cancel", managed_set_json)
                    self.assertIn("authz_policy_operation.approve", managed_set_json)
                    self.assertIn("authz_policy_operation.revoke", managed_set_json)
                    self.assertNotIn("authz_policy_grant.write", managed_set_json)
                    self.assertNotIn("privileged_secret_operation", managed_set_json)
                    self.assertNotIn("privileged_policy_operation_summary", managed_set_json)
                    self.assertNotIn("secrets.LAUNCHPLANE_AUTHZ_", managed_set_json)
                else:
                    self.assertEqual(managed_set_json, expected_secret)

    def test_privileged_operation_bootstrap_is_exact_and_fails_closed(self) -> None:
        job = self.dispatch_workflow.job("reconcile-privileged-operation-bootstrap")
        secrets = job["secrets"]
        assert isinstance(secrets, dict)
        managed_set_json = secrets["managed_set_json"]
        assert isinstance(managed_set_json, str)
        match = re.search(
            r"format\(\s*'(.*)',\s*github\.event\.repository\.owner\.type",
            managed_set_json,
            re.DOTALL,
        )
        self.assertIsNotNone(match)
        assert match is not None
        template = match.group(1)

        def render(owner_id: str, subject: str, token_label: str) -> dict[str, object]:
            payload = template.replace("{{", "\x00").replace("}}", "\x01")
            payload = payload.replace("{0}", owner_id)
            payload = payload.replace("{1}", subject)
            payload = payload.replace("{2}", token_label)
            payload = payload.replace("\x00", "{").replace("\x01", "}")
            parsed = json.loads(payload)
            assert isinstance(parsed, dict)
            return parsed

        configuration = render("123", json.dumps("terminal-agent"), json.dumps("owner"))
        envelope = AuthzManagedPolicyReconcileEnvelope.model_validate(
            {
                **configuration,
                "mode": "dry_run",
                "reason": "Review the explicit bootstrap canary.",
                "related_issue": "cbusillo/launchplane#2204",
            }
        )
        self.assertEqual(
            set(envelope.desired_policy.github_humans[0].actions),
            {
                "authz_policy_operation.read",
                "authz_policy_operation.cancel",
                "authz_policy_operation.approve",
                "authz_policy_operation.revoke",
            },
        )
        self.assertEqual(envelope.desired_policy.github_humans[0].github_ids, (123,))
        self.assertEqual(
            set(envelope.desired_policy.terminal_agents[0].actions),
            {"authz_policy_operation.propose"},
        )
        self.assertEqual(
            envelope.desired_policy.terminal_agents[0].subjects,
            ("terminal-agent",),
        )
        self.assertEqual(envelope.desired_policy.terminal_agents[0].token_labels, ("owner",))

        for values in (
            ("null", json.dumps("terminal-agent"), json.dumps("owner")),
            ("123", "null", json.dumps("owner")),
            ("123", json.dumps("terminal-agent"), "null"),
        ):
            with self.subTest(values=values), self.assertRaises(ValidationError):
                AuthzManagedPolicyReconcileEnvelope.model_validate(
                    {
                        **render(*values),
                        "mode": "dry_run",
                        "reason": "Reject incomplete bootstrap selectors.",
                        "related_issue": "cbusillo/launchplane#2204",
                    }
                )

    def test_deploy_workflow_does_not_administer_authorization(self) -> None:
        deploy_job = self.deploy_workflow.job("deploy")
        self.assertNotIn("authz_managed", str(deploy_job["if"]))
        self.assertNotIn("operator-authz-grants", self.deploy_workflow.jobs)
        dispatch = self.deploy_workflow.data["on"]
        assert isinstance(dispatch, dict)
        workflow_dispatch = dispatch["workflow_dispatch"]
        assert isinstance(workflow_dispatch, dict)
        dispatch_inputs = workflow_dispatch["inputs"]
        assert isinstance(dispatch_inputs, dict)
        self.assertFalse(any(name.startswith("authz_grants_") for name in dispatch_inputs))
        self.assertFalse(any(name.startswith("authz_managed_") for name in dispatch_inputs))
        self.assertNotIn("authz_managed_set", dispatch_inputs)
        self.assertNotIn(
            "operator-authz-policy-reconcile-bootstrap",
            self.deploy_workflow.jobs,
        )
        self.assertNotIn("operator-authz-managed", self.deploy_workflow.jobs)
        self.assertNotIn("operator-authz-managed-validate", self.deploy_workflow.jobs)
        self.assertNotIn(
            "LAUNCHPLANE_AUTHZ_MANAGED_SET_JSON",
            Path(".github/workflows/deploy-launchplane.yml").read_text(encoding="utf-8"),
        )

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
        self.assertNotIn("default", expected_managed_set_id)
        self.assertEqual(expected_managed_set_id["required"], True)
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
        verify_step = self.workflow.step_named(
            "reconcile", "Verify and summarize managed authz result"
        )
        self.assertIsNotNone(verify_step)
        assert verify_step is not None
        self.assertIn("operational_readiness_blocked_rule_count", verify_step.run)
        self.assertIn(
            "refused to verify an authz apply with operational-readiness blockers",
            verify_step.run,
        )
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
