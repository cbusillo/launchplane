import json
import os
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast

from click import Command
from click.testing import CliRunner

from control_plane.cli import main
from control_plane.generic_web_preview_http import _destroy_result_with_record_outcome

from pydantic import ValidationError

from control_plane.contracts.preview_workflow_contract import (
    PreviewWorkflowEvent,
    decide_preview_workflow_operation,
    preview_workflow_idempotency_key,
)
from control_plane.workflows.generic_web_preview import (
    GenericWebPreviewDestroyRequest,
    GenericWebPreviewRefreshRequest,
)
from tests.support.workflows import load_workflow
from tests.support.workflows import workflow_call_inputs


CLI_MAIN = cast(Command, main)
REPO_ROOT = Path(__file__).resolve().parents[1]


def _run_preview_cleanup_status(
    *,
    cleanup_result: str,
    cleanup_outcome: str,
    cleanup_failure_summary: str = "",
) -> tuple[subprocess.CompletedProcess[str], dict[str, str]]:
    workflow = load_workflow(REPO_ROOT / ".github/workflows/reusable-preview-feedback-status.yml")
    step = workflow.step_named("resolve", "Resolve preview feedback status")
    assert step is not None
    with TemporaryDirectory() as temporary_directory_name:
        output_path = Path(temporary_directory_name) / "github-output"
        result = subprocess.run(
            ["bash", "-c", step.run],
            check=False,
            cwd=REPO_ROOT,
            env={
                **os.environ,
                "CLEANUP_FAILURE_SUMMARY": cleanup_failure_summary,
                "CLEANUP_OUTCOME": cleanup_outcome,
                "CLEANUP_RESULT": cleanup_result,
                "GITHUB_OUTPUT": str(output_path),
                "MODE": "cleanup",
                "PROVISION_FAILURE_SUMMARY": "",
                "PROVISION_RESULT": "",
                "PUBLISH_FAILURE_SUMMARY": "",
                "PUBLISH_RESULT": "",
                "VERIFICATION_FAILURE_SUMMARY": "",
                "VERIFICATION_RESULT": "",
            },
            capture_output=True,
            text=True,
        )
        outputs = {}
        if output_path.exists():
            for line in output_path.read_text(encoding="utf-8").splitlines():
                name, value = line.split("=", 1)
                outputs[name] = value
    return result, outputs


def _event(**overrides: object) -> PreviewWorkflowEvent:
    values: dict[str, object] = {
        "event_name": "pull_request",
        "action": "synchronize",
        "repository": "cbusillo/sellyouroutboard",
        "anchor_repo": "cbusillo/sellyouroutboard",
        "anchor_pr_number": 105,
        "actor": "cbusillo",
        "base_repository": "cbusillo/sellyouroutboard",
        "head_repository": "cbusillo/sellyouroutboard",
        "head_sha": "abc123",
        "label_names": ("preview",),
        "preview_label": "preview",
    }
    values.update(overrides)
    return PreviewWorkflowEvent.model_validate(values)


