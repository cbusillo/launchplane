from __future__ import annotations

from pathlib import Path
import unittest

from tests.support.workflows import load_workflow


_LAUNCHPLANE_REQUEST = (
    "cbusillo/launchplane/.github/actions/launchplane-request@"
    "adcf937c6aef14e02478724040852d1d2a82a850"
)
_WORKER_SHA = "480c9280b1ae3610f05547192783da2230dc7ff5"


class OdooTargetReplacementWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.plan_wrapper = load_workflow(".github/workflows/odoo-target-replacement-plan.yml")
        self.apply_wrapper = load_workflow(".github/workflows/odoo-target-replacement-apply.yml")
        self.plan = load_workflow(".github/workflows/reusable-odoo-target-replacement-plan.yml")
        self.apply = load_workflow(".github/workflows/reusable-odoo-target-replacement-apply.yml")

    def test_dispatch_wrappers_pin_reviewed_reusable_workers(self) -> None:
        plan_contract = {
            "product": {"required": True, "type": "string"},
            "instance": {"required": True, "type": "string"},
            "strategy": {
                "required": True,
                "type": "choice",
                "options": ["recreate-in-place", "replace-and-cutover"],
            },
            "allow_empty_data": {"required": True, "default": False, "type": "boolean"},
            "data_source_mode": {
                "required": True,
                "type": "choice",
                "options": ["existing", "empty", "upstream_restore"],
            },
            "confirmation": {"required": False, "default": "", "type": "string"},
        }
        apply_contract = {
            "product": {"required": True, "type": "string"},
            "instance": {"required": True, "type": "string"},
            "strategy": {
                "required": True,
                "type": "choice",
                "options": ["recreate-in-place"],
            },
            "artifact_id": {"required": False, "default": "", "type": "string"},
            "source_git_ref": {"required": False, "default": "", "type": "string"},
            "allow_empty_data": {"required": True, "default": False, "type": "boolean"},
            "data_source_mode": {
                "required": True,
                "type": "choice",
                "options": ["existing", "empty", "upstream_restore"],
            },
            "confirmation": {"required": False, "default": "", "type": "string"},
            "verify_health": {"required": True, "default": True, "type": "boolean"},
            "verify_canonical": {"required": True, "default": True, "type": "boolean"},
            "verify_logo": {"required": True, "default": True, "type": "boolean"},
            "no_cache": {"required": True, "default": False, "type": "boolean"},
            "timeout_seconds": {"required": False, "default": "", "type": "string"},
            "health_timeout_seconds": {
                "required": False,
                "default": "",
                "type": "string",
            },
        }
        expected = (
            (
                self.plan_wrapper,
                "plan",
                "reusable-odoo-target-replacement-plan.yml",
                plan_contract,
            ),
            (
                self.apply_wrapper,
                "apply",
                "reusable-odoo-target-replacement-apply.yml",
                apply_contract,
            ),
        )
        for workflow, job_id, worker_name, dispatch_contract in expected:
            with self.subTest(workflow=workflow.label):
                trigger = workflow.data["on"]
                self.assertIsInstance(trigger, dict)
                assert isinstance(trigger, dict)
                self.assertEqual(set(trigger), {"workflow_dispatch"})
                dispatch = trigger["workflow_dispatch"]
                self.assertIsInstance(dispatch, dict)
                assert isinstance(dispatch, dict)
                inputs = dispatch["inputs"]
                self.assertIsInstance(inputs, dict)
                assert isinstance(inputs, dict)
                actual_contract = {}
                for name, definition in inputs.items():
                    self.assertIsInstance(definition, dict)
                    assert isinstance(definition, dict)
                    actual_contract[name] = {
                        key: definition[key]
                        for key in ("required", "default", "type", "options")
                        if key in definition
                    }
                self.assertEqual(actual_contract, dispatch_contract)
                self.assertEqual(set(workflow.jobs), {job_id})
                self.assertEqual(
                    workflow.job_uses(job_id),
                    f"cbusillo/launchplane/.github/workflows/{worker_name}@{_WORKER_SHA}",
                )
                self.assertEqual(
                    workflow.job_permissions(job_id),
                    {"contents": "read", "id-token": "write"},
                )
                self.assertEqual(workflow.steps(job_id), ())
                forwarded = workflow.job(job_id)["with"]
                self.assertIsInstance(forwarded, dict)
                assert isinstance(forwarded, dict)
                expected_forwarded = {
                    name: f"${{{{ inputs.{name} }}}}" for name in dispatch_contract
                }
                expected_forwarded.update(
                    {
                        "launchplane_url": "${{ vars.LAUNCHPLANE_PUBLIC_URL }}",
                        "launchplane_audience": "${{ vars.LAUNCHPLANE_SERVICE_AUDIENCE }}",
                    }
                )
                self.assertEqual(
                    forwarded,
                    expected_forwarded,
                )
                text = Path(workflow.path).read_text(encoding="utf-8")
                self.assertNotIn("launchplane-request@", text)
                self.assertNotIn("runs-on:", text)

    def test_workers_are_reusable_oidc_jobs(self) -> None:
        expected_inputs = {
            "plan": {
                "product",
                "instance",
                "strategy",
                "artifact_id",
                "source_git_ref",
                "allow_empty_data",
                "data_source_mode",
                "confirmation",
                "launchplane_url",
                "launchplane_audience",
            },
            "apply": {
                "product",
                "instance",
                "strategy",
                "artifact_id",
                "source_git_ref",
                "allow_empty_data",
                "data_source_mode",
                "confirmation",
                "verify_health",
                "verify_canonical",
                "verify_logo",
                "no_cache",
                "timeout_seconds",
                "health_timeout_seconds",
                "launchplane_url",
                "launchplane_audience",
            },
        }
        for workflow, job_id in ((self.plan, "plan"), (self.apply, "apply")):
            with self.subTest(workflow=workflow.label):
                trigger = workflow.data["on"]
                self.assertIsInstance(trigger, dict)
                assert isinstance(trigger, dict)
                self.assertEqual(set(trigger), {"workflow_call"})
                workflow_call = trigger["workflow_call"]
                self.assertIsInstance(workflow_call, dict)
                assert isinstance(workflow_call, dict)
                inputs = workflow_call["inputs"]
                self.assertIsInstance(inputs, dict)
                assert isinstance(inputs, dict)
                self.assertEqual(set(inputs), expected_inputs[job_id])
                self.assertEqual(workflow.permissions, {"contents": "read"})
                self.assertEqual(set(workflow.jobs), {job_id})
                self.assertEqual(
                    workflow.job_permissions(job_id),
                    {"contents": "read", "id-token": "write"},
                )
                self.assertEqual(workflow.job(job_id)["runs-on"], "ubuntu-latest")

    def test_plan_preflight_precedes_payload_and_plan_request(self) -> None:
        environment = self.plan.step_named("plan", "Read product environment authority")
        readiness = self.plan.step_named("plan", "Read target replacement readiness")
        enforce = self.plan.step_named("plan", "Enforce target replacement readiness")
        payload = self.plan.step_named("plan", "Write target replacement plan payload")
        request = self.plan.step_named("plan", "Build replacement plan")
        for step in (environment, readiness, enforce, payload, request):
            self.assertIsNotNone(step)
        assert environment is not None
        assert readiness is not None
        assert enforce is not None
        assert payload is not None
        assert request is not None

        self.assertEqual(environment.uses, _LAUNCHPLANE_REQUEST)
        self.assertEqual(environment.with_values["method"], "GET")
        self.assertEqual(environment.with_values["expected-status"], "200")
        self.assertEqual(environment.with_values["log-response-body"], "false")
        self.assertEqual(readiness.uses, _LAUNCHPLANE_REQUEST)
        self.assertEqual(readiness.with_values["method"], "GET")
        self.assertEqual(
            readiness.with_values["route-path"],
            "${{ steps.authority.outputs.readiness_route_path }}",
        )
        self.assertEqual(readiness.with_values["response-output-path"], "readiness")
        self.assertLess(environment.index, readiness.index)
        self.assertLess(readiness.index, enforce.index)
        self.assertLess(enforce.index, payload.index)
        self.assertLess(payload.index, request.index)
        self.assertEqual(
            request.with_values["route-path"],
            "/v1/drivers/odoo/target-replacement-plan",
        )
        self.assertNotIn("if", enforce.data)
        self.assertNotIn("continue-on-error", enforce.data)
        self.assertNotIn("if", request.data)
        self.assertNotIn("continue-on-error", request.data)
        verify = self.plan.step_named("plan", "Verify replacement plan response")
        self.assertIsNotNone(verify)
        assert verify is not None
        self.assertEqual(
            verify.data["env"],
            {"PLAN_STATUS_CODE": "${{ steps.launchplane.outputs.status-code }}"},
        )
        self.assertNotIn("${{", verify.run)
        self.assertIn(".result.plan_status", verify.run)
        self.assertIn("blockers: ((.result.blockers // [])[:10])", verify.run)

    def test_apply_preflight_precedes_payload_and_operation_creation(self) -> None:
        environment = self.apply.step_named("apply", "Read product environment authority")
        readiness = self.apply.step_named("apply", "Read target replacement readiness")
        enforce = self.apply.step_named("apply", "Enforce target replacement readiness")
        payload = self.apply.step_named("apply", "Write replacement request payload")
        request = self.apply.step_named("apply", "Create replacement operation")
        for step in (environment, readiness, enforce, payload, request):
            self.assertIsNotNone(step)
        assert environment is not None
        assert readiness is not None
        assert enforce is not None
        assert payload is not None
        assert request is not None

        self.assertEqual(environment.uses, _LAUNCHPLANE_REQUEST)
        self.assertEqual(environment.with_values["method"], "GET")
        self.assertEqual(readiness.uses, _LAUNCHPLANE_REQUEST)
        self.assertEqual(readiness.with_values["method"], "GET")
        self.assertEqual(
            readiness.with_values["route-path"],
            "${{ steps.authority.outputs.readiness_route_path }}",
        )
        self.assertEqual(readiness.with_values["response-output-path"], "readiness")
        self.assertLess(environment.index, readiness.index)
        self.assertLess(readiness.index, enforce.index)
        self.assertLess(enforce.index, payload.index)
        self.assertLess(payload.index, request.index)
        self.assertEqual(
            request.with_values["route-path"],
            "/v1/drivers/odoo/target-replacement-apply",
        )
        self.assertNotIn("if", enforce.data)
        self.assertNotIn("continue-on-error", enforce.data)
        self.assertNotIn("if", request.data)
        self.assertNotIn("continue-on-error", request.data)

    def test_workers_derive_lane_and_artifact_authority_from_reads(self) -> None:
        plan_authority = self.plan.step_named("plan", "Resolve readiness request from records")
        apply_authority = self.apply.step_named("apply", "Resolve readiness request from records")
        self.assertIsNotNone(plan_authority)
        self.assertIsNotNone(apply_authority)
        assert plan_authority is not None
        assert apply_authority is not None

        for step in (plan_authority, apply_authority):
            self.assertIn(".environment.context", step.run)
            self.assertIn(".environment.target.artifact_manifest.artifact_id", step.run)
            self.assertIn("@uri", step.run)
            self.assertIn("/operational-readiness?action=", step.run)
            self.assertIn("expected_current_artifact_id=", step.run)
            self.assertIn('resolved_product" != "$PRODUCT', step.run)
            self.assertIn('resolved_instance" != "$INSTANCE', step.run)
            self.assertNotIn("refs/heads/main", step.run)

        self.assertIn(
            ".environment.target.artifact_manifest.source_commit",
            apply_authority.run,
        )
        self.assertIn(
            'artifact_id="${REQUESTED_ARTIFACT_ID:-$current_artifact_id}"',
            apply_authority.run,
        )
        self.assertIn(
            'source_git_ref="${REQUESTED_SOURCE_GIT_REF:-$current_source_git_ref}"',
            apply_authority.run,
        )
        self.assertIn('if [ -z "$current_artifact_id" ]', apply_authority.run)
        self.assertIn(
            'if [ -z "$artifact_id" ] || [ -z "$source_git_ref" ]',
            apply_authority.run,
        )
        apply_enforce = self.apply.step_named("apply", "Enforce target replacement readiness")
        self.assertIsNotNone(apply_enforce)
        assert apply_enforce is not None
        self.assertNotIn(
            "The lane is missing an exact current artifact identity.",
            apply_enforce.run,
        )

        plan_payload = self.plan.step_named("plan", "Write target replacement plan payload")
        apply_payload = self.apply.step_named("apply", "Write replacement request payload")
        self.assertIsNotNone(plan_payload)
        self.assertIsNotNone(apply_payload)
        assert plan_payload is not None
        assert apply_payload is not None
        self.assertEqual(
            plan_payload.data["env"],
            {
                "ARTIFACT_ID": "${{ steps.authority.outputs.artifact_id }}",
                "CURRENT_ARTIFACT_ID": "${{ steps.authority.outputs.current_artifact_id }}",
                "SOURCE_GIT_REF": "${{ steps.authority.outputs.source_git_ref }}",
            },
        )
        apply_env = apply_payload.data["env"]
        self.assertIsInstance(apply_env, dict)
        assert isinstance(apply_env, dict)
        self.assertEqual(
            apply_env["CURRENT_ARTIFACT_ID"],
            "${{ steps.authority.outputs.current_artifact_id }}",
        )
        self.assertIn("expected_current_artifact_id", plan_payload.run)
        self.assertIn("expected_current_artifact_id", apply_payload.run)

    def test_workers_upload_bounded_readiness_evidence(self) -> None:
        for workflow, job_id in ((self.plan, "plan"), (self.apply, "apply")):
            with self.subTest(workflow=workflow.label):
                upload = workflow.step_named(job_id, "Upload readiness evidence")
                enforce = workflow.step_named(job_id, "Enforce target replacement readiness")
                readiness = workflow.step_named(job_id, "Read target replacement readiness")
                self.assertIsNotNone(upload)
                self.assertIsNotNone(enforce)
                self.assertIsNotNone(readiness)
                assert upload is not None
                assert enforce is not None
                assert readiness is not None
                self.assertEqual(upload.data["if"], "always()")
                self.assertEqual(upload.with_values["retention-days"], 14)
                evidence_paths = upload.with_values["path"]
                self.assertIsInstance(evidence_paths, str)
                assert isinstance(evidence_paths, str)
                self.assertIn("product-environment-evidence.json", evidence_paths)
                self.assertNotIn("product-environment.json\n", evidence_paths)
                self.assertEqual(
                    readiness.with_values["response-output-path"],
                    "readiness",
                )
                self.assertIn("details: (.details[:10])", enforce.run)
                self.assertNotIn("jq .", enforce.run)

                summarize = workflow.step_named(job_id, "Summarize product environment evidence")
                self.assertIsNotNone(summarize)
                assert summarize is not None
                self.assertIn("artifact_id", summarize.run)
                self.assertIn("source_commit", summarize.run)
                self.assertNotIn("managed_secrets", summarize.run)
                self.assertNotIn("runtime_settings", summarize.run)
                self.assertNotIn("secret_id", summarize.run)

    def test_reusable_workers_contain_no_real_product_authority(self) -> None:
        forbidden = (
            "odoo-tenant-cm",
            "odoo-tenant-opw",
            "cm-testing.shinycomputers.com",
            "opw-testing.shinycomputers.com",
        )
        for workflow in (self.plan, self.apply):
            text = Path(workflow.path).read_text(encoding="utf-8")
            with self.subTest(workflow=workflow.label):
                for token in forbidden:
                    self.assertNotIn(token, text)


if __name__ == "__main__":
    unittest.main()
