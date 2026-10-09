from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import MagicMock, patch
from typing import cast

from control_plane import merge_train_scheduler
from control_plane.merge_train import (
    MergeTrainBranchClient,
    apply_merge_train_branch_update_intent,
    build_merge_train_dry_run_result,
)
from control_plane.merge_admission_live import LiveMergeAdmissionEvaluator
from control_plane.repository_evidence import GitHubRepositoryEvidenceProvider
from control_plane.contracts.merge_train_controller_state import (
    MergeTrainControllerLeaseHeldError,
)
from control_plane.contracts.merge_train_policy import (
    MergeTrainPolicyRecord,
    MergeTrainSchedulerPolicy,
    parse_merge_train_policy_toml,
)
from control_plane.merge_train_controller_run_once import MergeTrainControllerRunOnceResult
from control_plane.merge_train_policy_source import MergeTrainPolicyStoreMissingError
from control_plane.merge_train_scheduler import (
    MergeTrainScheduledTargetResult,
    run_merge_train_scheduler_loop,
    run_merge_train_scheduler_pass,
)
from tests.merge_train_policy_fixtures import _policy_table
from tests.support.merge_train import _FakeExpandedMergeTrainSnapshotReader

_ROOT = Path("/tmp/launchplane-test-root")


def _policy_record(
    *schedulers: tuple[str, MergeTrainSchedulerPolicy],
) -> MergeTrainPolicyRecord:
    policy = parse_merge_train_policy_toml(
        "\n\n".join(
            ("schema_version = 1", *(_policy_table(repository) for repository, _ in schedulers))
        )
    )
    scheduler_by_repository = dict(schedulers)
    policy = policy.model_copy(
        update={
            "policies": tuple(
                repository_policy.model_copy(
                    update={"scheduler": scheduler_by_repository[repository_policy.repository]}
                )
                for repository_policy in policy.policies
            )
        }
    )
    return MergeTrainPolicyRecord(
        record_id="merge-train-policy-scheduler-test",
        source="test",
        updated_at="2026-10-01T00:00:00Z",
        policy=policy,
    )


def _admission(status: str, reason_code: str = "") -> SimpleNamespace:
    return SimpleNamespace(status=status, reason_code=reason_code)


def _controller_result() -> MergeTrainControllerRunOnceResult:
    return MergeTrainControllerRunOnceResult(
        accepted_result={
            "repository": "cbusillo/alpha",
            "base_branch": "main",
            "controller_action": "wait_for_checks",
            "candidate": {"entries": [{"pull_request_number": 7}]},
        },
        records={"merge_train_controller_state_record_id": "controller-1"},
    )