class PreviewWorkflowContractTests(unittest.TestCase):
    def test_same_repo_labeled_pr_refreshes_preview(self) -> None:
        decision = decide_preview_workflow_operation(
            _event(action="labeled", action_label="preview")
        )

        self.assertEqual(decision.operation, "refresh")
        self.assertEqual(decision.reason, "preview_label_added")
        self.assertEqual(decision.execution_trust, "same_repo")
        self.assertEqual(decision.launchplane_route_path, "/v1/drivers/generic-web/preview-refresh")
        self.assertEqual(decision.feedback_status, "pending")
        self.assertTrue(decision.product_build_required)
        self.assertTrue(decision.launchplane_feedback_required)

    def test_same_repo_preview_label_removal_uses_trusted_cleanup(self) -> None:
        decision = decide_preview_workflow_operation(
            _event(
                event_name="pull_request_target",
                action="unlabeled",
                action_label="preview",
                label_names=("bug",),
            )
        )

        self.assertEqual(decision.operation, "destroy")
        self.assertEqual(decision.reason, "preview_label_removed")
        self.assertEqual(decision.feedback_status, "destroyed")
        self.assertTrue(decision.launchplane_feedback_required)

    def test_same_repo_unlabeled_pr_is_ignored(self) -> None:
        decision = decide_preview_workflow_operation(_event(label_names=("bug",)))

        self.assertEqual(decision.operation, "ignore")
        self.assertEqual(decision.reason, "preview_label_not_enabled")

    def test_pull_request_target_fork_preview_writes_unsupported_notice_only(self) -> None:
        decision = decide_preview_workflow_operation(
            _event(
                event_name="pull_request_target",
                action="labeled",
                action_label="preview",
                head_repository="somebody/sellyouroutboard",
            )
        )

        self.assertEqual(decision.operation, "unsupported_notice")
        self.assertEqual(decision.execution_trust, "fork")
        self.assertEqual(decision.launchplane_route_path, "/v1/previews/pr-feedback")
        self.assertEqual(decision.feedback_status, "unsupported")
        self.assertFalse(decision.checkout_untrusted_head)
        self.assertFalse(decision.product_build_required)

    def test_pull_request_target_same_repo_closed_destroys_preview(self) -> None:
        decision = decide_preview_workflow_operation(
            _event(event_name="pull_request_target", action="closed")
        )

        self.assertEqual(decision.operation, "destroy")
        self.assertEqual(decision.reason, "pull_request_closed")
        self.assertEqual(decision.feedback_status, "destroyed")

    def test_pull_request_target_same_repo_refresh_is_ignored(self) -> None:
        decision = decide_preview_workflow_operation(_event(event_name="pull_request_target"))

        self.assertEqual(decision.operation, "ignore")
        self.assertEqual(decision.reason, "pull_request_target_does_not_change_preview")

    def test_pull_request_cleanup_is_ignored(self) -> None:
        decision = decide_preview_workflow_operation(
            _event(action="unlabeled", action_label="preview", label_names=("bug",))
        )

        self.assertEqual(decision.operation, "ignore")
        self.assertEqual(decision.reason, "pull_request_cleanup_runs_on_target")

    def test_dependabot_pull_request_event_fails_closed(self) -> None:
        with self.assertRaises(ValidationError):
            _event(actor="dependabot[bot]")

    def test_dependabot_pull_request_target_writes_unsupported_notice_only(self) -> None:
        decision = decide_preview_workflow_operation(
            _event(
                event_name="pull_request_target",
                actor="dependabot[bot]",
                action="synchronize",
            )
        )

        self.assertEqual(decision.operation, "unsupported_notice")
        self.assertEqual(decision.execution_trust, "dependabot")
        self.assertEqual(decision.feedback_status, "unsupported")

    def test_workflow_dispatch_destroy_uses_destroy_route(self) -> None:
        decision = decide_preview_workflow_operation(
            _event(event_name="workflow_dispatch", action="", operation="destroy")
        )

        self.assertEqual(decision.operation, "destroy")
        self.assertEqual(decision.reason, "manual_destroy_requested")
        self.assertEqual(decision.launchplane_route_path, "/v1/drivers/generic-web/preview-destroy")

    def test_preview_workflow_idempotency_key_is_run_scoped(self) -> None:
        self.assertEqual(
            preview_workflow_idempotency_key(
                product="sell-your-outboard",
                context="sellyouroutboard-testing",
                operation="refresh",
                anchor_pr_number=105,
                run_id="123456",
                run_attempt="2",
            ),
            "preview-workflow:sell-your-outboard:sellyouroutboard-testing:refresh:pr-105:123456:2",
        )

    def test_ignored_preview_workflow_does_not_have_idempotency_key(self) -> None:
        with self.assertRaises(ValueError):
            preview_workflow_idempotency_key(
                product="sell-your-outboard",
                context="sellyouroutboard-testing",
                operation="ignore",
                anchor_pr_number=105,
                run_id="123456",
                run_attempt="2",
            )

    def test_reusable_preview_feedback_workflow_owns_request_details(self) -> None:
        workflow_path = REPO_ROOT / ".github/workflows/reusable-preview-pr-feedback.yml"
        workflow = workflow_path.read_text(encoding="utf-8")
        workflow_inputs = workflow_call_inputs(load_workflow(workflow_path))

        self.assertIn("route-path: /v1/previews/pr-feedback", workflow)
        self.assertIn("status=${{ steps.request.outputs.status }}", workflow)
        self.assertIn("feedback_status=result.status", workflow)
        self.assertNotIn("result.feedback_status", workflow)
        self.assertIn("for required in PRODUCT ANCHOR_PR_NUMBER ANCHOR_PR_URL STATUS", workflow)
        self.assertIn("context=${{ steps.request.outputs.context }}", workflow)
        self.assertNotIn('CONTEXT="$PRODUCT"', workflow)
        self.assertIn("idempotency_key", workflow)
        self.assertIn("preview-pr-feedback", workflow)

        self.assertNotIn("marker", workflow_inputs)
        self.assertNotIn("idempotency-key", workflow_inputs)
        self.assertNotIn("payload", workflow_inputs)
        self.assertNotIn("route-path", workflow_inputs)

    def test_reusable_preview_feedback_workflow_accepts_all_preview_statuses(self) -> None:
        workflow = (REPO_ROOT / ".github/workflows/reusable-preview-pr-feedback.yml").read_text(
            encoding="utf-8"
        )

        for status in (
            "pending",
            "ready",
            "destroyed",
            "failed",
            "cleanup_failed",
            "unsupported",
            "cleared",
        ):
            self.assertIn(status, workflow)

    def test_reusable_preview_feedback_status_workflow_owns_status_selection(self) -> None:
        workflow_path = REPO_ROOT / ".github/workflows/reusable-preview-feedback-status.yml"
        workflow = workflow_path.read_text(encoding="utf-8")
        workflow_inputs = workflow_call_inputs(load_workflow(workflow_path))

        self.assertIn("mode", workflow_inputs)
        self.assertIn("publish_result", workflow_inputs)
        self.assertIn("provision_result", workflow_inputs)
        self.assertIn("verification_result", workflow_inputs)
        self.assertIn("cleanup_result", workflow_inputs)
        self.assertIn("cleanup_outcome", workflow_inputs)
        self.assertIn("status='ready'", workflow)
        self.assertIn("status='failed'", workflow)
        self.assertIn("status='destroyed'", workflow)
        self.assertIn("status='cleanup_failed'", workflow)
        self.assertIn("status='cleared'", workflow)
        self.assertIn("no_preview_recorded", workflow)
        self.assertIn("cleanup_outcome\" = 'failed'", workflow)
        self.assertIn("Unknown Launchplane cleanup outcome; failing closed.", workflow)
        self.assertIn("mode must be refresh or cleanup", workflow)
        self.assertIn("result must be a GitHub Actions terminal result", workflow)
        self.assertIn("success|failure|cancelled|skipped)", workflow)
        self.assertIn("single_line_output", workflow)
        self.assertIn("value=\"${value//$'\\n'/ }\"", workflow)
        self.assertNotIn("LAUNCHPLANE_FAILURE_SUMMARY", workflow)
        self.assertIn(
            "uses: ./.github/workflows/reusable-preview-pr-feedback.yml",
            workflow,
        )

        self.assertNotIn("marker", workflow_inputs)
        self.assertNotIn("idempotency-key", workflow_inputs)
        self.assertNotIn("payload", workflow_inputs)
        self.assertNotIn("route-path", workflow_inputs)
        self.assertNotIn("feedback_markdown", workflow_inputs)
        self.assertNotIn("provider_target", workflow_inputs)

    def test_reusable_preview_feedback_status_executes_cleanup_outcome_matrix(self) -> None:
        cases = (
            ("success", "no_preview_recorded", "cleared"),
            ("success", "destroyed", "destroyed"),
            ("success", "", "destroyed"),
            ("failure", "no_preview_recorded", "cleanup_failed"),
            ("skipped", "", "cleanup_failed"),
            ("cancelled", "destroyed", "cleanup_failed"),
            ("success", "failed", "cleanup_failed"),
            ("success", "future_outcome", "cleanup_failed"),
        )

        for cleanup_result, cleanup_outcome, expected_status in cases:
            with self.subTest(
                cleanup_result=cleanup_result,
                cleanup_outcome=cleanup_outcome,
            ):
                result, outputs = _run_preview_cleanup_status(
                    cleanup_result=cleanup_result,
                    cleanup_outcome=cleanup_outcome,
                    cleanup_failure_summary="provider teardown failed",
                )

                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(outputs["status"], expected_status)
                if expected_status == "cleanup_failed":
                    self.assertEqual(outputs["failure_summary"], "provider teardown failed")

    def test_reusable_preview_feedback_status_accepts_backend_destroy_outcomes(
        self,
    ) -> None:
        backend_outcomes = {
            _destroy_result_with_record_outcome(
                records={"transition": "destroyed"},
                result={"destroy_status": "pass", "application_id": "app"},
            )["destroy_outcome"],
            _destroy_result_with_record_outcome(
                records={"transition": "destroyed_missing_preview"},
                result={"destroy_status": "pass", "application_id": ""},
            )["destroy_outcome"],
            _destroy_result_with_record_outcome(
                records={"transition": "destroy_failed"},
                result={"destroy_status": "fail", "application_id": "app"},
            )["destroy_outcome"],
        }
        workflow = (REPO_ROOT / ".github/workflows/reusable-preview-feedback-status.yml").read_text(
            encoding="utf-8"
        )

        self.assertEqual(backend_outcomes, {"destroyed", "no_preview_recorded", "failed"})
        for outcome in backend_outcomes:
            self.assertIn(str(outcome), workflow)

    def test_reusable_preview_request_notice_owns_notice_decision(self) -> None:
        workflow_path = REPO_ROOT / ".github/workflows/reusable-preview-request-notice.yml"
        workflow = workflow_path.read_text(encoding="utf-8")
        parsed_workflow = load_workflow(workflow_path)
        workflow_inputs = workflow_call_inputs(parsed_workflow)

        self.assertIn("pull_request_target", workflow)
        self.assertIn("context.eventName !== 'pull_request_target'", workflow)
        self.assertRegex(
            workflow,
            r"uses: actions/github-script@(?:v\d+(?:\.\d+){0,2}|[0-9a-f]{40})(?:\s|$)",
        )
        self.assertIn(
            "uses: ./.github/workflows/reusable-preview-pr-feedback.yml",
            workflow,
        )
        self.assertEqual(
            parsed_workflow.job_uses("cleanup"),
            "./.github/workflows/reusable-generic-web-preview-lifecycle.yml",
        )
        self.assertEqual(
            parsed_workflow.job_uses("feedback-cleanup"),
            "./.github/workflows/reusable-preview-feedback-status.yml",
        )
        self.assertEqual(parsed_workflow.job_permissions("resolve"), {"contents": "read"})
        self.assertIn("status: ${{ needs.resolve.outputs.status }}", workflow)
        self.assertIn("operation: destroy", workflow)
        self.assertIn("mode: cleanup", workflow)
        self.assertIn("cleanup_outcome: ${{ needs.cleanup.outputs.destroy_outcome }}", workflow)
        self.assertIn("const shouldCleanup =", workflow)
        self.assertIn("executionTrust === 'same_repo'", workflow)
        self.assertIn("failure_summary: ${{ needs.resolve.outputs.failure_summary }}", workflow)
        self.assertIn("const unsupportedTrust =", workflow)
        self.assertIn("action === 'edited'", workflow)
        self.assertIn("const shouldSetUnsupported =", workflow)
        self.assertNotIn("status = 'pending'", workflow)

        self.assertNotIn("actions/checkout", workflow)
        self.assertNotIn("ref:", workflow)
        self.assertNotIn("marker", workflow_inputs)
        self.assertNotIn("idempotency-key", workflow_inputs)
        self.assertNotIn("payload", workflow_inputs)
        self.assertNotIn("route-path", workflow_inputs)

    def test_reusable_generic_web_preview_lifecycle_derives_preview_slug(self) -> None:
        workflow_path = REPO_ROOT / ".github/workflows/reusable-generic-web-preview-lifecycle.yml"
        workflow_text = workflow_path.read_text(encoding="utf-8")
        workflow = load_workflow(workflow_path)
        workflow_inputs = workflow_call_inputs(workflow)

        self.assertIn("route-path: /v1/drivers/generic-web/preview-refresh", workflow_text)
        self.assertIn("route-path: /v1/drivers/generic-web/preview-destroy", workflow_text)
        self.assertIn("route-path: /v1/authz-diagnostics/github-actions/evaluate", workflow_text)
        self.assertIn('"action": "preview_refresh.execute"', workflow_text)
        self.assertIn("expected-status: 200,202,403", workflow_text)
        self.assertIn(
            "refresh.anchor_pr_number=${{ needs.resolve.outputs.anchor_pr_number }}",
            workflow_text,
        )
        self.assertIn(
            "destroy.anchor_pr_number=${{ needs.resolve.outputs.anchor_pr_number }}",
            workflow_text,
        )

        resolve_outputs = cast(dict[str, object], workflow.job("resolve")["outputs"])
        self.assertEqual(
            resolve_outputs["timeout_seconds"],
            "${{ steps.request.outputs.timeout_seconds }}",
        )
        resolver = workflow.step_named("resolve", "Resolve Launchplane preview request")
        self.assertIsNotNone(resolver)
        assert resolver is not None
        self.assertIn('TIMEOUT_SECONDS="300"', resolver.run)
        self.assertIn('echo "timeout_seconds=$TIMEOUT_SECONDS"', resolver.run)
        for job_id, field_name in (
            ("refresh", "refresh.timeout_seconds"),
            ("destroy", "destroy.timeout_seconds"),
        ):
            request = workflow.step_named(
                job_id,
                f"Request Launchplane generic-web preview {job_id}",
            )
            self.assertIsNotNone(request)
            assert request is not None
            payload_fields = cast(str, request.with_values["payload-fields"])
            self.assertIn(
                f"{field_name}=${{{{ needs.resolve.outputs.timeout_seconds }}}}",
                payload_fields,
            )
            self.assertNotIn(
                f"{field_name}=${{{{ inputs['timeout-seconds'] }}}}",
                payload_fields,
            )

        self.assertNotIn("preview_slug", workflow_inputs)
        self.assertNotIn("preview_url", workflow_inputs)
        self.assertNotIn('CONTEXT="$PRODUCT"', workflow_text)
        self.assertNotIn("PRODUCT CONTEXT ANCHOR_PR_NUMBER", workflow_text)
        self.assertNotIn("refresh.preview_slug=", workflow_text)
        self.assertNotIn("destroy.preview_slug=", workflow_text)
        self.assertIn("destroy_outcome=result.destroy_outcome", workflow_text)
        self.assertIn("destroy_outcome: ${{ steps.lp.outputs.destroy_outcome }}", workflow_text)

    def test_reusable_generic_web_preview_lifecycle_feedback_maps_record_status(
        self,
    ) -> None:
        workflow = (
            REPO_ROOT / ".github/workflows/reusable-generic-web-preview-lifecycle.yml"
        ).read_text(encoding="utf-8")

        self.assertIn("route-path: /v1/previews/pr-feedback", workflow)
        self.assertIn("feedback_status=result.status", workflow)
        self.assertNotIn("result.feedback_status", workflow)

    def test_reusable_generic_web_preview_verification_derives_context(self) -> None:
        workflow_path = (
            REPO_ROOT / ".github/workflows/reusable-generic-web-preview-verification.yml"
        )
        workflow = (workflow_path).read_text(encoding="utf-8")
        workflow_inputs = workflow_call_inputs(load_workflow(workflow_path))

        self.assertIn("route-path: /v1/drivers/generic-web/preview-verification", workflow)
        self.assertIn(
            "payload-file: .launchplane/generic-web-preview-verification-payload.json",
            workflow,
        )
        self.assertIn("context: process.env.CONTEXT ?? ''", workflow)
        self.assertIn(
            "anchor_pr_number: anchorPrNumber",
            workflow,
        )
        self.assertIn(
            "verification_status: process.env.VERIFICATION_STATUS",
            workflow,
        )
        self.assertIn("verification_status=result.verification_status", workflow)
        self.assertIn("generic-web-preview-verification", workflow)
        self.assertNotIn("payload-fields:", workflow)

        self.assertNotIn("idempotency-key", workflow_inputs)
        self.assertNotIn("payload", workflow_inputs)
        self.assertNotIn("route-path", workflow_inputs)

    def test_reusable_generic_web_preview_facade_keeps_repo_contract_thin(self) -> None:
        workflow_path = REPO_ROOT / ".github/workflows/reusable-generic-web-preview.yml"
        workflow = load_workflow(workflow_path)
        workflow_inputs = workflow_call_inputs(workflow)

        self.assertEqual(
            workflow_inputs,
            {
                "build_args",
                "docker_context",
                "docker_target",
                "dockerfile",
                "image_repository",
                "launchplane_audience",
                "launchplane_url",
                "preview_label",
                "timeout-ms",
                "timeout-seconds",
                "verification_command",
            },
        )
        self.assertNotIn("product", workflow_inputs)
        self.assertNotIn("context", workflow_inputs)
        self.assertNotIn("health_path", workflow_inputs)
        self.assertNotIn("port", workflow_inputs)
        self.assertNotIn("target_id", workflow_inputs)
        self.assertNotIn("domain", workflow_inputs)
        self.assertNotIn("skip_teardown", workflow_inputs)

    def test_reusable_generic_web_preview_facade_isolates_untrusted_jobs(self) -> None:
        workflow = load_workflow(REPO_ROOT / ".github/workflows/reusable-generic-web-preview.yml")

        self.assertEqual(workflow.permissions, {"contents": "read"})
        self.assertEqual(workflow.job_permissions("resolve"), {"contents": "read"})
        self.assertEqual(
            workflow.job_permissions("build"),
            {"contents": "read", "packages": "write"},
        )
        self.assertEqual(workflow.job_permissions("verify"), {"contents": "read"})
        self.assertEqual(workflow.job_permissions("verification-outcome"), {"contents": "read"})
        for job_id in (
            "provision",
            "record-verification-pass",
            "record-verification-fail",
            "feedback-refresh",
        ):
            self.assertEqual(
                workflow.job_permissions(job_id),
                {"contents": "read", "id-token": "write"},
                job_id,
            )

        for job_id in ("build", "verify"):
            checkout = workflow.step_named(
                job_id,
                next(
                    step.name
                    for step in workflow.steps(job_id)
                    if step.uses.startswith("actions/checkout@")
                ),
            )
            self.assertIsNotNone(checkout)
            assert checkout is not None
            self.assertEqual(checkout.with_values.get("persist-credentials"), False)

    def test_reusable_generic_web_preview_facade_composes_typed_stages(self) -> None:
        workflow_path = REPO_ROOT / ".github/workflows/reusable-generic-web-preview.yml"
        workflow = load_workflow(workflow_path)

        self.assertEqual(
            workflow.job_uses("provision"),
            "./.github/workflows/reusable-generic-web-preview-lifecycle.yml",
        )
        for job_id in ("record-verification-pass", "record-verification-fail"):
            self.assertEqual(
                workflow.job_uses(job_id),
                "./.github/workflows/reusable-generic-web-preview-verification.yml",
            )
        provision_inputs = cast(dict[str, object], workflow.job("provision")["with"])
        self.assertEqual(
            provision_inputs["timeout-seconds"],
            "${{ inputs['timeout-seconds'] }}",
        )
        for job_id in ("record-verification-pass", "record-verification-fail"):
            verification_inputs = cast(dict[str, object], workflow.job(job_id)["with"])
            self.assertEqual(
                verification_inputs["timeout-seconds"],
                "${{ inputs['timeout-seconds'] || '300' }}",
                job_id,
            )
        self.assertEqual(
            workflow.job_uses("feedback-refresh"),
            "./.github/workflows/reusable-preview-feedback-status.yml",
        )

        resolver = workflow.step_named("resolve", "Resolve generic-web preview request")
        self.assertIsNotNone(resolver)
        assert resolver is not None
        resolver_script = cast(str, resolver.with_values["script"])
        self.assertIn("context.eventName !== 'pull_request'", resolver_script)
        self.assertIn("repository === headRepository", resolver_script)
        self.assertIn("author !== 'dependabot[bot]'", resolver_script)
        self.assertNotIn("action === 'closed'", resolver_script)
        self.assertNotIn("action === 'unlabeled'", resolver_script)
        workflow_text = workflow_path.read_text(encoding="utf-8")
        self.assertNotIn("pull_request_target", workflow_text)
        self.assertIn("image_reference=${IMAGE_REPOSITORY}@${IMAGE_DIGEST}", workflow_text)
        self.assertLess(
            workflow_text.index("${{ inputs.build_args }}"),
            workflow_text.index("LAUNCHPLANE_BUILD_REVISION="),
        )
        self.assertIn("preview lifecycle failed; see workflow run logs", workflow_text)
        self.assertIn("mode: refresh", workflow_text)
        self.assertNotIn("mode: cleanup", workflow_text)

    def test_generic_web_preview_requests_accept_anchor_pr_number_without_slug(
        self,
    ) -> None:
        refresh_request = GenericWebPreviewRefreshRequest(
            product="demo",
            image_reference="ghcr.io/example/demo@sha256:abc123",
            anchor_pr_number=42,
        )
        destroy_request = GenericWebPreviewDestroyRequest(
            product="demo",
            anchor_pr_number=42,
            destroy_reason="preview_label_removed",
        )

        self.assertEqual(refresh_request.preview_slug, "")
        self.assertEqual(refresh_request.anchor_pr_number, 42)
        self.assertEqual(destroy_request.preview_slug, "")
        self.assertEqual(destroy_request.anchor_pr_number, 42)


