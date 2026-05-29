from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest import TestCase


def _load_feedback_module() -> ModuleType:
    module_path = (
        Path(__file__).resolve().parents[1] / "scripts" / "merge_train_controller_feedback.py"
    )
    spec = importlib.util.spec_from_file_location("merge_train_controller_feedback", module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


feedback = _load_feedback_module()


class MergeTrainControllerFeedbackTests(TestCase):
    def test_build_feedback_payloads_emits_building_candidate_entries(self) -> None:
        response: dict[str, Any] = {
            "result": {
                "repository": "cbusillo/example",
                "base_branch": "main",
                "controller_action": "plan_candidate",
                "candidate": {
                    "status": "planned",
                    "entries": [
                        {"pull_request_number": 7},
                        {"pull_request_number": 8},
                    ],
                },
            },
            "records": {"merge_train_batch_candidate_record_id": "candidate-123"},
        }

        payloads = feedback.build_feedback_payloads(response=response)

        self.assertEqual([7, 8], [payload["pull_request_number"] for payload in payloads])
        self.assertEqual({"building"}, {payload["event"] for payload in payloads})
        self.assertEqual(
            {"candidate-123"}, {payload["controller_record_id"] for payload in payloads}
        )
        self.assertEqual(
            {"workflow:merge-train-runner"}, {payload["source"] for payload in payloads}
        )

    def test_build_feedback_payloads_marks_pending_checks_waiting(self) -> None:
        response: dict[str, Any] = {
            "result": {
                "repository": "cbusillo/example",
                "base_branch": "main",
                "controller_action": "observe_candidate",
                "candidate": {
                    "status": "building",
                    "required_checks_status": "pending",
                    "entries": [{"pull_request_number": 7}],
                },
            },
            "records": {"merge_train_batch_candidate_record_id": "candidate-123"},
        }

        payloads = feedback.build_feedback_payloads(response=response)

        self.assertEqual(1, len(payloads))
        self.assertEqual("waiting", payloads[0]["event"])
        self.assertIn("waiting", payloads[0]["message"])

    def test_build_feedback_payloads_marks_merged_landing_plan_completed(self) -> None:
        response: dict[str, Any] = {
            "result": {
                "repository": "cbusillo/example",
                "base_branch": "main",
                "controller_action": "land_batch",
                "landing_plan": {
                    "status": "completed",
                    "entries": [
                        {"pull_request_number": 7, "status": "merged"},
                        {"pull_request_number": 8, "status": "merged"},
                    ],
                },
            },
            "records": {"merge_train_batch_landing_plan_record_id": "landing-plan-123"},
        }

        payloads = feedback.build_feedback_payloads(response=response)

        self.assertEqual([7, 8], [payload["pull_request_number"] for payload in payloads])
        self.assertEqual({"completed"}, {payload["event"] for payload in payloads})

    def test_build_feedback_payloads_marks_stale_landing_plan_terminal(self) -> None:
        response: dict[str, Any] = {
            "result": {
                "repository": "cbusillo/example",
                "base_branch": "main",
                "controller_action": "land_batch",
                "landing_plan": {
                    "entries": [
                        {"pull_request_number": 7, "status": "merged"},
                        {"pull_request_number": 8, "status": "stale"},
                    ],
                },
            },
            "records": {"merge_train_batch_landing_plan_record_id": "landing-plan-123"},
        }

        payloads = feedback.build_feedback_payloads(response=response)

        self.assertEqual([7, 8], [payload["pull_request_number"] for payload in payloads])
        self.assertEqual({"stale_policy"}, {payload["event"] for payload in payloads})
        self.assertIn("stale", payloads[0]["message"])

    def test_build_feedback_payloads_skips_actions_without_pr_numbers(self) -> None:
        response: dict[str, Any] = {
            "result": {
                "repository": "cbusillo/example",
                "base_branch": "main",
                "controller_action": "plan_candidate",
                "candidate": {"status": "planned", "entries": []},
            },
            "records": {"merge_train_batch_candidate_record_id": "candidate-123"},
        }

        self.assertEqual([], feedback.build_feedback_payloads(response=response))

    def test_build_feedback_payloads_skips_unknown_actions(self) -> None:
        response: dict[str, Any] = {
            "result": {
                "repository": "cbusillo/example",
                "base_branch": "main",
                "controller_action": "unknown_future_action",
                "candidate": {"entries": [{"pull_request_number": 7}]},
            },
            "records": {},
        }

        self.assertEqual([], feedback.build_feedback_payloads(response=response))

    def test_build_feedback_payloads_infers_candidate_plan_action(self) -> None:
        response: dict[str, Any] = {
            "result": {
                "repository": "cbusillo/example",
                "base_branch": "main",
                "mode": "plan",
                "candidate": {
                    "status": "planned",
                    "entries": [{"pull_request_number": 7}],
                },
            },
            "records": {"merge_train_batch_candidate_record_id": "candidate-123"},
        }

        payloads = feedback.build_feedback_payloads(response=response, phase="batch-candidate")

        self.assertEqual(1, len(payloads))
        self.assertEqual("plan_candidate", payloads[0]["controller_action"])

    def test_build_feedback_payloads_infers_stack_plan_action(self) -> None:
        response: dict[str, Any] = {
            "result": {
                "repository": "cbusillo/example",
                "base_branch": "main",
                "mode": "plan",
                "stack_collapse_plan": {
                    "status": "planned",
                    "entries": [{"pull_request_number": 7}],
                },
            },
            "records": {"merge_train_stack_collapse_plan_record_id": "stack-123"},
        }

        payloads = feedback.build_feedback_payloads(response=response, phase="batch-candidate")

        self.assertEqual(1, len(payloads))
        self.assertEqual("plan_stack_collapse", payloads[0]["controller_action"])

    def test_build_feedback_payloads_infers_landing_action(self) -> None:
        response: dict[str, Any] = {
            "result": {
                "repository": "cbusillo/example",
                "base_branch": "main",
                "mode": "plan",
                "landing_plan": {
                    "status": "planned",
                    "entries": [{"pull_request_number": 7, "status": "planned"}],
                },
            },
            "records": {"merge_train_batch_landing_plan_record_id": "landing-123"},
        }

        payloads = feedback.build_feedback_payloads(response=response, phase="batch-landing")

        self.assertEqual(1, len(payloads))
        self.assertEqual("plan_landing", payloads[0]["controller_action"])
