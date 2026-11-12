from __future__ import annotations

from typing import Any, cast
from unittest import TestCase

from control_plane import merge_train_controller_feedback as feedback
from control_plane.merge_train import build_merge_train_dry_run_result
from control_plane.merge_train import MergeTrainDryRunSnapshot
from control_plane.merge_train_handoff import MergeTrainReadyHandoff, ready_handoff_refusal
from tests.merge_train_policy_fixtures import build_test_merge_train_policy
from tests.merge_train_policy_fixtures import dependency_bot_policy
from tests.support.merge_train import _FakeExpandedMergeTrainSnapshotReader
from tests.test_merge_train import _pull_request


class MergeTrainControllerFeedbackTests(TestCase):
    def test_ready_stack_child_reports_parent_wait_and_rechecks_without_flat_admission(
        self,
    ) -> None:
        policy = build_test_merge_train_policy(trusted_automation_github_user_ids=(20,))
        repository = policy.policies[0].repository
        parent = _pull_request(
            8, labels=(), actor_id=20, head_ref="feature/root", repository=repository
        )
        child = _pull_request(
            9,
            labels=(),
            actor_id=20,
            head_ref="feature/child",
            base_ref=parent.head_ref,
            repository=repository,
        )
        handoff = MergeTrainReadyHandoff(
            status="ready", head_sha=child.head_sha, actor_id=20, comment_id=100
        )
        child.ready_handoff = handoff
        grandchild = _pull_request(
            10,
            labels=(),
            actor_id=20,
            head_ref="feature/leaf",
            base_ref=child.head_ref,
            repository=repository,
        )
        grandchild.ready_handoff = handoff.model_copy(update={"head_sha": grandchild.head_sha})
        fork = _pull_request(
            11,
            labels=(),
            actor_id=20,
            head_ref=parent.head_ref,
            repository=repository,
            head_repository="example/fork",
        )
        for parent_ready in (False, True):
            with self.subTest(parent_ready=parent_ready):
                if parent_ready:
                    parent.ready_handoff = handoff.model_copy(update={"head_sha": parent.head_sha})
                result = build_merge_train_dry_run_result(
                    policy=policy,
                    snapshot=MergeTrainDryRunSnapshot(
                        repository=repository,
                        base_branch="main",
                        pull_requests=(parent, child, grandchild, fork),
                    ),
                    batch_landing=True,
                )
                payloads = feedback.build_feedback_payloads(
                    response={
                        "result": {
                            "repository": repository,
                            "base_branch": "main",
                            "controller_action": "idle",
                            "dry_run_result": result.model_dump(mode="json"),
                        }
                    }
                )
                child_payloads = [p for p in payloads if p["pull_request_number"] == child.number]
                self.assertEqual(len(child_payloads), 1)
                message = child_payloads[0]
                self.assertIn(f"#{parent.number}", cast(str, message["message"]))
                if not parent_ready:
                    for reason in result.queue[0].ineligible_reasons:
                        self.assertIn(reason, cast(str, message["message"]))
                        leaf_message = next(
                            p for p in payloads if p["pull_request_number"] == grandchild.number
                        )
                        self.assertIn(reason, cast(str, leaf_message["message"]))
                self.assertNotIn(f"#{fork.number}", cast(str, message["message"]))
                self.assertEqual(result.queue_order, (parent.number,) if parent_ready else ())
                self.assertEqual([e.number for e in result.queue], [parent.number, fork.number])
                self.assertFalse(result.stack_waits[0].eligible)
                self.assertEqual(message["event"], "waiting")
                self.assertFalse(message["controller_action"])
                self.assertFalse(message["controller_record_id"])

    def test_excluded_ready_gets_its_queue_reason_even_when_train_is_idle(self) -> None:
        repository = "example/site"
        policy = build_test_merge_train_policy(
            repository=repository, trusted_automation_github_user_ids=(20,)
        )
        policy.policies[0].enqueue.dependency_update_bots = (dependency_bot_policy(20),)
        head = "a" * 40
        handoff = MergeTrainReadyHandoff(status="ready", head_sha=head, actor_id=20, comment_id=100)
        refused = _pull_request(1, labels=(), actor_id=999, repository=repository).model_copy(
            update={"head_sha": head, "actor_role": "unknown", "ready_handoff": handoff}
        )
        stale = refused.model_copy(
            update={"actor_id": 20, "head_sha": "b" * 40, "dependency_update_class": "needs_review"}
        )
        unrequested = refused.model_copy(update={"number": 2, "ready_handoff": None})
        blocked = refused.model_copy(update={"labels": (policy.policies[0].blocked_label,)})
        client_wait = refused.model_copy(
            update={
                "actor_id": 20,
                "ready_handoff": handoff.model_copy(
                    update={"status": "waiting", "client_wait": True}
                ),
            }
        )
        pending = refused.model_copy(update={"actor_id": 20, "required_checks_status": "pending"})
        edited = refused.model_copy(
            update={
                "ready_handoff": handoff.model_copy(update={"status": "edited", "head_sha": ""}),
            }
        )
        for change in (refused, stale, blocked, client_wait, pending, edited):
            for another_candidate in (False, True):
                with self.subTest(change=change, another_candidate=another_candidate):
                    dry_run = build_merge_train_dry_run_result(
                        policy=policy,
                        snapshot=MergeTrainDryRunSnapshot(
                            repository=repository,
                            base_branch="main",
                            pull_requests=(change, unrequested),
                        ),
                        batch_landing=True,
                    )
                    self.assertFalse(dry_run.queue_order)
                    if change is stale:
                        self.assertIn(
                            ready_handoff_refusal(policy.policies[0], handoff, change.head_sha),
                            dry_run.queue[0].ineligible_reasons,
                        )
                    payloads = feedback.build_feedback_payloads(
                        response={
                            "records": {
                                "merge_train_batch_candidate_record_id": "unrelated-candidate"
                            },
                            "result": {
                                "repository": repository,
                                "base_branch": "main",
                                "controller_action": "plan_candidate"
                                if another_candidate
                                else "idle",
                                "candidate": {
                                    "status": "planned",
                                    "entries": [{"pull_request_number": 3}],
                                }
                                if another_candidate
                                else {},
                                "dry_run_result": dry_run.model_dump(mode="json"),
                            },
                        }
                    )
                    excluded = [p for p in payloads if p["pull_request_number"] == change.number]
                    if change is blocked or change is edited:
                        self.assertFalse(excluded)
                        continue
                    self.assertEqual(len(excluded), 1)
                    self.assertEqual(excluded[0]["event"], "waiting")
                    self.assertFalse(excluded[0]["controller_action"])
                    self.assertFalse(excluded[0]["controller_record_id"])
                    for reason in dry_run.queue[0].ineligible_reasons:
                        self.assertIn(reason, cast(str, excluded[0]["message"]))
                    self.assertNotIn(
                        unrequested.number, [p["pull_request_number"] for p in payloads]
                    )

    def test_unverified_base_retest_removal_does_not_claim_verified_recovery(self) -> None:
        for observation in ("absent_after_uncertain_attempt", "removed_by_foreign_actor"):
            with self.subTest(observation=observation):
                message = feedback._feedback_message(
                    controller_action="recover_candidate_block",
                    event="waiting",
                    result={
                        "reason_code": "candidate_block_new_base_retest",
                        "details": {"removal_observation": observation},
                    },
                    controller_record_id="failed-candidate",
                    phase="controller",
                )

                self.assertEqual(
                    message,
                    feedback._feedback_message(
                        controller_action="recover_candidate_block",
                        event="waiting",
                        result={"details": {"removal_observation": observation}},
                        controller_record_id="failed-candidate",
                        phase="controller",
                    ),
                )

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

    def test_invalid_shared_notes_block_the_member_before_building_and_explain_why(self) -> None:
        snapshot = _FakeExpandedMergeTrainSnapshotReader(
            transport=object()
        ).read_merge_train_snapshot(repository="cbusillo/sellyouroutboard", base_branch="main")
        bad = snapshot.pull_requests[1].model_copy(
            update={
                "owner_review_required": False,
                "required_checks_status": "fail",
                "client_test_notes_error": "Client test notes are missing.",
            }
        )
        snapshot = snapshot.model_copy(update={"pull_requests": (snapshot.pull_requests[0], bad)})
        dry_run = build_merge_train_dry_run_result(
            policy=build_test_merge_train_policy(), snapshot=snapshot, batch_landing=True
        )
        self.assertEqual(dry_run.intended_next_action, "block")
        assert dry_run.selected_pr is not None
        self.assertEqual(dry_run.selected_pr.number, bad.number)
        for applied in (False, True):
            payloads = feedback.build_feedback_payloads(
                response={
                    "result": {
                        "repository": dry_run.repository,
                        "base_branch": dry_run.base_branch,
                        "mode": "dry-run",
                        "controller_action": "block",
                        "dry_run_result": dry_run.model_dump(mode="json"),
                        "block_result": {"status": "blocked"} if applied else {},
                    },
                    "records": {},
                }
            )
            self.assertEqual(payloads[0]["pull_request_number"], bad.number)
            self.assertIn(bad.client_test_notes_error, cast(str, payloads[0]["message"]))
        blocked = bad.model_copy(update={"labels": (*bad.labels, dry_run.blocked_label)})
        next_run = build_merge_train_dry_run_result(
            policy=build_test_merge_train_policy(),
            snapshot=snapshot.model_copy(
                update={"pull_requests": (snapshot.pull_requests[0], blocked)}
            ),
            batch_landing=True,
        )
        self.assertEqual(next_run.queue_order, (snapshot.pull_requests[0].number,))
        self.assertEqual(next_run.intended_next_action, "merge")

    def test_controller_refresh_without_a_receipt_does_not_claim_an_update(self) -> None:
        payloads = feedback.build_feedback_payloads(
            response={
                "result": {
                    "repository": "example/repo",
                    "base_branch": "main",
                    "controller_action": "update_branch",
                    "dry_run_result": {"selected_pr": {"number": 7}},
                },
                "records": {},
            }
        )
        self.assertEqual([7], [payload["pull_request_number"] for payload in payloads])
        self.assertIn("refresh", cast(str, payloads[0]["message"]))
        self.assertNotIn("updated", cast(str, payloads[0]["message"]))

    def test_build_feedback_payloads_marks_pending_checks_waiting(self) -> None:
        diagnostic = "missing-example-gate"
        response: dict[str, Any] = {
            "result": {
                "repository": "cbusillo/example",
                "base_branch": "main",
                "controller_action": "observe_candidate",
                "candidate": {
                    "status": "building",
                    "required_checks_status": "pending",
                    "required_checks_observation_error": diagnostic,
                    "entries": [{"pull_request_number": 7}],
                },
            },
            "records": {"merge_train_batch_candidate_record_id": "candidate-123"},
        }

        payloads = feedback.build_feedback_payloads(response=response)

        self.assertEqual(1, len(payloads))
        self.assertEqual("waiting", payloads[0]["event"])
        self.assertIn("waiting", cast(str, payloads[0]["message"]))
        self.assertIn(diagnostic, cast(str, payloads[0]["message"]))

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
        self.assertIn("stale", cast(str, payloads[0]["message"]))

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
            all("protected batch PR #99" in cast(str, payload["message"]) for payload in payloads)
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
        self.assertIn("Fresh merge readiness evidence", cast(str, payloads[0]["message"]))

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
                {
                    "required_checks_status": "pass",
                    "branch_update_required": True,
                    "requires_individual_landing": True,
                },
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
        response["result"]["dry_run_result"]["selected_pr"] = {"number": 7}
        response["result"]["blocking_reason"] = {"message": "child #8 is held"}
        payloads = feedback.build_feedback_payloads(response=response, phase="batch-candidate")
        self.assertEqual([p["pull_request_number"] for p in payloads], [7])
        self.assertIn(
            response["result"]["blocking_reason"]["message"], cast(str, payloads[0]["message"])
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
