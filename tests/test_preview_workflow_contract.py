import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast

from click import Command
from click.testing import CliRunner

from control_plane.cli import main

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


CLI_MAIN = cast(Command, main)
REPO_ROOT = Path(__file__).resolve().parents[1]


def _workflow_call_inputs(workflow: str) -> set[str]:
    inputs: set[str] = set()
    in_inputs = False
    input_indent = 0
    for line in workflow.splitlines():
        stripped = line.strip()
        if stripped == "inputs:":
            in_inputs = True
            input_indent = len(line) - len(line.lstrip())
            continue
        if in_inputs:
            indent = len(line) - len(line.lstrip())
            if stripped and indent <= input_indent:
                break
            if indent == input_indent + 2 and stripped.endswith(":"):
                inputs.add(stripped[:-1])
    return inputs


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

    def test_same_repo_preview_label_removal_destroys_preview(self) -> None:
        decision = decide_preview_workflow_operation(
            _event(action="unlabeled", action_label="preview", label_names=("bug",))
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

    def test_pull_request_target_same_repo_is_ignored(self) -> None:
        decision = decide_preview_workflow_operation(_event(event_name="pull_request_target"))

        self.assertEqual(decision.operation, "ignore")
        self.assertEqual(decision.reason, "pull_request_target_is_only_for_unsupported_notices")

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
        workflow_inputs = _workflow_call_inputs(workflow)

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

    def test_reusable_preview_request_notice_owns_notice_decision(self) -> None:
        workflow_path = REPO_ROOT / ".github/workflows/reusable-preview-request-notice.yml"
        workflow = workflow_path.read_text(encoding="utf-8")
        workflow_inputs = _workflow_call_inputs(workflow)

        self.assertIn("pull_request_target", workflow)
        self.assertIn("context.eventName !== 'pull_request_target'", workflow)
        self.assertIn("uses: actions/github-script@v8", workflow)
        self.assertIn(
            "uses: cbusillo/launchplane/.github/workflows/reusable-preview-pr-feedback.yml@main",
            workflow,
        )
        self.assertIn("status: ${{ needs.resolve.outputs.status }}", workflow)
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
        workflow = (
            REPO_ROOT / ".github/workflows/reusable-generic-web-preview-lifecycle.yml"
        ).read_text(encoding="utf-8")
        workflow_inputs = _workflow_call_inputs(workflow)

        self.assertIn("route-path: /v1/drivers/generic-web/preview-refresh", workflow)
        self.assertIn("route-path: /v1/drivers/generic-web/preview-destroy", workflow)
        self.assertIn(
            "refresh.anchor_pr_number=${{ needs.resolve.outputs.anchor_pr_number }}", workflow
        )
        self.assertIn(
            "destroy.anchor_pr_number=${{ needs.resolve.outputs.anchor_pr_number }}", workflow
        )

        self.assertNotIn("preview_slug", workflow_inputs)
        self.assertNotIn("preview_url", workflow_inputs)
        self.assertNotIn('CONTEXT="$PRODUCT"', workflow)
        self.assertNotIn("PRODUCT CONTEXT ANCHOR_PR_NUMBER", workflow)
        self.assertNotIn("refresh.preview_slug=", workflow)
        self.assertNotIn("destroy.preview_slug=", workflow)

    def test_reusable_generic_web_preview_lifecycle_feedback_maps_record_status(
        self,
    ) -> None:
        workflow = (
            REPO_ROOT / ".github/workflows/reusable-generic-web-preview-lifecycle.yml"
        ).read_text(encoding="utf-8")

        self.assertIn("route-path: /v1/previews/pr-feedback", workflow)
        self.assertIn("feedback_status=result.status", workflow)
        self.assertNotIn("result.feedback_status", workflow)

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