class PreviewWorkflowDecisionCliTests(unittest.TestCase):
    def test_cli_derives_refresh_decision_from_github_event_file(self) -> None:
        with TemporaryDirectory() as temp_dir:
            event_file = Path(temp_dir) / "event.json"
            event_file.write_text(
                json.dumps(
                    {
                        "action": "synchronize",
                        "repository": {"full_name": "cbusillo/sellyouroutboard"},
                        "pull_request": {
                            "number": 105,
                            "labels": [{"name": "preview"}],
                            "base": {
                                "repo": {"full_name": "cbusillo/sellyouroutboard"},
                            },
                            "head": {
                                "repo": {"full_name": "cbusillo/sellyouroutboard"},
                                "sha": "abc123",
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )

            result = CliRunner().invoke(
                CLI_MAIN,
                [
                    "work-graph",
                    "preview-workflow-decision",
                    "--event-file",
                    str(event_file),
                    "--event-name",
                    "pull_request",
                    "--actor",
                    "cbusillo",
                    "--product",
                    "sell-your-outboard",
                    "--context",
                    "sellyouroutboard-testing",
                    "--run-id",
                    "123456",
                    "--run-attempt",
                    "2",
                ],
            )

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload["event"]["anchor_pr_number"], 105)
        self.assertEqual(payload["event"]["label_names"], ["preview"])
        self.assertEqual(payload["decision"]["operation"], "refresh")
        self.assertEqual(
            payload["decision"]["launchplane_route_path"],
            "/v1/drivers/generic-web/preview-refresh",
        )
        self.assertEqual(
            payload["idempotency_key"],
            "preview-workflow:sell-your-outboard:sellyouroutboard-testing:refresh:pr-105:123456:2",
        )

    def test_cli_uses_github_environment_when_event_options_are_omitted(self) -> None:
        with TemporaryDirectory() as temp_dir:
            event_file = Path(temp_dir) / "event.json"
            event_file.write_text(
                json.dumps(
                    {
                        "action": "synchronize",
                        "repository": {"full_name": "cbusillo/sellyouroutboard"},
                        "pull_request": {
                            "number": 108,
                            "labels": [{"name": "preview"}],
                            "base": {
                                "repo": {"full_name": "cbusillo/sellyouroutboard"},
                            },
                            "head": {
                                "repo": {"full_name": "cbusillo/sellyouroutboard"},
                                "sha": "def456",
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )

            result = CliRunner().invoke(
                CLI_MAIN,
                [
                    "work-graph",
                    "preview-workflow-decision",
                    "--actor",
                    "cbusillo",
                    "--product",
                    "sell-your-outboard",
                    "--context",
                    "sellyouroutboard-testing",
                    "--run-id",
                    "123459",
                    "--run-attempt",
                    "1",
                ],
                env={
                    "GITHUB_EVENT_NAME": "pull_request",
                    "GITHUB_EVENT_PATH": str(event_file),
                },
            )

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload["event"]["anchor_pr_number"], 108)
        self.assertEqual(payload["event"]["event_name"], "pull_request")
        self.assertEqual(payload["decision"]["operation"], "refresh")

    def test_cli_reports_unsupported_notice_without_untrusted_checkout(self) -> None:
        result = CliRunner().invoke(
            CLI_MAIN,
            [
                "work-graph",
                "preview-workflow-decision",
                "--event-name",
                "pull_request_target",
                "--action",
                "labeled",
                "--repository",
                "cbusillo/sellyouroutboard",
                "--anchor-repo",
                "cbusillo/sellyouroutboard",
                "--anchor-pr-number",
                "106",
                "--actor",
                "contributor",
                "--base-repository",
                "cbusillo/sellyouroutboard",
                "--head-repository",
                "someone/sellyouroutboard",
                "--label",
                "preview",
                "--action-label",
                "preview",
                "--product",
                "sell-your-outboard",
                "--context",
                "sellyouroutboard-testing",
                "--run-id",
                "123457",
                "--run-attempt",
                "1",
            ],
        )

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload["decision"]["operation"], "unsupported_notice")
        self.assertEqual(payload["decision"]["execution_trust"], "fork")
        self.assertFalse(payload["decision"]["checkout_untrusted_head"])
        self.assertEqual(payload["decision"]["feedback_status"], "unsupported")

    def test_cli_omits_idempotency_key_for_ignored_events(self) -> None:
        result = CliRunner().invoke(
            CLI_MAIN,
            [
                "work-graph",
                "preview-workflow-decision",
                "--event-name",
                "pull_request",
                "--action",
                "synchronize",
                "--repository",
                "cbusillo/sellyouroutboard",
                "--anchor-repo",
                "cbusillo/sellyouroutboard",
                "--anchor-pr-number",
                "107",
                "--actor",
                "cbusillo",
                "--base-repository",
                "cbusillo/sellyouroutboard",
                "--head-repository",
                "cbusillo/sellyouroutboard",
                "--label",
                "bug",
                "--product",
                "sell-your-outboard",
                "--context",
                "sellyouroutboard-testing",
                "--run-id",
                "123458",
                "--run-attempt",
                "1",
            ],
        )

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload["decision"]["operation"], "ignore")
        self.assertEqual(payload["idempotency_key"], "")


if __name__ == "__main__":
    unittest.main()
