from __future__ import annotations

from typing import Any, cast
from unittest import TestCase

from control_plane import merge_train_controller_feedback as feedback
from control_plane.merge_train import build_merge_train_dry_run_result
from tests.merge_train_policy_fixtures import build_test_merge_train_policy
from tests.support.merge_train import _FakeExpandedMergeTrainSnapshotReader


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

    def test_planned_candidate_tells_held_out_prs_what_they_conflict_with(self) -> None:
        response: dict[str, Any] = {
            "result": {
                "repository": "cbusillo/example",
                "base_branch": "main",
                "controller_action": "plan_candidate",
                "candidate": {
                    "status": "planned",
                    "entries": [{"pull_request_number": 92}, {"pull_request_number": 118}],
                    "held_out": [
                        {"pull_request_number": 97, "conflicts_with": [92]},
                        {"pull_request_number": 120, "conflicts_with": []},
                    ],
                },
            },
            "records": {"merge_train_batch_candidate_record_id": "candidate-123"},
        }

        payloads = feedback.build_feedback_payloads(response=response)

        self.assertEqual(
            [(payload["pull_request_number"], payload["event"]) for payload in payloads],
            [(92, "building"), (118, "building"), (97, "blocked"), (120, "blocked")],
        )
        self.assertIn("#92", cast(str, payloads[2]["message"]))
        self.assertIn("`main`", cast(str, payloads[3]["message"]))

    def test_client_review_wait_reaches_the_pr_before_a_candidate_exists(self) -> None:
        snapshot = _FakeExpandedMergeTrainSnapshotReader(
            transport=object()
        ).read_merge_train_snapshot(repository="cbusillo/sellyouroutboard", base_branch="main")
        labelled = snapshot.pull_requests[1]
        for status, event in (("pending", "waiting"), ("fail", "blocked")):
            with self.subTest(status=status):
                dry_run_result = build_merge_train_dry_run_result(
                    policy=build_test_merge_train_policy(),
                    snapshot=snapshot.model_copy(
                        update={
                            "pull_requests": (
                                snapshot.pull_requests[0],
                                labelled.model_copy(
                                    update={
                                        "owner_review_required": True,
                                        "required_checks_status": status,
                                    }
                                ),
                            )
                        }
                    ),
                    batch_landing=True,
                )
                # The controller's initial planning result: no candidate, no record.
                payloads = feedback.build_feedback_payloads(
                    response={
                        "result": {
                            "repository": dry_run_result.repository,
                            "base_branch": dry_run_result.base_branch,
                            "mode": "dry-run",
                            "controller_action": dry_run_result.intended_next_action,
                            "dry_run_result": dry_run_result.model_dump(mode="json"),
                        },
                        "records": {},
                    }
                )
                self.assertEqual(
                    [(payload["pull_request_number"], payload["event"]) for payload in payloads],
                    [(labelled.number, event)],
                )
                message = cast(str, payloads[0]["message"])
                self.assertIn("review", message)
                self.assertNotIn(dry_run_result.blocked_label, message)

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
        self.assertIn("waiting", str(payloads[0]["message"]))

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
        self.assertIn("stale", str(payloads[0]["message"]))

    def test_completed_batch_feedback_links_every_original_to_the_shared_pr(self) -> None:
        payloads = feedback.build_feedback_payloads(
            response={
                "result": {
                    "repository": "example/repo",
                    "base_branch": "main",
                    "controller_action": "land_batch",
                    "landing_plan": {
                        "candidate_pull_request_number": 99,
                        "entries": [
                            {"pull_request_number": 7, "status": "merged"},
                            {"pull_request_number": 8, "status": "merged"},
                        ],
                    },
                },
                "records": {"merge_train_batch_landing_plan_record_id": "batch-landing"},
            }
        )
        self.assertEqual([payload["pull_request_number"] for payload in payloads], [7, 8])
        self.assertTrue(all(payload["event"] == "completed" for payload in payloads))
        self.assertTrue(
            all("protected batch PR #99" in str(payload["message"]) for payload in payloads)
        )

    def test_build_feedback_payloads_reports_admission_block_detail(self) -> None:
        response: dict[str, Any] = {
            "result": {
                "repository": "cbusillo/example",
                "base_branch": "main",
                "controller_action": "block",
                "blocking_reason": {
                    "code": "merge_readiness_not_ready",
                    "message": "Fresh merge readiness evidence did not admit the provider effect.",
                },
                "landing_plan": {
                    "entries": [{"pull_request_number": 7, "status": "planned"}],
                },
            },
            "records": {"merge_train_batch_landing_plan_record_id": "landing-plan-123"},
        }

        payloads = feedback.build_feedback_payloads(response=response)

        self.assertEqual(1, len(payloads))
        self.assertEqual("blocked", payloads[0]["event"])
        self.assertIn("Fresh merge readiness evidence", str(payloads[0]["message"]))

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
                "mode": "plan",
                "candidate": {
                    "repository": "cbusillo/example",
                    "base_branch": "main",
                    "status": "planned",
                    "required_checks_status": "unknown",
                    "entries": [
                        {"pull_request_number": 7},
                        {"pull_request_number": 8},
                    ],
                },
                "dry_run_result": {
                    "repository": "cbusillo/example",
                    "base_branch": "main",
                },
            },
            "records": {"merge_train_batch_candidate_record_id": "candidate-123"},
        }

        payloads = feedback.build_feedback_payloads(response=response, phase="batch-candidate")

        self.assertEqual([7, 8], [payload["pull_request_number"] for payload in payloads])
        self.assertEqual({"cbusillo/example"}, {payload["repository"] for payload in payloads})
        self.assertEqual({"main"}, {payload["base_branch"] for payload in payloads})
        self.assertEqual({"plan_candidate"}, {payload["controller_action"] for payload in payloads})
        self.assertEqual({"waiting"}, {payload["event"] for payload in payloads})
        self.assertEqual(
            {"candidate-123"}, {payload["controller_record_id"] for payload in payloads}
        )

    def test_build_feedback_payloads_uses_candidate_identity_for_build_and_observe(self) -> None:
        for mode in ("build", "observe"):
            with self.subTest(mode=mode):
                response: dict[str, Any] = {
                    "result": {
                        "mode": mode,
                        "candidate": {
                            "repository": "cbusillo/example",
                            "base_branch": "main",
                            "status": "building",
                            "entries": [{"pull_request_number": 7}],
                        },
                    },
                    "records": {"merge_train_batch_candidate_record_id": "candidate-123"},
                }

                payloads = feedback.build_feedback_payloads(
                    response=response, phase="batch-candidate"
                )

                self.assertEqual(1, len(payloads))
                self.assertEqual("cbusillo/example", payloads[0]["repository"])
                self.assertEqual(f"{mode}_candidate", payloads[0]["controller_action"])

    def test_manual_plan_reports_selected_queue_decision_without_a_record(self) -> None:
        snapshot = _FakeExpandedMergeTrainSnapshotReader(
            transport=object()
        ).read_merge_train_snapshot(repository="cbusillo/sellyouroutboard", base_branch="main")
        selected = snapshot.pull_requests[1]
        cases = (
            ({"required_checks_status": "pending"}, "wait_for_checks", "waiting"),
            (
                {"required_checks_status": "pass", "branch_update_required": True},
                "update_branch",
                "blocked",
            ),
            (
                {"required_checks_status": "pending", "owner_review_required": True},
                "wait_for_checks",
                "waiting",
            ),
            ({"required_checks_status": "fail", "owner_review_required": True}, "block", "blocked"),
        )
        for update, action, event in cases:
            with self.subTest(update=update):
                dry_run = build_merge_train_dry_run_result(
                    policy=build_test_merge_train_policy(),
                    snapshot=snapshot.model_copy(
                        update={"pull_requests": (selected.model_copy(update=update),)}
                    ),
                    batch_landing=True,
                )
                self.assertEqual(dry_run.intended_next_action, action)
                payloads = feedback.build_feedback_payloads(
                    response={
                        "result": {
                            "mode": "plan",
                            "next_action": dry_run.intended_next_action,
                            "dry_run_result": dry_run.model_dump(mode="json"),
                        },
                        "records": {},
                    },
                    phase="batch-candidate",
                )
                self.assertEqual(len(payloads), 1)
                payload = payloads[0]
                self.assertEqual(payload["pull_request_number"], selected.number)
                self.assertEqual(payload["repository"], dry_run.repository)
                self.assertEqual(payload["base_branch"], dry_run.base_branch)
                self.assertEqual(payload["controller_action"], action)
                self.assertEqual(payload["event"], event)
                self.assertEqual(payload["controller_record_id"], "")
                message = cast(str, payload["message"])
                if update.get("owner_review_required"):
                    self.assertIn("review", message)
                    self.assertNotIn(dry_run.blocked_label, message)
                elif action == "update_branch":
                    self.assertIn("Refresh", message)

    def test_manual_queue_decision_requires_complete_dry_run_identity(self) -> None:
        for identity in ({}, {"repository": "cbusillo/example"}, {"base_branch": "main"}):
            with self.subTest(identity=identity), self.assertRaises(ValueError):
                feedback.build_feedback_payloads(
                    response={
                        "result": {
                            "mode": "plan",
                            "next_action": "wait_for_checks",
                            "dry_run_result": {**identity, "selected_pr": {"number": 7}},
                        }
                    },
                    phase="batch-candidate",
                )

    def test_build_feedback_payloads_infers_stack_plan_action(self) -> None:
        response: dict[str, Any] = {
            "result": {
                "mode": "plan",
                "dry_run_result": {
                    "repository": "cbusillo/example",
                    "base_branch": "main",
                },
                "stack_collapse_plan": {
                    "repository": "cbusillo/example",
                    "base_branch": "main",
                    "status": "planned",
                    "entries": [{"pull_request_number": 7}],
                },
            },
            "records": {"merge_train_stack_collapse_plan_record_id": "stack-123"},
        }

        payloads = feedback.build_feedback_payloads(response=response, phase="batch-candidate")

        self.assertEqual(1, len(payloads))
        self.assertEqual("plan_stack_collapse", payloads[0]["controller_action"])

    def test_build_feedback_payloads_handles_stack_unsupported_without_entries(self) -> None:
        response: dict[str, Any] = {
            "result": {
                "mode": "plan",
                "next_action": "stack_unsupported",
                "dry_run_result": {
                    "repository": "cbusillo/example",
                    "base_branch": "main",
                },
            },
            "records": {},
        }

        self.assertEqual(
            [], feedback.build_feedback_payloads(response=response, phase="batch-candidate")
        )

    def test_build_feedback_payloads_uses_stack_phase_identity(self) -> None:
        execute_response: dict[str, Any] = {
            "result": {
                "mode": "execute",
                "stack_collapse_plan": {
                    "repository": "cbusillo/example",
                    "base_branch": "main",
                    "entries": [{"pull_request_number": 7}],
                },
            },
            "records": {"merge_train_stack_collapse_plan_record_id": "stack-123"},
        }
        admit_response: dict[str, Any] = {
            "result": {
                "mode": "admit",
                "candidate": {
                    "repository": "cbusillo/example",
                    "base_branch": "main",
                    "status": "planned",
                    "entries": [{"pull_request_number": 7}],
                },
                "dry_run_result": {
                    "repository": "cbusillo/example",
                    "base_branch": "main",
                },
            },
            "records": {"merge_train_batch_candidate_record_id": "candidate-123"},
        }

        execute_payload = feedback.build_feedback_payloads(
            response=execute_response, phase="stack-collapse"
        )[0]
        admit_payload = feedback.build_feedback_payloads(
            response=admit_response, phase="stack-collapse"
        )[0]

        self.assertEqual("cbusillo/example", execute_payload["repository"])
        self.assertEqual("execute_stack_collapse", execute_payload["controller_action"])
        self.assertEqual("cbusillo/example", admit_payload["repository"])
        self.assertEqual("admit_collapsed_root", admit_payload["controller_action"])

    def test_build_feedback_payloads_infers_landing_action(self) -> None:
        response: dict[str, Any] = {
            "result": {
                "mode": "plan",
                "landing_plan": {
                    "repository": "cbusillo/example",
                    "base_branch": "main",
                    "status": "planned",
                    "entries": [{"pull_request_number": 7, "status": "planned"}],
                },
            },
            "records": {"merge_train_batch_landing_plan_record_id": "landing-123"},
        }

        payloads = feedback.build_feedback_payloads(response=response, phase="batch-landing")

        self.assertEqual(1, len(payloads))
        self.assertEqual("plan_landing", payloads[0]["controller_action"])

    def test_build_feedback_payloads_cross_checks_landing_stack_identity(self) -> None:
        response: dict[str, Any] = {
            "result": {
                "mode": "land",
                "landing_plan": {
                    "repository": "cbusillo/example",
                    "base_branch": "main",
                    "entries": [{"pull_request_number": 7, "status": "merged"}],
                },
                "stack_collapse_plan": {
                    "repository": "cbusillo/example",
                    "base_branch": "main",
                    "entries": [{"pull_request_number": 7}],
                },
            },
            "records": {"merge_train_batch_landing_plan_record_id": "landing-123"},
        }

        payloads = feedback.build_feedback_payloads(response=response, phase="batch-landing")

        self.assertEqual(1, len(payloads))
        self.assertEqual("completed", payloads[0]["event"])
        self.assertEqual("land_batch", payloads[0]["controller_action"])

    def test_build_feedback_payloads_rejects_conflicting_nested_identity(self) -> None:
        response: dict[str, Any] = {
            "result": {
                "mode": "plan",
                "candidate": {
                    "repository": "cbusillo/example",
                    "base_branch": "main",
                    "status": "planned",
                    "entries": [{"pull_request_number": 7}],
                },
                "dry_run_result": {
                    "repository": "cbusillo/other",
                    "base_branch": "main",
                },
            },
            "records": {"merge_train_batch_candidate_record_id": "candidate-123"},
        }

        with self.assertRaisesRegex(ValueError, "identity mismatch"):
            feedback.build_feedback_payloads(response=response, phase="batch-candidate")

    def test_build_feedback_payloads_rejects_conflicting_landing_stack_identity(self) -> None:
        response: dict[str, Any] = {
            "result": {
                "mode": "land",
                "landing_plan": {
                    "repository": "cbusillo/example",
                    "base_branch": "main",
                    "entries": [{"pull_request_number": 7, "status": "merged"}],
                },
                "stack_collapse_plan": {
                    "repository": "cbusillo/other",
                    "base_branch": "main",
                    "entries": [{"pull_request_number": 7}],
                },
            },
            "records": {"merge_train_batch_landing_plan_record_id": "landing-123"},
        }

        with self.assertRaisesRegex(ValueError, "identity mismatch"):
            feedback.build_feedback_payloads(response=response, phase="batch-landing")

    def test_build_feedback_payloads_rejects_missing_or_partial_phase_identity(self) -> None:
        responses: tuple[dict[str, Any], ...] = (
            {
                "result": {
                    "mode": "build",
                    "candidate": {
                        "status": "building",
                        "entries": [{"pull_request_number": 7}],
                    },
                }
            },
            {
                "result": {
                    "mode": "plan",
                    "landing_plan": {
                        "repository": "cbusillo/example",
                        "entries": [{"pull_request_number": 7}],
                    },
                }
            },
        )

        phases = ("batch-candidate", "batch-landing")
        for response, phase in zip(responses, phases, strict=True):
            with self.subTest(phase=phase), self.assertRaises(ValueError):
                feedback.build_feedback_payloads(response=response, phase=phase)