class MergeTrainSchedulerPassTests(TestCase):
    def setUp(self) -> None:
        self.record_store = MagicMock()
        self.patchers = {
            name: patch.object(merge_train_scheduler, name)
            for name in (
                "resolve_merge_train_policy_record",
                "evaluate_merge_train_admission_from_store",
                "resolve_merge_train_github_token",
                "execute_merge_train_controller_run_once",
                "execute_recorded_merge_train_run_once",
                "build_merge_train_pr_feedback_record",
            )
        }
        self.mocks = {name: patcher.start() for name, patcher in self.patchers.items()}
        for patcher in self.patchers.values():
            self.addCleanup(patcher.stop)
        self.mocks["resolve_merge_train_github_token"].return_value = "token"
        self.mocks["execute_merge_train_controller_run_once"].return_value = _controller_result()
        self.mocks["build_merge_train_pr_feedback_record"].return_value = SimpleNamespace(
            delivery_status="delivered"
        )

    def _run(self) -> tuple[MergeTrainScheduledTargetResult, ...]:
        return run_merge_train_scheduler_pass(
            record_store=self.record_store,
            control_plane_root=_ROOT,
            now=lambda: "2026-10-01T06:00:00Z",
        )

    def test_no_policy_record_is_an_empty_pass(self) -> None:
        self.mocks[
            "resolve_merge_train_policy_record"
        ].side_effect = MergeTrainPolicyStoreMissingError("missing")

        self.assertEqual(self._run(), ())

    def test_controller_evidence_reads_leave_the_scheduler_token_usable(self) -> None:
        self.mocks["resolve_merge_train_policy_record"].return_value = _policy_record(
            ("cbusillo/alpha", MergeTrainSchedulerPolicy(enabled=True, mutate=True)),
        )
        self.mocks["evaluate_merge_train_admission_from_store"].return_value = _admission(
            "admitted"
        )
        revoked: list[str] = []
        reads: list[str] = []

        def provider(**kwargs: object) -> object:
            token = kwargs["token"]
            assert isinstance(token, str)
            if kwargs.get("method") == "DELETE":
                revoked.append(token)
                return None
            self.assertNotIn(token, revoked)
            reads.append(token)
            return []

        def execute(**kwargs: object) -> MergeTrainControllerRunOnceResult:
            evaluator = kwargs["admission_evaluator"]
            assert isinstance(evaluator, LiveMergeAdmissionEvaluator)
            evidence = evaluator.repository_evidence_provider
            assert isinstance(evidence, GitHubRepositoryEvidenceProvider)
            for _ in range(2):
                self.assertEqual(evidence.list_open_pull_requests("cbusillo/alpha", limit=1), ())
            # The controller still needs its credential after admission evidence completes.
            provider(path="/repos/cbusillo/alpha/pulls", token=kwargs["token"])
            return _controller_result()

        self.mocks["execute_merge_train_controller_run_once"].side_effect = execute
        with (
            patch("control_plane.merge_train_scheduler.github_api_request", side_effect=provider),
            patch.object(
                merge_train_scheduler, "_deliver_controller_feedback", return_value=(0, 0)
            ),
        ):
            results = self._run()
        self.assertEqual(results[0].status, "ran")
        self.assertEqual(reads, ["token", "token", "token"])
        self.assertEqual(revoked, [])

    def test_one_pass_advances_planning_and_passed_checks_to_landing(self) -> None:
        feedback = patch.object(
            merge_train_scheduler, "_deliver_controller_feedback", return_value=(0, 0)
        )
        feedback.start()
        self.addCleanup(feedback.stop)
        self.mocks["resolve_merge_train_policy_record"].return_value = _policy_record(
            ("cbusillo/alpha", MergeTrainSchedulerPolicy(enabled=True, mutate=True)),
        )
        self.mocks["evaluate_merge_train_admission_from_store"].return_value = _admission(
            "admitted"
        )
        actions = (
            ("admit_collapsed_root", "planned"),
            ("build_candidate", "ready_for_checks"),
            ("observe_candidate", "passed"),
            ("plan_landing", "passed"),
            ("land_batch", "passed"),
        )
        self.mocks["execute_merge_train_controller_run_once"].side_effect = [
            MergeTrainControllerRunOnceResult(
                accepted_result={"controller_action": action, "candidate": {"status": status}},
                records={},
            )
            for action, status in actions
        ]
        results = self._run()
        self.assertEqual(len(results), len(actions))
        self.assertFalse(results[-1].continue_pass)

    def test_pending_candidate_checks_end_the_pass(self) -> None:
        feedback = patch.object(
            merge_train_scheduler, "_deliver_controller_feedback", return_value=(0, 0)
        )
        feedback.start()
        self.addCleanup(feedback.stop)
        self.mocks["resolve_merge_train_policy_record"].return_value = _policy_record(
            ("cbusillo/alpha", MergeTrainSchedulerPolicy(enabled=True, mutate=True)),
        )
        self.mocks["evaluate_merge_train_admission_from_store"].return_value = _admission(
            "admitted"
        )
        self.mocks["execute_merge_train_controller_run_once"].side_effect = [
            MergeTrainControllerRunOnceResult(
                accepted_result={"controller_action": action, "candidate": {"status": status}},
                records={},
            )
            for action, status in (
                ("plan_candidate", "planned"),
                ("build_candidate", "ready_for_checks"),
                ("observe_candidate", "ready_for_checks"),
            )
        ]
        self.assertEqual(len(self._run()), 3)

    def test_policy_disarmed_between_actions_stops_the_pass(self) -> None:
        feedback = patch.object(
            merge_train_scheduler, "_deliver_controller_feedback", return_value=(0, 0)
        )
        feedback.start()
        self.addCleanup(feedback.stop)
        enabled = _policy_record(
            ("cbusillo/alpha", MergeTrainSchedulerPolicy(enabled=True, mutate=True))
        )
        disabled = _policy_record(("cbusillo/alpha", MergeTrainSchedulerPolicy()))
        self.mocks["resolve_merge_train_policy_record"].side_effect = [enabled, enabled, disabled]
        self.mocks["evaluate_merge_train_admission_from_store"].return_value = _admission(
            "admitted"
        )
        self.mocks[
            "execute_merge_train_controller_run_once"
        ].return_value = MergeTrainControllerRunOnceResult(
            accepted_result={"controller_action": "plan_candidate"}, records={}
        )
        self.assertEqual(len(self._run()), 1)

    def test_repeated_progress_is_bounded_and_other_targets_run(self) -> None:
        feedback = patch.object(
            merge_train_scheduler, "_deliver_controller_feedback", return_value=(0, 0)
        )
        feedback.start()
        self.addCleanup(feedback.stop)
        self.mocks["resolve_merge_train_policy_record"].return_value = _policy_record(
            ("cbusillo/alpha", MergeTrainSchedulerPolicy(enabled=True, mutate=True)),
            ("cbusillo/beta", MergeTrainSchedulerPolicy(enabled=True, mutate=True)),
        )
        self.mocks["evaluate_merge_train_admission_from_store"].return_value = _admission(
            "admitted"
        )
        self.mocks[
            "execute_merge_train_controller_run_once"
        ].return_value = MergeTrainControllerRunOnceResult(
            accepted_result={"controller_action": "plan_candidate"}, records={}
        )
        results = self._run()
        self.assertTrue(any(result.repository == "cbusillo/beta" for result in results))

    def test_admission_is_rechecked_between_actions(self) -> None:
        feedback = patch.object(
            merge_train_scheduler, "_deliver_controller_feedback", return_value=(0, 0)
        )
        feedback.start()
        self.addCleanup(feedback.stop)
        self.mocks["resolve_merge_train_policy_record"].return_value = _policy_record(
            ("cbusillo/alpha", MergeTrainSchedulerPolicy(enabled=True, mutate=True)),
        )
        self.mocks["evaluate_merge_train_admission_from_store"].side_effect = [
            _admission("admitted"),
            _admission("deferred", "backoff_pending"),
        ]
        self.mocks[
            "execute_merge_train_controller_run_once"
        ].return_value = MergeTrainControllerRunOnceResult(
            accepted_result={"controller_action": "plan_candidate"}, records={}
        )
        results = self._run()
        self.assertEqual(results[-1].reason_code, "backoff_pending")
        self.mocks["execute_merge_train_controller_run_once"].assert_called_once()

    def test_only_scheduler_enabled_targets_run(self) -> None:
        self.mocks["resolve_merge_train_policy_record"].return_value = _policy_record(
            ("cbusillo/alpha", MergeTrainSchedulerPolicy(enabled=True)),
            ("cbusillo/beta", MergeTrainSchedulerPolicy()),
        )
        self.mocks["evaluate_merge_train_admission_from_store"].return_value = _admission(
            "admitted"
        )

        results = self._run()

        self.assertEqual([result.repository for result in results], ["cbusillo/alpha"])
        self.assertEqual(results[0].status, "ran")

    def test_deferred_admission_runs_nothing(self) -> None:
        self.mocks["resolve_merge_train_policy_record"].return_value = _policy_record(
            ("cbusillo/alpha", MergeTrainSchedulerPolicy(enabled=True)),
        )
        self.mocks["evaluate_merge_train_admission_from_store"].return_value = _admission(
            "deferred", "poll_interval_not_elapsed"
        )

        (result,) = self._run()

        self.assertEqual(
            (result.status, result.reason_code), ("deferred", "poll_interval_not_elapsed")
        )
        self.mocks["execute_merge_train_controller_run_once"].assert_not_called()

    def test_missing_token_fails_closed(self) -> None:
        self.mocks["resolve_merge_train_policy_record"].return_value = _policy_record(
            ("cbusillo/alpha", MergeTrainSchedulerPolicy(enabled=True)),
        )
        self.mocks["evaluate_merge_train_admission_from_store"].return_value = _admission(
            "admitted"
        )
        self.mocks["resolve_merge_train_github_token"].return_value = ""

        (result,) = self._run()

        self.assertEqual(
            (result.status, result.reason_code), ("failed", "github_token_not_configured")
        )
        self.mocks["execute_merge_train_controller_run_once"].assert_not_called()

    def test_one_failing_train_does_not_stop_the_others(self) -> None:
        self.mocks["resolve_merge_train_policy_record"].return_value = _policy_record(
            ("cbusillo/alpha", MergeTrainSchedulerPolicy(enabled=True)),
            ("cbusillo/beta", MergeTrainSchedulerPolicy(enabled=True)),
            ("cbusillo/gamma", MergeTrainSchedulerPolicy(enabled=True)),
        )
        self.mocks["evaluate_merge_train_admission_from_store"].return_value = _admission(
            "admitted"
        )
        self.mocks["execute_merge_train_controller_run_once"].side_effect = [
            RuntimeError("GitHub is down"),
            MergeTrainControllerLeaseHeldError("held"),
            _controller_result(),
        ]

        results = self._run()

        self.assertEqual(
            [(result.repository, result.status, result.reason_code) for result in results],
            [
                ("cbusillo/alpha", "failed", "RuntimeError"),
                ("cbusillo/beta", "deferred", "controller_lease_held"),
                ("cbusillo/gamma", "ran", ""),
            ],
        )

    def test_each_target_acts_on_the_policy_current_when_it_starts(self) -> None:
        both_enabled = _policy_record(
            ("cbusillo/alpha", MergeTrainSchedulerPolicy(enabled=True)),
            ("cbusillo/beta", MergeTrainSchedulerPolicy(enabled=True)),
        )
        beta_disabled = _policy_record(
            ("cbusillo/alpha", MergeTrainSchedulerPolicy(enabled=True)),
            ("cbusillo/beta", MergeTrainSchedulerPolicy()),
        )
        # The operator turns beta off while alpha's pass is running.
        self.mocks["resolve_merge_train_policy_record"].side_effect = [
            both_enabled,
            both_enabled,
            beta_disabled,
        ]
        self.mocks["evaluate_merge_train_admission_from_store"].return_value = _admission(
            "admitted"
        )

        results = self._run()

        self.assertEqual([result.repository for result in results], ["cbusillo/alpha"])
        self.mocks["execute_merge_train_controller_run_once"].assert_called_once()

    def test_a_stop_request_starts_no_further_targets(self) -> None:
        self.mocks["resolve_merge_train_policy_record"].return_value = _policy_record(
            ("cbusillo/alpha", MergeTrainSchedulerPolicy(enabled=True)),
            ("cbusillo/beta", MergeTrainSchedulerPolicy(enabled=True)),
        )
        self.mocks["evaluate_merge_train_admission_from_store"].return_value = _admission(
            "admitted"
        )
        stop_requested = Event()

        def run_controller_then_request_stop(**_: object) -> MergeTrainControllerRunOnceResult:
            stop_requested.set()
            return _controller_result()

        self.mocks[
            "execute_merge_train_controller_run_once"
        ].side_effect = run_controller_then_request_stop

        results = run_merge_train_scheduler_pass(
            record_store=self.record_store,
            control_plane_root=_ROOT,
            now=lambda: "2026-10-01T06:00:00Z",
            should_stop=stop_requested.is_set,
        )

        self.assertEqual([result.repository for result in results], ["cbusillo/alpha"])

    def test_dry_run_controller_pass_posts_no_feedback(self) -> None:
        self.mocks["resolve_merge_train_policy_record"].return_value = _policy_record(
            ("cbusillo/alpha", MergeTrainSchedulerPolicy(enabled=True)),
        )
        self.mocks["evaluate_merge_train_admission_from_store"].return_value = _admission(
            "admitted"
        )

        (result,) = self._run()

        request = self.mocks["execute_merge_train_controller_run_once"].call_args.kwargs["request"]
        self.assertFalse(request.mutate)
        self.assertEqual(result.feedback_delivered, 0)
        self.mocks["build_merge_train_pr_feedback_record"].assert_not_called()

    def test_mutating_controller_pass_delivers_feedback(self) -> None:
        self.mocks["resolve_merge_train_policy_record"].return_value = _policy_record(
            ("cbusillo/alpha", MergeTrainSchedulerPolicy(enabled=True, mutate=True)),
        )
        self.mocks["evaluate_merge_train_admission_from_store"].return_value = _admission(
            "admitted"
        )

        (result,) = self._run()

        self.assertTrue(
            self.mocks["execute_merge_train_controller_run_once"].call_args.kwargs["request"].mutate
        )
        feedback_request = self.mocks["build_merge_train_pr_feedback_record"].call_args.kwargs[
            "request"
        ]
        self.assertEqual(feedback_request.pull_request_number, 7)
        self.assertEqual(feedback_request.source, "launchplane:merge-train-scheduler")
        self.assertEqual(result.feedback_delivered, 1)
        self.record_store.write_merge_train_pr_feedback_record.assert_called_once()

    def test_candidate_less_wait_and_refresh_reach_scheduled_feedback(self) -> None:
        policy = _policy_record(
            ("cbusillo/alpha", MergeTrainSchedulerPolicy(enabled=True, mutate=True)),
        )
        snapshot = _FakeExpandedMergeTrainSnapshotReader(
            transport=object()
        ).read_merge_train_snapshot(repository="cbusillo/alpha", base_branch="main")
        selected = snapshot.pull_requests[1]
        scenarios = (
            {"required_checks_status": "pending"},
            {"mergeable": "unknown"},
            {"branch_update_required": True},
        )
        for changes in scenarios:
            with self.subTest(changes=changes):
                self.mocks["build_merge_train_pr_feedback_record"].reset_mock()
                self.record_store.write_merge_train_pr_feedback_record.reset_mock()
                self.mocks["resolve_merge_train_policy_record"].return_value = policy
                self.mocks["evaluate_merge_train_admission_from_store"].return_value = _admission(
                    "admitted"
                )
                queue = build_merge_train_dry_run_result(
                    policy=policy.policy,
                    snapshot=snapshot.model_copy(
                        update={"pull_requests": (selected.model_copy(update=changes),)}
                    ),
                    batch_landing=True,
                )
                response: dict[str, object] = {
                    "repository": queue.repository,
                    "base_branch": queue.base_branch,
                    "controller_action": queue.intended_next_action,
                    "dry_run_result": queue.model_dump(mode="json"),
                }
                if queue.intended_next_action == "update_branch":
                    branch_client = MagicMock()
                    response["branch_update_result"] = apply_merge_train_branch_update_intent(
                        dry_run_result=queue,
                        branch_client=cast(MergeTrainBranchClient, branch_client),
                    ).model_dump(mode="json")
                    branch_client.update_pull_request_branch.assert_called_once()
                self.mocks[
                    "execute_merge_train_controller_run_once"
                ].return_value = MergeTrainControllerRunOnceResult(
                    accepted_result=response, records={}
                )

                (result,) = self._run()

                self.assertEqual(result.feedback_delivered, 1)
                request = self.mocks["build_merge_train_pr_feedback_record"].call_args.kwargs[
                    "request"
                ]
                self.assertEqual(request.pull_request_number, selected.number)
                self.assertEqual(request.controller_record_id, "")
                self.assertEqual(request.controller_action, queue.intended_next_action)
                self.assertNotIn(queue.blocked_label, request.message)
                self.assertNotIn("candidate", request.message)
                if queue.intended_next_action == "update_branch":
                    self.assertIn("updated", request.message.lower())
                else:
                    self.assertEqual(request.event, "waiting")
                self.record_store.write_merge_train_pr_feedback_record.assert_called_once()

                # The same candidate-less result must never deliver in a dry run.
                self.mocks["build_merge_train_pr_feedback_record"].reset_mock()
                self.mocks["resolve_merge_train_policy_record"].return_value = _policy_record(
                    ("cbusillo/alpha", MergeTrainSchedulerPolicy(enabled=True)),
                )
                (dry_run,) = self._run()
                self.assertEqual(dry_run.feedback_delivered, 0)
                self.mocks["build_merge_train_pr_feedback_record"].assert_not_called()

    def test_level1_target_runs_the_level1_step(self) -> None:
        self.mocks["resolve_merge_train_policy_record"].return_value = _policy_record(
            (
                "cbusillo/alpha",
                MergeTrainSchedulerPolicy(enabled=True, runner_mode="level1", mutate=True),
            ),
        )
        self.mocks["evaluate_merge_train_admission_from_store"].return_value = _admission(
            "admitted"
        )
        self.mocks["execute_recorded_merge_train_run_once"].return_value = SimpleNamespace(
            records={"merge_train_run_id": "run-1"}
        )

        (result,) = self._run()

        call = self.mocks["execute_recorded_merge_train_run_once"].call_args.kwargs
        self.assertTrue(call["request"].mutate)
        self.assertIsNotNone(call["controller_state_store"])
        self.assertEqual(result.records, {"merge_train_run_id": "run-1"})
        self.mocks["execute_merge_train_controller_run_once"].assert_not_called()


