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
    }
    values.update(overrides)
    return PreviewWorkflowEvent.model_validate(values)


class PreviewWorkflowContractTests(unittest.TestCase):
    def test_same_repo_ready_pr_refreshes_preview_on_every_ready_event(self) -> None:
        for action in ("opened", "reopened", "synchronize", "ready_for_review"):
            with self.subTest(action=action):
                decision = decide_preview_workflow_operation(_event(action=action))

                self.assertEqual(decision.operation, "refresh")
                self.assertEqual(decision.reason, f"pull_request_{action}")
                self.assertEqual(decision.execution_trust, "same_repo")
                self.assertEqual(
                    decision.launchplane_route_path, "/v1/drivers/generic-web/preview-refresh"
                )
                self.assertTrue(decision.product_build_required)
                self.assertTrue(decision.launchplane_feedback_required)

    def test_draft_pr_gets_no_preview(self) -> None:
        decision = decide_preview_workflow_operation(_event(action="opened", draft=True))

        self.assertEqual(decision.operation, "ignore")
        self.assertEqual(decision.reason, "pull_request_draft")

    def test_converting_to_draft_destroys_the_preview(self) -> None:
        decision = decide_preview_workflow_operation(
            _event(event_name="pull_request_target", action="converted_to_draft", draft=True)
        )

        self.assertEqual(decision.operation, "destroy")
        self.assertEqual(decision.reason, "pull_request_converted_to_draft")
        self.assertEqual(decision.feedback_status, "destroyed")

    def test_label_events_change_no_preview(self) -> None:
        for event_name in ("pull_request", "pull_request_target"):
            for action in ("labeled", "unlabeled"):
                with self.subTest(event_name=event_name, action=action):
                    decision = decide_preview_workflow_operation(
                        _event(event_name=event_name, action=action)
                    )

                    self.assertEqual(decision.operation, "ignore")

    def test_pull_request_target_fork_preview_writes_unsupported_notice_only(self) -> None:
        decision = decide_preview_workflow_operation(
            _event(
                event_name="pull_request_target",
                action="opened",
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
        for action in ("closed", "converted_to_draft"):
            with self.subTest(action=action):
                decision = decide_preview_workflow_operation(_event(action=action))

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

    def test_reusable_preview_feedback_status_maps_backend_destroy_outcomes(
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
        expected_statuses = {
            "destroyed": "destroyed",
            "no_preview_recorded": "cleared",
            "failed": "cleanup_failed",
        }

        self.assertEqual(backend_outcomes, set(expected_statuses))
        for outcome, expected_status in expected_statuses.items():
            with self.subTest(outcome=outcome):
                result, outputs = _run_preview_cleanup_status(
                    cleanup_result="success",
                    cleanup_outcome=outcome,
                )

                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(outputs["status"], expected_status)

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
            destroy_reason="pull_request_closed",
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
                            "draft": False,
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
        self.assertFalse(payload["event"]["draft"])
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
                "opened",
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
                "--draft",
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