class MergeTrainSchedulerLoopTests(TestCase):
    def test_event_wait_starts_a_fresh_pass_without_waiting_for_the_sweep(self) -> None:
        stop_event = MagicMock(spec=Event)
        stop_event.is_set.return_value = False
        event_wait = MagicMock()
        with patch.object(
            merge_train_scheduler, "run_merge_train_scheduler_pass", return_value=()
        ) as run:
            run_merge_train_scheduler_loop(
                record_store=object(),
                control_plane_root=_ROOT,
                stop_event=stop_event,
                max_passes=2,
                wait_for_event=event_wait,
                monotonic=lambda: 0,
            )
        self.assertEqual(run.call_count, 2)
        event_wait.assert_called_once()
        stop_event.wait.assert_not_called()

    def test_passes_start_on_the_interval_and_survive_a_failed_pass(self) -> None:
        clock = iter([0.0, 40.0, 300.0, 310.0])
        waits: list[float] = []
        stop_event = MagicMock(spec=Event)
        stop_event.is_set.return_value = False
        stop_event.wait.side_effect = lambda timeout: waits.append(timeout)
        passes: list[tuple[MergeTrainScheduledTargetResult, ...]] = []

        with patch.object(
            merge_train_scheduler,
            "run_merge_train_scheduler_pass",
            side_effect=[RuntimeError("policy unreadable"), ()],
        ):
            count = run_merge_train_scheduler_loop(
                record_store=object(),
                control_plane_root=_ROOT,
                interval_seconds=300,
                stop_event=stop_event,
                max_passes=2,
                pass_callback=passes.append,
                monotonic=lambda: next(clock),
            )

        self.assertEqual(count, 2)
        self.assertEqual(passes, [(), ()])
        # The first pass took 40 seconds, so the next one starts 260 seconds later.
        self.assertEqual(waits, [260.0])
